"""9 个命令 + 菜单映射 —— **渠道无关**（飞书私聊 / QQ 通用）。

设计
====

命令层只做「**文本进 → 文本出**」，不认识任何平台：

* 飞书 dispatcher：验签/解密 → 本模块 → ``im/v1/messages`` 回复（可附 PNG）
* QQ dispatcher：验 Ed25519 → 本模块 → ``/v2/.../messages`` 回复

这样「加一个渠道」不需要重写任何一条命令（Q18 扩展点）。

``COMMANDS`` / ``MENU_KEYS`` / ``HELP_TEXT`` 与 legacy ``feishu_bot.py``
**逐字一致**（含 emoji 与空格）—— 用户的肌肉记忆不该被重写打断。

零凭据 / 零数据的降级文案
========================

legacy 明确要求「无凭据时不抛异常，返回一句礼貌的中文」。这里全部保留：

==========================================  ====================================
情况                                        回复
==========================================  ====================================
没有 roomId（从未抓取成功）                  「暂未发现 roomId,请先跑一次抓取。」
表是空的                                    「暂未抓到电量数据,等下次抓取。」等
未知命令                                    「未知命令: xxx」+ 完整帮助
==========================================  ====================================

⚠️ 文案里的半角逗号是 legacy 的原样（不是笔误），改了会与用户的截图对不上。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from starwatt import flags
from starwatt.config_registry import get_str
from starwatt.db.coerce import coerce_float, coerce_str
from starwatt.db.repositories import (
    DailyElecRepo,
    PayRepo,
    RecordRepo,
    RunStatusRepo,
    ViolationRepo,
)

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "COMMANDS",
    "DISABLED_TEXT",
    "HELP_TEXT",
    "MENU_KEYS",
    "Reply",
    "current_room_id",
    "dispatch_menu",
    "dispatch_text",
    "parse_history_args",
    "strip_mention",
]

#: 斜杠命令 → 处理器名（与 legacy 一致）
COMMANDS: dict[str, str] = {
    "/状态": "remain",
    "/剩余": "remain",
    "/电表": "meter",
    "/电表状态": "meter",
    "/今日": "today",
    "/历史": "history",
    "/缴费": "pay",
    "/违规": "violations",
    "/帮助": "help",
}

#: 自定义菜单 ``event_key`` → 处理器名（与 legacy 一致）
MENU_KEYS: dict[str, str] = {
    "menu_remain": "remain",
    "menu_meter": "meter",
    "menu_today": "today",
    "menu_history": "history",
    "menu_pay": "pay",
    "menu_violations": "violations",
    "menu_help": "help",
}

#: 帮助文本（**逐字**沿用 legacy）
HELP_TEXT = """⚡ 宿舍电量小助手 · 命令列表

/状态 /剩余 — 当前剩余电量
/电表 /电表状态 — 实时电表 (电压 / 电流 / 功率)
/今日 — 今天用了多少度
/历史 [N] — 最近 N 小时趋势 (默认 24)
/缴费 — 最近 3 笔缴费
/违规 — 最近 30 天违规
/帮助 — 显示本条

