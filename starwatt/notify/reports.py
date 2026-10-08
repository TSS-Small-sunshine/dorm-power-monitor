"""L3 报表内容 —— 日报 / 周报 / 月报（M3）。

为什么自成一个文件
==================

``REWRITE_PLAN`` §1.2 的 ``notify/`` 树里没有 ``reports.py``；但 L3 报表是
**「读库 → 计算 → 组装摘要」** 三件事的组合，塞进 ``policies.py``（门控层）
会让它变成大杂烩。这里按职责切开：

==============  ==========================================================
模块             职责
==============  ==========================================================
``policies``     **什么时候发**（``l3_due`` / ``stamp_l3`` / 开关 / 静默）
``reports``      本文件：**发什么**（三份摘要的字段与算法）
``card_builder`` 长什么样（``build_l3_card``）
==============  ==========================================================

三份摘要的字段（与 legacy ``compute_*_summary`` 一致）
=====================================================

======  ================================================================
kind     字段
======  ================================================================
daily    ``today_kwh`` / ``yesterday_kwh`` / ``avg7_kwh`` /
         ``month_total_kwh`` / ``violations_today``
weekly   ``last_week_kwh`` / ``month_total_kwh`` / ``recharge_total`` /
         ``violations_week``
monthly  ``last_month_kwh`` / ``this_month_kwh`` / ``recharge_total`` /
         ``violations_total``
======  ================================================================

算法全在 :mod:`starwatt.domain.metrics`（纯函数）；本模块只负责**取数**与
**拼装**。缺数据一律记 ``0.0`` / ``0``，**绝不抛异常**（报表不能因为某张表
为空就让调度器崩掉）。

📌 与 legacy 的差异：legacy 的 ``compute_*_summary(room_id, remain, dt)``
要调用方把「当前剩余电量」传进来（它的数据源是刚抓到的 F1 响应）；重写直接
读 ``records`` 表最新一行 —— 报表可能在任何时刻被触发，从库里取比依赖调用方
传值更稳。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from starwatt import timeutil
from starwatt.config_registry import get_str
from starwatt.db.coerce import coerce_float, coerce_str
from starwatt.db.repositories import DailyElecRepo, PayRepo, RecordRepo, ViolationRepo
from starwatt.domain import metrics

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "KINDS",
    "L3_TEMPLATES",
    "L3_TITLES",
    "build_summary",
    "daily_summary",
    "monthly_summary",
    "weekly_summary",
]

#: 三种报表（顺序即 UI / 日志顺序）
KINDS: tuple[str, ...] = ("daily", "weekly", "monthly")

#: 报表标题的 emoji 前缀（唯一真相：标题与底色永远一致）
L3_TITLES: dict[str, str] = {
    "daily": "📊 每日用电报告",
    "weekly": "📈 每周用电报告",
    "monthly": "📅 每月用电报告",
}

#: 报表 header 底色
L3_TEMPLATES: dict[str, str] = {
    "daily": "blue",
    "weekly": "green",
    "monthly": "purple",
}



def _moment(now: datetime | None = None) -> datetime:
    return now or timeutil.now_cst()


def _room_id() -> str:
    """报表的房间号 = ``last_room_id``（抓取时发布；未抓过则为空）。"""
    try:
        return coerce_str(get_str("last_room_id", "")) or ""
    except Exception:  # noqa: BLE001 —— 首次启动时 meta 表可能还不存在
        logger.warning("读取 last_room_id 失败 —— 本次报表按「无房间」处理")
        return ""


def _latest_record() -> tuple[float | None, str | None]:
    """最新一条 F1 快照的 ``(剩余电量, 抄表时间)``；读不到返回 ``(None, None)``。"""
    try:
        latest = RecordRepo.latest()
    except Exception:  # noqa: BLE001
        logger.warning("读取 records 失败 —— 报表不含剩余电量")
        return None, None
    if latest is None:
        return None, None
    return coerce_float(latest.remain), coerce_str(latest.read_time)


def _daily_rows(days: int) -> list[dict[str, Any]]:
    """``daily_elec`` 行（转成 dict 供纯函数使用）；读不到返回空列表。"""
    try:
        return [row.as_dict() for row in DailyElecRepo.recent(_room_id(), days=days)]
    except Exception:  # noqa: BLE001
        logger.warning("读取 daily_elec 失败 —— 报表按无数据计算")
        return []


def _violation_rows(room_id: str, days: int) -> list[dict[str, Any]]:
    """近 ``days`` 天的违规行（dict）；读不到返回空列表。"""
    if not room_id:
        return []
    try:
        return [v.as_dict() for v in ViolationRepo.recent(room_id, days=days)]
    except Exception:  # noqa: BLE001
        logger.warning("读取 violations 失败 —— 违规数按 0 计")
        return []


def _violation_count(room_id: str, days: int) -> int:
    """近 ``days`` 天的违规条数；读不到返回 0。"""
    return len(_violation_rows(room_id, days))


def _recharge_total(room_id: str, days: int) -> float:
    """近 ``days`` 天的**充值**合计（不含退费）；读不到返回 0.0。"""
    if not room_id:
        return 0.0
    try:
        rows = [p.as_dict() for p in PayRepo.recent(room_id, days=days)]
    except Exception:  # noqa: BLE001
        logger.warning("读取 pay_history 失败 —— 充值额按 0 计")
        return 0.0
    return metrics.sum_recharges(rows)


def _month_prefix(moment: datetime) -> str:
    return moment.strftime("%Y-%m")


def _month_total(usable: list[dict[str, Any]], month_prefix: str) -> float:
    """本月累计表码（取本月最后一行的累积值；没有则 0）。"""
    rows = [r for r in usable if str(r.get("dt") or "").startswith(month_prefix)]
    if not rows:
        return 0.0
    return metrics.cumulative_of(rows[-1]) or 0.0


def daily_summary(*, now: datetime | None = None) -> dict[str, Any]:
    """日报摘要：今日 / 昨日 / 7 日均值 / 本月累计 / 今日违规数。"""
    room_id = _room_id()
    moment = _moment(now)
    month_prefix = _month_prefix(moment)

    rows = _daily_rows(7) if room_id else []
    usable = metrics.sort_usable(rows)
    remain, dt = _latest_record()

    violations_today = 0
    if room_id:
        violations_today = sum(
            1
            for v in _violation_rows(room_id, 1)
            if str(v.get("dt") or "").startswith(month_prefix)
        )

    return {
        "kind": "daily",
        "remain": remain,
        "dt": dt,
        "today_kwh": metrics.step_delta(usable),
        "yesterday_kwh": metrics.step_delta(usable, back=2),
        "avg7_kwh": metrics.avg_daily_from_cumulative(rows) or 0.0,
        "month_total_kwh": _month_total(usable, month_prefix),
        "violations_today": violations_today,
    }


def weekly_summary(*, now: datetime | None = None) -> dict[str, Any]:
    """周报摘要：上周累计 / 本月累计 / 近 30 天充值 / 近 7 天违规数。"""
    room_id = _room_id()
    moment = _moment(now)
    month_prefix = _month_prefix(moment)

    rows = _daily_rows(30) if room_id else []
    usable = metrics.sort_usable(rows)
    remain, dt = _latest_record()

    recharge_total = 0.0
    violations_week = 0
    if room_id:
        recharge_total = _recharge_total(room_id, 30)
        violations_week = _violation_count(room_id, 7)

    # 「上周」= 最近 8 行里的首尾差（legacy 的同一算法）
    window = usable[-8:] if len(usable) >= 8 else usable
    return {
        "kind": "weekly",
        "remain": remain,
        "dt": dt,
        "last_week_kwh": metrics.span_delta(window),
        "month_total_kwh": _month_total(usable, month_prefix),
        "recharge_total": recharge_total,
        "violations_week": violations_week,
    }


def monthly_summary(*, now: datetime | None = None) -> dict[str, Any]:
    """月报摘要：上月累计 / 本月累计 / 近 60 天充值 / 近 60 天违规数。"""
    room_id = _room_id()
    moment = _moment(now)
    month_prefix = _month_prefix(moment)

    rows = _daily_rows(60) if room_id else []
    usable = metrics.sort_usable(rows)
    remain, dt = _latest_record()

    last_month_rows = [
        r for r in usable if not str(r.get("dt") or "").startswith(month_prefix)
    ]
    this_month_rows = [
        r for r in usable if str(r.get("dt") or "").startswith(month_prefix)
    ]
    this_month = metrics.span_delta(this_month_rows)
    if this_month == 0.0 and this_month_rows:
        # 本月只有一行 → 用它的累积值兜底（legacy 的同一处兜底）
        this_month = metrics.cumulative_of(this_month_rows[-1]) or 0.0

    recharge_total = 0.0
    violations_total = 0
    if room_id:
        recharge_total = _recharge_total(room_id, 60)
        violations_total = _violation_count(room_id, 60)

    return {
        "kind": "monthly",
        "remain": remain,
        "dt": dt,
        "last_month_kwh": metrics.span_delta(last_month_rows),
        "this_month_kwh": this_month,
        "recharge_total": recharge_total,
        "violations_total": violations_total,
    }


def build_summary(kind: str, *, now: datetime | None = None) -> dict[str, Any]:
    """按 ``kind`` 分发（``daily`` / ``weekly`` / ``monthly``）。"""
    if kind == "daily":
        return daily_summary(now=now)
    if kind == "weekly":
        return weekly_summary(now=now)
    if kind == "monthly":
        return monthly_summary(now=now)
    raise KeyError(f"未知的报表类型：{kind!r}（可用：{list(KINDS)}）")
