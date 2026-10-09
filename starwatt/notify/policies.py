"""告警判定与去重 —— 「**什么时候推、推不推**」（M3）。

分工
====

===============  =========================================================
模块              负责
===============  =========================================================
``flags.py``      **开关**：总开关 / 分项开关 / 静默时段（Q16，关闭即静默降级）
``policies.py``   本文件：**跃迁判定 + 冷却 + 幂等打点 + 组装并发送**
``card_builder``  卡片长什么样
``transport``     怎么发出去
===============  =========================================================

四条告警的判定规则（全部来自 legacy，逐条对齐）
=============================================

L1 低电（``push_low_battery_if_due``）
    只在**非低电 → 低电**的跃迁时推；同一段低电期保持安静。
    重写额外实现注册表里的 ``low_battery_rearm``（默认 40 度）：
    **回升到该值以上才算重新武装** —— 否则「充一点 → 又用完」会反复轰炸。

违规（``push_violations_if_due``）
    冷却 ``violation_cooldown_min``（默认 30 分钟）+ 只推 ``dt`` **晚于**
    上次告警时间的记录。两重条件缺一不可。

stale（``push_stale_if_due``）
    距上次**成功**抓取超过 ``stale_hours``（默认 2 小时），且
    ``last_stale_alert_at`` 不晚于 ``last_scrape_at``（同一段陈旧期只推一次）。

离线（``push_offline_if_due``）
    电表状态不在 ``offline_status_values`` 里（**含字段缺失**）即视为离线
    （B6：宁可误报也不能静默失败）。

L3 日报 / 周报 / 月报（``l3_due`` + ``stamp_l3``）
    到点（配置的 HH:MM）+ 当天/当周/当月还没推过。**打点在推送成功之后**
    —— 失败的下一个 tick 还能重试。

幂等铁律
========

所有 ``push_*`` 函数**只在发送成功后**才写状态键。发送失败（webhook 抖动、
未配置）→ 不打点 → 下个 tick 重试。反过来写（先打点再发）会让一次网络抖动
静默吞掉一整天的告警。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from starwatt import flags, timeutil
from starwatt.config_registry import get_float, get_int, get_json, get_str, set_state
from starwatt.db.coerce import coerce_float, coerce_str
from starwatt.db.repositories import RecordRepo, RunStatusRepo, ViolationRepo
from starwatt.notify import card_builder as cards
from starwatt.notify import transport

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "DEFAULT_PUSH_TIMES",
    "L3_KINDS",
    "STATE_KEYS",
    "is_offline",
    "l3_due",
    "low_battery_due",
    "offline_card_payload",
    "push_l3_if_due",
    "push_low_battery_if_due",
    "push_offline_if_due",
    "push_stale_if_due",
    "push_summary",
    "push_violations_if_due",
    "read_push_time",
    "room_label",
    "stale_due_gap",
    "stamp_l3",
    "violation_rows_to_push",
]

#: 状态键（与 legacy meta 键名完全一致 —— 零迁移）
STATE_KEYS: dict[str, str] = {
    "low_battery": "last_low_battery_alert_at",
    "violation": "last_violation_alert_at",
    "stale": "last_stale_alert_at",
    "daily": "last_daily_report_date",
    "weekly": "last_weekly_report_iso",
    "monthly": "last_monthly_report_mo",
    "scrape": "last_scrape_at",
}

#: L3 三种报表
L3_KINDS: tuple[str, ...] = ("daily", "weekly", "monthly")

#: L3 默认推送时间（HH:MM）
DEFAULT_PUSH_TIMES: dict[str, str] = {
    "daily": "09:00",
    "weekly": "09:00",
    "monthly": "09:00",
}

#: 违规卡最多提示多少条（与 card_builder 的折叠阈值一致）
_VIOLATION_MAX = 8


def _now(moment: datetime | None = None) -> datetime:
    """统一时间源（可注入，测试友好）。"""
    return moment or timeutil.now_cst()


def _parse(raw: object) -> datetime | None:
    return timeutil.parse_stamp(raw)


def _state_time(key: str) -> datetime | None:
    return _parse(get_str(key, ""))


# ---------------------------------------------------------------------------
# 离线判定
# ---------------------------------------------------------------------------
def _run_status_label(run_status: Any) -> str | None:
    """从 ``RunStatus`` 实体 / dict 里取 ``runStatus`` 文本。"""
    if run_status is None:
        return None
    if isinstance(run_status, Mapping):
        raw = run_status.get("run_status") or run_status.get("runStatus")
    else:
        raw = getattr(run_status, "run_status", None)
    return coerce_str(raw)


def is_offline(run_status: Any) -> bool:
    """电表是否离线（**缺字段也算离线** —— B6 防静默失败）。"""
    label = _run_status_label(run_status)
    if label is None:
        return True
    values = get_json("offline_status_values", None)
    online = (
        tuple(str(v) for v in values)
        if isinstance(values, list) and values
        else ("在线", "正常", "通讯正常")
    )
    return label not in online


def offline_card_payload(run_status: Any) -> dict[str, Any]:
    """把 ``RunStatus`` 实体转成**卡片契约**用的驼峰键 dict。

    📌 这里修掉一个真 bug：legacy 直接把 DB 行（``run_status`` / ``work_status``
    / ``stop_reason`` / ``update_dt`` 蛇形键）喂给 ``_build_offline_card``
    （只认驼峰），于是离线卡**永远显示 —** —— ``cards.json`` 里三个用例输出
    完全相同，正是这个 bug 的证据。
    """
    if run_status is None:
        return {}
    if isinstance(run_status, Mapping):
        get = run_status.get
    else:

        def get(key: str, default: Any = None) -> Any:
            return getattr(run_status, key, default)

    def _pick(*names: str) -> Any:
        for name in names:
            value = get(name)
            if value not in (None, ""):
                return value
        return None

    return {
        "runStatus": _pick("run_status", "runStatus"),
        "workStatus": _pick("work_status", "workStatus"),
        "stopReason": _pick("stop_reason", "stopReason"),
        "updateDt": _pick("update_dt", "updateDt"),
    }


# ---------------------------------------------------------------------------
# L1 低电
# ---------------------------------------------------------------------------
def low_battery_due(*, now: datetime | None = None) -> bool:
    """此刻是否该推低电告警（跃迁 + 重新武装判定）。"""
    rows = RecordRepo.query(hours=24)  # 升序
    if not rows:
        return False

    red = get_float("threshold_red", cards.DEFAULT_RED_BELOW)
    current = coerce_float(rows[-1].remain)
    if current is None or current >= red:
        return False  # 不低电 / 数据缺失 → 不打扰

    last_alert = _state_time(STATE_KEYS["low_battery"])
    if last_alert is not None:
        # 已告警过：只有在「回升到 rearm 以上」之后才允许再次告警
        rearm = get_float("low_battery_rearm", 40.0)
        for row in rows:
            ts = _parse(row.ts)
            remain = coerce_float(row.remain)
            if ts is not None and ts >= last_alert and remain is not None and remain >= rearm:
                return True
        return False

    # 从未告警：沿用 legacy 的「前一条是否也低」跃迁判定
    prior = coerce_float(rows[-2].remain) if len(rows) >= 2 else None
    return prior is None or prior >= red


# ---------------------------------------------------------------------------
# 违规
# ---------------------------------------------------------------------------
def violation_rows_to_push(
    room_id: str, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """返回**需要提示的新违规行**（空列表 = 不推）。

    两道闸门：冷却期（``violation_cooldown_min``）+ 时间过滤（``dt`` 晚于
    上次告警时间）。``dt`` 解析失败的行走「视为新记录」（与 legacy 一致）。
    """
    if not room_id:
        return []

    moment = _now(now)
    last_alert = _state_time(STATE_KEYS["violation"])
    cooldown = timedelta(minutes=get_int("violation_cooldown_min", 30))
    if last_alert is not None and moment - last_alert < cooldown:
        return []

    fresh: list[dict[str, Any]] = []
    for row in ViolationRepo.recent(room_id, days=30):
        data = row.as_dict()
        dt = _parse(coerce_str(data.get("dt")))
        if dt is None or last_alert is None or dt > last_alert:
            fresh.append(data)
    return fresh[:_VIOLATION_MAX]


# ---------------------------------------------------------------------------
# stale
# ---------------------------------------------------------------------------
def stale_due_gap(*, now: datetime | None = None) -> float | None:
    """需要推 stale 告警时返回间隔秒数；不需要则 ``None``。"""
    last_scrape = _state_time(STATE_KEYS["scrape"])
    if last_scrape is None:
        return None  # 从未成功抓取 → 没有「陈旧」可言

    gap = (_now(now) - last_scrape).total_seconds()
    if gap < get_int("stale_hours", 2) * 3600:
        return None

    last_alert = _state_time(STATE_KEYS["stale"])
    if last_alert is not None and last_alert > last_scrape:
        return None  # 这段陈旧期已经提醒过了
    return gap


# ---------------------------------------------------------------------------
# L3 报表节奏
# ---------------------------------------------------------------------------
def read_push_time(key: str, default_hhmm: str) -> tuple[int, int]:
    """读 ``HH:MM`` 配置 → ``(hour, minute)``；非法值回落默认（**不抛**）。"""
    raw = coerce_str(get_str(key, "")) or default_hhmm
    try:
        hour_s, minute_s = raw.split(":")
        hour, minute = int(hour_s), int(minute_s)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("hour/minute out of range")
        return hour, minute
    except (ValueError, AttributeError):
        logger.warning("推送时间配置无法解析：%r，回落 %s", raw, default_hhmm)
        hour_s, minute_s = default_hhmm.split(":")
        return int(hour_s), int(minute_s)


def _l3_stamp_value(kind: str, moment: datetime) -> str:
    """本次报表的幂等键：``2026-10-06`` / ``2026-W41`` / ``2026-10``。"""
    if kind == "weekly":
        iso = moment.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if kind == "monthly":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y-%m-%d")


def _l3_enabled(kind: str) -> bool:
    """该报表是否开启（**走开关体系**：总开关关闭时自动为 False，Q16）。"""
    return flags.is_enabled(flags.LAYER_FLAGS[kind])


def l3_due(kind: str, *, now: datetime | None = None) -> bool:
    """该 L3 报表此刻是否该发（到点 + 本周期还没发过）。

    ``weekly`` 只在周一发（ISO weekday 1），``monthly`` 只在 1 号发。
    """
    if kind not in L3_KINDS:
        raise KeyError(f"未知的 L3 报表类型：{kind!r}（可用：{list(L3_KINDS)}）")
    if not _l3_enabled(kind):
        return False

    moment = _now(now)
    hour, minute = read_push_time(f"push_{kind}_time", DEFAULT_PUSH_TIMES[kind])
    if (moment.hour, moment.minute) != (hour, minute):
        return False
    if kind == "weekly" and moment.isocalendar().weekday != 1:
        return False
    if kind == "monthly" and moment.day != 1:
        return False

    return get_str(STATE_KEYS[kind], "") != _l3_stamp_value(kind, moment)


def stamp_l3(kind: str, *, now: datetime | None = None) -> None:
    """记录该报表**已成功发送**（只在发送成功后调用，失败可重试）。"""
    if kind not in L3_KINDS:
        raise KeyError(f"未知的 L3 报表类型：{kind!r}")
    set_state(STATE_KEYS[kind], _l3_stamp_value(kind, _now(now)))


# ---------------------------------------------------------------------------
# 组装 + 发送（开关闸门在 _send 里统一收口）
# ---------------------------------------------------------------------------
def _send(layer: str, card: dict[str, Any]) -> bool:
    """发一条卡片：先过**层闸门**，再扇出到各渠道；返回「是否有渠道发出去了」。

    分工（重要，别把两件事混在一起）：

    * **层闸门** ``flags.should_push(layer)`` —— 总开关 / 分项开关 / 静默时段。
      它描述「**这条内容该不该发**」；用户关掉「L1 低电告警」时，
      飞书和 QQ **都不发**。
    * **渠道** —— 描述「**发到哪**」。飞书群走 :func:`transport.post_card`；
      其它渠道（QQ）走 :func:`channels.broadcast_card`，各自 ``enabled()``
      决定是否真的发。

    返回值 = 「**任意一个渠道成功**」：只有飞书配了 webhook、或 QQ 配好了
    凭据与目标，才算发出去 → ``push_*`` 才会打点。否则不打点，
    等用户把渠道配好之后，下个 tick 会把待发的告警补上。
    """
    if not flags.should_push(layer):
        return False

    posted = transport.post_card(card)

    others: list[str] = []
    try:
        from starwatt.notify import channels

        others = channels.broadcast_card(
            card, exclude={transport.WebhookNotifier.name}
        )
    except Exception:  # noqa: BLE001 —— 扇出失败不该影响飞书那条路径的结论
        logger.warning("多渠道扇出失败（已忽略）", exc_info=True)

    return bool(posted or others)


def push_summary(
    body: dict[str, Any],
    room_label: str | None,
    *,
    stale: bool = False,
    now: datetime | None = None,
) -> bool:
    """L2 常规摘要卡（整点自动加 ⏰ 前缀，由 card_builder 决定）。"""
    if not flags.should_push("l2"):
        return False
    card = cards.build_summary_card(
        body, room_label, stale=stale, top_of_hour=cards.is_top_of_hour(_now(now))
    )
    return transport.post_card(card)


def push_low_battery_if_due(*, now: datetime | None = None) -> bool:
    """L1 低电告警（含跃迁判定与打点）。"""
    if not low_battery_due(now=now):
        return False

    latest = RecordRepo.latest()
    remain = coerce_float(latest.remain) if latest else None
    if remain is None:
        return False

    if not _send("l1", cards.build_low_battery_card(remain)):
        return False
    set_state(STATE_KEYS["low_battery"], timeutil.to_stamp(_now(now)))
    return True


def push_violations_if_due(room_id: str, *, now: datetime | None = None) -> bool:
    """违规告警（冷却 + 只推新记录 + 打点）。"""
    rows = violation_rows_to_push(room_id, now=now)
    if not rows:
        return False
    if not _send("violation", cards.build_violation_card(rows)):
        return False
    set_state(STATE_KEYS["violation"], timeutil.to_stamp(_now(now)))
    return True


def push_stale_if_due(*, now: datetime | None = None) -> bool:
    """stale 告警（同一段陈旧期只推一次 + 打点）。"""
    gap = stale_due_gap(now=now)
    if gap is None:
        return False

    last_success = coerce_str(get_str(STATE_KEYS["scrape"], "")) or "—"
    if not _send("stale", cards.build_stale_card(last_success, gap)):
        return False
    set_state(STATE_KEYS["stale"], timeutil.to_stamp(_now(now)))
    return True


def push_offline_if_due(room_id: str) -> bool:
    """电表离线告警（B6 判定 + 驼峰键映射）。"""
    if not room_id:
        return False
    run_status = RunStatusRepo.get(room_id)
    if not is_offline(run_status):
        return False
    return _send("offline", cards.build_offline_card(offline_card_payload(run_status)))


def push_l3_if_due(kind: str, *, now: datetime | None = None) -> bool:
    """L3 报表（日报 / 周报 / 月报）：到点 → 组装 → 发送 → **打点**。

    打点必须在发送成功之后 —— 失败的下一个 tick 还能重试
    （``l3_due`` 用当天/当周/当月的幂等键判断，见 :func:`stamp_l3`）。
    """
    if not l3_due(kind, now=now):
        return False

    from starwatt.notify import reports  # 延迟导入：报表内容层只在这里用到

    summary = reports.build_summary(kind, now=now)
    card = cards.build_l3_card(
        kind,
        summary,
        room_label(),
        title_prefix=reports.L3_TITLES[kind],
        header_template=reports.L3_TEMPLATES[kind],
    )
    if not _send(kind, card):
        return False

    stamp_l3(kind, now=now)
    return True


def room_label() -> str | None:
    """房间显示名（``room_label``，由房间发现时写入）；未设置返回 ``None``。"""
    return coerce_str(get_str("room_label", "")) or None
