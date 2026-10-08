"""功能开关体系（Q16）—— 17 个开关，**每一个都真正生效**。

为什么这个文件是「修 bug」而不是「加功能」
==========================================

旧代码的推送开关体系是**死代码**（缺陷 **L21**，证据链见
``tests/regression/fixtures/behaviors.json``）::

    feishu_bot.py:2107  def push_if_enabled(layer, card)   -> 0 个调用点
    feishu_bot.py:2113  _should_push(layer)                -> 只被上面那个调用
    feishu_bot.py:2116  _in_quiet_hours()                  -> 只被上面那个调用
    push_receivers_l1/l2/report/alert                      -> 0 个调用点

后果：Admin 页面上的「L1 告警」「L2 播报」「静默时段」开关**改了没有任何
影响**；五条推送路径（L1 低电 / L2 摘要 / 违规 / 离线 / stale）全部绕过
检查直接 ``_post_feishu(card)``。

所以 Q16 不是「把现有开关搬到 UI」，而是**从零实现真正的开关体系**。
本模块就是那套体系。

铁律：**关闭 ≠ 报错**
=====================

===============  ==============================================
场景                       关闭后的行为
===============  ==============================================
群推送总开关关闭            ``post_card()`` 静默返回 ``False``，记 ``NOTICE``
                           （出口兜底）；告警路径更早一步就被
                           ``should_push()`` 拦下
机器人总开关关闭            ``/feishu/event`` 返回 200 但不处理
                           （**飞书侧不会报错重试**）
QQ 机器人总开关关闭         ``/qq/events`` 返回 200 ``{"code": 0}`` 但不处理
某告警层关闭                该层 ``should_push()`` 返回 False，记 ``NOTICE``
某命令关闭                  ``commands`` 回「该命令已禁用」，记 ``NOTICE``
===============  ==============================================

「静默降级」的工程含义：**调用方不需要写 try/except，也不需要判断
「为什么没发出去」** —— 它只问 :func:`should_push`，拿到 ``False`` 就
安静地跳过。至于原因（总开关？分项开关？静默时段？）由本模块记进日志。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import time

from starwatt import timeutil
from starwatt.config_registry import get_bool, get_str

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "COMMAND_FLAGS",
    "FLAGS",
    "FLAG_LABELS",
    "LAYER_FLAGS",
    "QUIET_HOURS_LAYERS",
    "Flag",
    "command_enabled",
    "enabled_flags",
    "in_quiet_hours",
    "is_enabled",
    "layer_enabled",
    "own_value",
    "should_push",
    "why_suppressed",
]

#: 总开关的 key
GROUP_MASTER = "push_group_enabled"
BOT_MASTER = "push_bot_enabled"

#: QQ 官方机器人总开关（独立渠道，不隶属飞书群/私聊开关）
QQ_MASTER = "qq_bot_enabled"


@dataclass(frozen=True, slots=True)
class Flag:
    """一个开关的声明。"""

    key: str
    label: str
    parent: str | None = None  # 总开关；父为 False 时本项一律无效


def _flag(key: str, label: str, parent: str | None = None) -> Flag:
    return Flag(key=key, label=label, parent=parent)


_ALL_FLAGS: tuple[Flag, ...] = (
    # -- 总开关 ----------------------------------------------------------
    _flag(GROUP_MASTER, "群推送总开关"),
    _flag(BOT_MASTER, "私聊机器人总开关"),
    _flag(QQ_MASTER, "QQ 机器人总开关"),
    # -- 告警层（群推送）--------------------------------------------------
    _flag("push_l1_enable", "L1 低电告警", GROUP_MASTER),
    _flag("push_l2_enable", "L2 常规摘要卡", GROUP_MASTER),
    _flag("push_daily_enable", "L3 日报", GROUP_MASTER),
    _flag("push_weekly_enable", "L3 周报", GROUP_MASTER),
    _flag("push_monthly_enable", "L3 月报", GROUP_MASTER),
    _flag("push_offline_enable", "L4 电表离线", GROUP_MASTER),
    _flag("push_violation_enable", "违规告警", GROUP_MASTER),
    _flag("push_stale_enable", "数据陈旧", GROUP_MASTER),
    # -- 命令（私聊机器人）------------------------------------------------
    _flag("cmd_remain_enabled", "/状态 /剩余", BOT_MASTER),
    _flag("cmd_meter_enabled", "/电表", BOT_MASTER),
    _flag("cmd_today_enabled", "/今日", BOT_MASTER),
    _flag("cmd_history_enabled", "/历史", BOT_MASTER),
    _flag("cmd_pay_enabled", "/缴费", BOT_MASTER),
    _flag("cmd_violations_enabled", "/违规", BOT_MASTER),
    _flag("cmd_help_enabled", "/帮助", BOT_MASTER),
)

#: ``key -> Flag``（唯一真相）
FLAGS: dict[str, Flag] = {f.key: f for f in _ALL_FLAGS}

#: ``key -> 中文标签``（UI 与文档用）
FLAG_LABELS: dict[str, str] = {f.key: f.label for f in _ALL_FLAGS}

#: 告警层别名 → 开关 key。**这是 L21 的修复点** —— 每个层都必须映射到
#: 一个真实开关，不允许「没有开关的层」存在。
LAYER_FLAGS: dict[str, str] = {
    "l1": "push_l1_enable",
    "l2": "push_l2_enable",
    "daily": "push_daily_enable",
    "weekly": "push_weekly_enable",
    "monthly": "push_monthly_enable",
    "offline": "push_offline_enable",
    "violation": "push_violation_enable",
    "stale": "push_stale_enable",
}

#: 命令名（去掉斜杠）→ 开关 key
COMMAND_FLAGS: dict[str, str] = {
    "remain": "cmd_remain_enabled",
    "status": "cmd_remain_enabled",  # /状态 与 /剩余 共用同一个开关
    "meter": "cmd_meter_enabled",
    "today": "cmd_today_enabled",
    "history": "cmd_history_enabled",
    "pay": "cmd_pay_enabled",
    "violations": "cmd_violations_enabled",
    "help": "cmd_help_enabled",
}

#: 静默时段只对**这些层**生效（日报 / 周报 / 月报照常发 —— 用户主动订阅的）
QUIET_HOURS_LAYERS: frozenset[str] = frozenset({"l1", "l2"})


def _lookup(key: str) -> Flag:
    flag = FLAGS.get(key)
    if flag is None:
        # 拼错开关名是**编程错误**，不是运行时数据问题。
        # 静默返回 True/False 都会让「告警从此不发」这类 bug 藏很久 ——
        # 所以这里**大声失败**，并由 test_flags 的源码扫描提前拦住。
        raise KeyError(f"未注册的开关：{key!r}（可用：{sorted(FLAGS)}）")
    return flag


def is_enabled(key: str) -> bool:
    """开关是否生效（**总开关优先**，其次分项开关）。

    总开关关闭时，分项开关的值**被忽略** —— 用户不必逐个关掉 8 个层。
    """
    flag = _lookup(key)
    if flag.parent is not None and not get_bool(flag.parent):
        return False
    return get_bool(flag.key)


def own_value(key: str) -> bool:
    """本开关**自身的值**（不看总开关）。

    只给「已经自己判过总开关」的调用方用，典型是
    :mod:`starwatt.notify.commands`：渠道层（``dispatcher.handle_event`` /
    ``qq.handle_event``）先按 ``BOT_MASTER`` / ``QQ_MASTER`` 决定整条渠道
    是否静默，命令层再按各自的 ``cmd_*_enabled`` 决定**这一条命令**是否可用。

    两处都用 :func:`is_enabled` 会出问题：机器人总开关默认**关闭**，那样连
    「命令层单独渲染文本」都会被连带否决，而且渠道层的总开关语义会被判两遍。
    """
    return get_bool(_lookup(key).key)


def enabled_flags() -> list[str]:
    """当前处于「开启」状态的开关 key 列表（UI 展示用）。"""
    return [key for key in FLAGS if is_enabled(key)]


def layer_enabled(layer: str) -> bool:
    """告警层是否开启（``layer`` 取 :data:`LAYER_FLAGS` 的键）。"""
    key = LAYER_FLAGS.get(layer)
    if key is None:
        raise KeyError(f"未知的告警层：{layer!r}（可用：{sorted(LAYER_FLAGS)}）")
    return is_enabled(key)


def command_enabled(command: str) -> bool:
    """命令是否开启（``command`` 是去掉斜杠的命令名）。"""
    key = COMMAND_FLAGS.get(command)
    if key is None:
        raise KeyError(f"未知的命令：{command!r}（可用：{sorted(COMMAND_FLAGS)}）")
    return is_enabled(key)


# ---------------------------------------------------------------------------
# 静默时段（B9）—— 旧版是死代码（L21），现在真正生效
# ---------------------------------------------------------------------------
def _parse_hhmm(text: str, fallback: tuple[int, int]) -> time:
    """``"23:00"`` → ``time(23, 0)``；非法值回落 ``fallback``（不抛异常）。"""
    try:
        hour, minute = (int(part) for part in text.strip().split(":"))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return time(hour, minute)
    except (ValueError, AttributeError):
        pass
    logger.warning("静默时段配置无法解析：%r，回落 %02d:%02d", text, *fallback)
    return time(*fallback)


def in_quiet_hours(moment=None) -> bool:
    """当前（北京时间）是否落在静默时段内。

    正确处理**跨午夜**的区间：``23:00``–``07:00`` 表示
    ``[23:00, 24:00) ∪ [00:00, 07:00)``。

    起止相同 → 视为**不静默**：用户把 ``quiet_hours_start`` 与
    ``quiet_hours_end`` 填成同一个值时，意图显然是「关掉静默」，
    而不是「整天静默」（后者会让告警永远发不出去，很难排查）。
    """
    start = _parse_hhmm(get_str("quiet_hours_start", "23:00"), (23, 0))
    end = _parse_hhmm(get_str("quiet_hours_end", "07:00"), (7, 0))
    if start == end:
        return False

    current = (moment or timeutil.now_cst()).time()
    if start < end:
        return start <= current < end
    # 跨午夜
    return current >= start or current < end


def why_suppressed(layer: str) -> str | None:
    """返回抑制原因；不应抑制则返回 ``None``。

    返回中文短语，直接进日志 —— 排查「为什么没收到推送」时一眼可见。
    """
    key = LAYER_FLAGS.get(layer)
    if key is None:
        raise KeyError(f"未知的告警层：{layer!r}")
    flag = FLAGS[key]

    if flag.parent is not None and not get_bool(flag.parent):
        return f"总开关 {flag.parent} 已关闭"
    if not get_bool(key):
        return f"{flag.label}（{key}）已关闭"
    if layer in QUIET_HOURS_LAYERS and in_quiet_hours():
        start = get_str("quiet_hours_start", "23:00")
        end = get_str("quiet_hours_end", "07:00")
        return f"处于静默时段 {start}–{end}"
    return None


def should_push(layer: str) -> bool:
    """该告警层此刻是否应该推送（**L21 的统一入口**）。

    所有推送路径都必须先过这一关 —— 这就是旧代码缺的那一步。
    被抑制时记 ``NOTICE``（级别 25，见 ``logging_setup``）：用户主动关的
    开关不是异常，不该刷 ``WARNING``。
    """
    reason = why_suppressed(layer)
    if reason is None:
        return True
    logger.log(25, "推送抑制：layer=%s，原因=%s", layer, reason)  # NOTICE
    return False