群里 @ 我发命令。私聊直接发。"""

#: 命令 → 图片卡片标题（renderer 落地后使用；空 = 纯文本回复）
TITLES: dict[str, str] = {
    "remain": "⚡ 剩余电量",
    "meter": "⚡ 实时电表",
    "today": "📅 今日用电",
    "history": "📊 用电历史",
    "pay": "📋 缴费记录",
    "violations": "⚠ 违规记录",
    "help": "⚡ 宿舍电量小助手 · 命令列表",
}

#: 命令被开关关闭时的统一回复（Q16：**静默降级**，不是报错）
DISABLED_TEXT = "该命令已禁用（可在管理后台「功能开关」中重新开启）。"

#: 在线标签（与 ``policies`` / 注册表默认值一致）
_ONLINE = ("在线", "正常", "通讯正常")

#: 群聊里的 @机器人 前缀：
#: * 飞书：``@_user_1 /电表``（``@`` + 非空白）
#: * QQ：``<@!1234567890> /状态``（尖括号包裹，可能是 ``<@!id>`` 或 ``<@id>``）
_MENTION_RE = re.compile(r"^\s*(?:(?:<@[!&]?[^>\s]+>|@\S+)\s*)*")
_DIGITS_RE = re.compile(r"^\d+$")


@dataclass(frozen=True, slots=True)
class Reply:
    """一次命令回复。

    ``text`` 是所有渠道都能发的内容（QQ / 纯文本降级路径）；
    ``title`` 是图片卡片的标题 —— 渲染器可用时渠道可以选择发图。
    """

    text: str
    title: str = ""

    @property
    def is_empty(self) -> bool:
        """空回复 = 不该回（例如群里别人 @ 了机器人但没发命令）。"""
        return not self.text.strip()


def current_room_id() -> str | None:
    """当前房间号（``last_room_id``，抓取时发布）；读不到返回 ``None``。"""
    try:
        return coerce_str(get_str("last_room_id", ""))
    except Exception:  # noqa: BLE001 —— 首次启动 meta 表可能还没建
        logger.debug("读取 last_room_id 失败 —— 命令层按「无房间」处理")
        return None


def strip_mention(text: str) -> str:
    """剥掉群聊里 @机器人 的前缀。

    两种形态都要认（Q18 加渠道时踩到的坑）：

    * 飞书：``@_user_1 /电表`` → ``/电表``
    * QQ：``<@!1234567890> /状态`` → ``/状态``

    只有 @ 没有正文的消息会被剥成空串 —— 调用方据此**不回复**
    （群里别人 @ 了机器人但没发命令时不该刷屏）。
    """
    if not text:
        return ""
    return _MENTION_RE.sub("", text).strip()


def parse_history_args(args: str) -> int:
    """``/历史 N`` 的 N：默认 24，非法或超范围（>720）一律回落 24。"""
    raw = (args or "").strip()
    if not raw or not _DIGITS_RE.match(raw):
        return 24
    hours = int(raw)
    if hours <= 0 or hours > 720:
        return 24
    return hours


# ---------------------------------------------------------------------------
# 7 个命令实现（文本版 —— 也是图片渲染失败时的降级内容）
# ---------------------------------------------------------------------------
_NO_ROOM = "暂未发现 roomId,请先跑一次抓取。"


def cmd_remain() -> str:
    """``/状态`` ``/剩余`` —— 当前剩余电量。"""
    latest = RecordRepo.latest()
    if latest is None:
        return "暂未抓到电量数据,等下次抓取。"
    remain = coerce_float(latest.remain)
    read_time = coerce_str(latest.read_time) or "—"
    if remain is None:
        return f"抄表时间: {read_time}\n剩余电量数据缺失。"
    return f"⚡ 当前剩余: {remain:.2f} kW·h\n🕐 抄表时间: {read_time}"


def cmd_meter(room_id: str | None = None) -> str:
    """``/电表`` —— 实时电表（电压 / 电流 / 有功功率）。"""
    room = room_id or current_room_id()
    if not room:
        return _NO_ROOM
    row = RunStatusRepo.get(room)
    if row is None:
        return "暂无电表数据,等下一次抓取。"

    vol = coerce_float(row.vol)
    cur = coerce_float(row.cur)
    yggl = coerce_float(row.yggl)
    label = coerce_str(row.run_status) or "—"
    update_dt = coerce_str(row.update_dt) or "—"
    icon = "🟢" if label in _ONLINE else "🔴"
    parts = [
        f"{icon} 电表状态: {label}",
        f"⚡ 电压: {vol:.2f} V" if vol is not None else "⚡ 电压: —",
        f"🔌 电流: {cur:.2f} A" if cur is not None else "🔌 电流: —",
        f"💡 有功功率: {yggl:.3f} W" if yggl is not None else "💡 有功功率: —",
        f"🕐 最后上报: {update_dt}",
    ]
    return "\n".join(parts)


def cmd_today(room_id: str | None = None) -> str:
    """``/今日`` —— 今天用了多少度（``total_eq`` 优先，``zong_eq`` 兜底）。"""
    room = room_id or current_room_id()
    if not room:
        return _NO_ROOM
    rows = DailyElecRepo.recent(room, days=2)  # 2 天窗口保证「今天」一定在范围内
    if not rows:
        return "暂无今日用电数据,等下一次抓取。"
    today = max(str(r.dt) for r in rows)
    today_row = next((r for r in rows if str(r.dt) == today), None)
    if today_row is None:
        return "暂无今日用电数据。"
    used = coerce_float(today_row.total_eq)
    if used is None:
        used = coerce_float(today_row.zong_eq)
    if used is None:
        return f"日期 {today}: 今日用电数据缺失。"
    return f"📅 {today}\n⚡ 今日用电: {used:.2f} kW·h"


def cmd_history(room_id: str | None = None, hours: int = 24) -> str:
    """``/历史 [N]`` —— 近 N 小时的消耗（首尾剩余电量之差）。"""
    room = room_id or current_room_id()
    if not room:
        return _NO_ROOM
    rows = RecordRepo.query(hours=hours)
    if not rows:
        return f"近 {hours} 小时无抓取数据。"
    # 显式循环收窄类型（``coerce_float`` 返回 ``float | None``）——
    # 这样下面的差值算术不必再判 None，mypy 也能过。
    valid: list[tuple[str, float]] = []
    for row in rows:
        value = coerce_float(row.remain)
        if value is not None:
            valid.append((str(row.ts), value))
    if not valid:
        return f"近 {hours} 小时无有效剩余电量数据。"
    first_ts, first_value = valid[0]
    last_ts, last_value = valid[-1]
    delta = first_value - last_value
    return (
        f"📊 近 {hours} 小时用电\n"
        f"开始 ({first_ts}): {first_value:.2f} kW·h\n"
        f"结束 ({last_ts}): {last_value:.2f} kW·h\n"
        f"消耗: {delta:.2f} kW·h"
    )


def cmd_pay(room_id: str | None = None) -> str:
    """``/缴费`` —— 最近 3 笔缴费记录。"""
    room = room_id or current_room_id()
    if not room:
        return _NO_ROOM
    rows = PayRepo.recent(room, days=90)
    if not rows:
        return "📋 暂无缴费记录。"
    parts = ["📋 最近缴费 (最近 3 笔):"]
    for row in rows[:3]:
        dt = coerce_str(row.dt) or "—"
        pay_type = coerce_str(row.pay_type) or "—"
        fee_type = coerce_str(row.fee_type) or "—"
        money = coerce_float(row.money)
        money_str = f"¥{money:.2f}" if money is not None else "—"
        parts.append(f"  · {dt} {pay_type} / {fee_type} {money_str}")
    return "\n".join(parts)


def cmd_violations(room_id: str | None = None) -> str:
    """``/违规`` —— 近 30 天违规记录（最多列 8 条）。"""
    room = room_id or current_room_id()
    if not room:
        return _NO_ROOM
    rows = ViolationRepo.recent(room, days=30)
    if not rows:
        return "✓ 近 30 天无违规记录,太棒了！"
    parts = [f"⚠ 近 30 天违规 ({len(rows)} 条):"]
    for row in rows[:8]:
        dt = coerce_str(row.dt) or "—"
        reason = coerce_str(row.wg_reason) or "—"
        power = coerce_float(row.wg_power)
        # 📌 wg_power 是瞬时功率（kW），不是电量（kW·h）—— L7 的修正
        power_str = f"{power:.2f} kW" if power is not None else "—"
        parts.append(f"  · {dt} {reason} {power_str}")
    if len(rows) > 8:
        parts.append(f"  · …另有 {len(rows) - 8} 条未显示。")
    return "\n".join(parts)


def cmd_help() -> str:
    """``/帮助`` —— 命令列表。"""
    return HELP_TEXT


# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
def _command_switch(handler: str) -> str | None:
    """处理器名 → 开关 key（``remain`` → ``cmd_remain_enabled``）。"""
    return flags.COMMAND_FLAGS.get(handler)


def _command_enabled(handler: str) -> bool:
    """Q16：命令开关关闭 → **静默降级**（回一句说明，不抛异常）。

    这一层是**唯一**的命令开关卡点：``dispatch_text`` 与 ``dispatch_menu`` 都
    经过 :func:`_run`，所以飞书私聊与 QQ 两个渠道一次覆盖（Q18：加渠道不必
    重写命令）。

    用 :func:`starwatt.flags.own_value` 而非 ``is_enabled``：总开关（机器人是否
    启用整条渠道）由渠道层判过（``dispatcher.handle_event`` 查 ``BOT_MASTER``、
    ``qq.handle_event`` 查 ``QQ_MASTER``），这里只关心**这一条命令**自己的开关。
    未知处理器返回 ``True`` —— 让下面原有的「未知处理器」warning 分支去报错。
    """
    key = _command_switch(handler)
    if key is None or flags.own_value(key):
        return True
    logger.log(25, "命令 %s 已被开关 %s 关闭 —— 静默降级", handler, key)  # NOTICE
    return False


def _run(handler: str, room_id: str | None, hours: int = 24) -> str:
    """按处理器名调用命令实现（**未知处理器返回空串**）。"""
    if not _command_enabled(handler):
        return DISABLED_TEXT
    if handler == "remain":
        return cmd_remain()
    if handler == "meter":
        return cmd_meter(room_id)
    if handler == "today":
        return cmd_today(room_id)
    if handler == "history":
        return cmd_history(room_id, hours)
    if handler == "pay":
        return cmd_pay(room_id)
    if handler == "violations":
        return cmd_violations(room_id)
    if handler == "help":
        return cmd_help()
    logger.warning("未知的命令处理器：%r", handler)
    return ""


def dispatch_text(text: str, room_id: str | None = None) -> Reply:
    """解析一条消息文本 → :class:`Reply`（空 ``text`` = 不回复）。

    空消息 / 只有 @ 的消息 → 空回复（群里别人闲聊也会触发事件，
    不能每条都回一遍）。
    """
    cleaned = strip_mention(text)
    if not cleaned:
        return Reply(text="")

    command, _, args = cleaned.partition(" ")
    command = command.strip()
    handler = COMMANDS.get(command)
    if handler is None:
        return Reply(text=f"未知命令: {command}\n\n{HELP_TEXT}")

    hours = parse_history_args(args) if handler == "history" else 24
    return Reply(
        text=_run(handler, room_id, hours),
        title=TITLES.get(handler, ""),
    )


def dispatch_menu(event_key: str, room_id: str | None = None) -> Reply:
    """菜单点击 → :class:`Reply`（未知菜单项给一句礼貌回复）。"""
    handler = MENU_KEYS.get(event_key or "")
    if handler is None:
        return Reply(text="未知菜单项。")
    return Reply(
        text=_run(handler, room_id, 24),
        title=TITLES.get(handler, ""),
    )
