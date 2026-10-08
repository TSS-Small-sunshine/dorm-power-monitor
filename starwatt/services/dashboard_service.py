"""仪表盘数据装配 —— Web 与调度器共享（M4 §4.1）。

契约（**冻结**，见 ``tests/regression/fixtures/api.json``）
=========================================================

``GET /api/data?hours=N`` → ::

    {"hours": N, "rows": [{id, read_time, remain, ts}...], "stats": {...}}

``GET /api/live`` → ::

    {"eqprice", "latest_ts", "monthly_breakdown", "monthly_projection",
     "run_status", "scrape_status", "stale", "stats"}

``stats`` 的四个字段（``remain`` / ``hourly_used`` / ``read_time`` /
``daily_avg``）与 ``monthly_breakdown`` 的六个字段都在 fixtures 里逐字段冻结，
``tests/unit/test_dashboard_service.py`` 用同一批向量比对。

两个「隐形规则」（容易写错，都是 legacy 踩出来的）
================================================

1. **``hourly_used`` 要求至少 3500 秒的间隔** —— 10 分钟的抓取周期里，
   相邻两条记录只差 600 秒，直接相减会得到「每小时用量 = 10 分钟用量」的
   假数字。所以要从最新一条往回找**第一条间隔 ≥ 3500 秒**的记录。
   而且**只算下降**（``v >= latest_remain`` 时记 ``None``）—— 充值会让剩余
   电量上升，那不是耗电。
2. **``daily_avg`` 要求跨度 ≥ 1 天**，且用 `(最旧 − 最新) / 跨度的秒数/86400`。
   跨度不足 1 天时返回 ``None``（否则 1 小时的窗口会被外推成「每天 12 度」）。

本模块**只做装配**：算法在 :mod:`starwatt.domain`，取数在 repositories。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from starwatt import timeutil
from starwatt.config_registry import get_int, get_str, raw
from starwatt.db.coerce import coerce_float, coerce_str
from starwatt.db.repositories import (
    DailyElecRepo,
    RecordRepo,
    RunStatusRepo,
)
from starwatt.domain import metrics, pricing, thresholds

logger = logging.getLogger("starwatt.web")

__all__ = [
    "HOURLY_GAP_SEC",
    "history",
    "live",
    "monthly_projection",
    "stats",
]

#: ``hourly_used`` 的最小间隔（秒）—— legacy 的 3500s（≈1 小时）
HOURLY_GAP_SEC = 3500

#: ``/api/data`` 默认窗口
DEFAULT_HOURS = 24

#: 算「日均」用的窗口（7 天）—— 见 :func:`days_remaining` 的说明
DAILY_AVG_HOURS = 168


# ---------------------------------------------------------------------------
# stats（冻结算法）
# ---------------------------------------------------------------------------
def _valid_rows(rows: list[Any]) -> list[tuple[str, float]]:
    """``Record`` 列表 → ``(ts, remain)``，**丢掉 remain 缺失的行**。

    用显式循环而不是推导式，两个好处：

    ① ``coerce_float`` 每行只调一次（推导式里为了过滤得调两次）；
    ② 类型收窄成 ``list[tuple[str, float]]`` —— 调用方的算术不必再判 ``None``。
    """
    valid: list[tuple[str, float]] = []
    for row in rows:
        value = coerce_float(row.remain)
        if value is not None:
            valid.append((str(row.ts), value))
    return valid


def stats(rows: list[Any]) -> dict[str, Any]:
    """4 个 stat-card 值（``rows`` 为 ``Record`` 实体列表，按 ts 升序）。"""
    valid = _valid_rows(rows)

    if not valid:
        return {
            "remain": None,
            "hourly_used": None,
            "read_time": None,
            "daily_avg": None,
        }

    latest_ts, latest_remain = valid[-1]
    read_time = next(
        (coerce_str(row.read_time) for row in reversed(rows) if coerce_str(row.read_time)),
        None,
    )

    # ---- 过去一小时用量：往回找第一条间隔 ≥ 3500s 的记录 ----
    hourly_used: float | None = None
    if len(valid) >= 2:
        latest_dt = timeutil.parse_stamp(latest_ts)
        for ts, value in reversed(valid[:-1]):
            earlier = timeutil.parse_stamp(ts)
            if earlier is None:
                continue
            if latest_dt is None or (latest_dt - earlier).total_seconds() >= HOURLY_GAP_SEC:
                # 只算下降：剩余电量上升 = 充值，不是耗电
                hourly_used = round(value - latest_remain, 2) if value >= latest_remain else None
                break

    # ---- 日均：跨度 ≥ 1 天才算 ----
    daily_avg: float | None = None
    if len(valid) >= 2:
        oldest_dt = timeutil.parse_stamp(valid[0][0])
        newest_dt = timeutil.parse_stamp(latest_ts)
        if oldest_dt is not None and newest_dt is not None:
            span_days = (newest_dt - oldest_dt).total_seconds() / 86400
            if span_days >= 1:
                daily_avg = round((valid[0][1] - latest_remain) / span_days, 2)

    return {
        "remain": round(latest_remain, 2),
        "hourly_used": hourly_used,
        "read_time": read_time,
        "daily_avg": daily_avg,
    }


# ---------------------------------------------------------------------------
# /api/data
# ---------------------------------------------------------------------------
def history(
    hours: int = DEFAULT_HOURS,
    *,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """``/api/data`` 的 payload（``rows`` + ``stats``）。"""
    rows = RecordRepo.query(hours=hours, start_dt=start, end_dt=end)
    return {
        "hours": hours,
        "rows": [
            {
                "id": row.id,
                "ts": row.ts,
                "read_time": row.read_time,
                "remain": round(row.remain, 2) if row.remain is not None else None,
            }
            for row in rows
        ],
        "stats": stats(rows),
    }


# ---------------------------------------------------------------------------
# /api/live
# ---------------------------------------------------------------------------
def eqprice() -> float:
    """单价回退链：配置中心 → 环境变量 ``DORM_EQPRICE`` → 默认 0.5。

    📌 legacy 有**两份**实现（``dorm_power`` / ``web``），Round 42 才对
    齐；重写只留这一处，算法在 :func:`starwatt.domain.pricing.parse_price`。

    ⚠️ 必须读 **``raw()``（meta 里的原始值）而不是 ``get_str()``**：
    注册表的默认值就是 0.5，用 ``get_str`` 会让「用户没设过」和「用户设成
    0.5」无法区分，环境变量覆盖（运维逃生舱）永远不生效。
    """
    import os

    return pricing.parse_price(
        raw("eqprice"),  # None = 用户没设过 → 继续往下找
        os.environ.get("DORM_EQPRICE", ""),
    )


def monthly_projection(
    stats_payload: dict[str, Any],
    *,
    room_id: str | None,
    price: float | None,
    today: date | None = None,
) -> dict[str, Any]:
    """「本月预计电费」明细（冻结公式）。

    ::

        monthly_projection = (used_kwh + avg_daily × days_left) × eqprice

    * ``used_kwh``：本月**累计表码**的首尾差（不是求和！Round 41 修过一个
      把累计值相加导致「日均 7806 度」的 bug）
    * ``avg_daily`` 优先级：本月 ``used_kwh / (days_observed - 1)`` →
      ``stats.daily_avg``（历史窗口）→ ``None``
    * ``days_left = 当月天数 - 今天``
    * 电表重置（首尾差为负）→ 不假装有数据：``used_kwh=None`` 且
      ``days_observed`` 归零
    """
    moment = today or timeutil.today_cst()
    days_in_month = _days_in_month(moment.year, moment.month)
    days_left = max(days_in_month - moment.day, 0)

    zongs = _month_zongs(room_id, moment)
    used_kwh, days_observed = _month_usage(zongs)

    if used_kwh is not None and days_observed > 1:
        avg_daily: float | None = round(used_kwh / (days_observed - 1), 3)
    else:
        avg_daily = coerce_float(stats_payload.get("daily_avg"))

    if price is None or price <= 0 or avg_daily is None:
        return {
            "eqprice": price,
            "used_kwh": round(used_kwh, 2) if used_kwh else None,
            "days_observed": days_observed,
            "days_left": days_left,
            "avg_daily": avg_daily,
            "monthly_projection": None,
        }

    base = used_kwh if used_kwh is not None else 0.0
    projected_kwh = base + avg_daily * days_left
    return {
        "eqprice": price,
        "used_kwh": round(base, 2) if base else None,
        "days_observed": days_observed,
        "days_left": days_left,
        "avg_daily": round(avg_daily, 3),
        "monthly_projection": round(projected_kwh * price, 2),
    }


def _month_zongs(room_id: str | None, moment: date) -> list[tuple[date, float]]:
    """本月（截至 ``moment``）的 ``(日期, 累计表码)`` 列表（升序）。"""
    if not room_id:
        return []

    rows: list[tuple[date, float]] = []
    for row in DailyElecRepo.recent(room_id, days=31):
        raw_dt = coerce_str(row.dt)
        if not raw_dt:
            continue
        try:
            day = date.fromisoformat(raw_dt[:10])
        except ValueError:
            continue
        if (day.year, day.month) != (moment.year, moment.month) or day.day > moment.day:
            continue
        value = coerce_float(row.zong_eq)
        if value is None or value <= 0:
            continue
        rows.append((day, value))
    return rows


def _month_usage(zongs: list[tuple[date, float]]) -> tuple[float | None, int]:
    """``(本月用量, 观测天数)`` —— 首尾差；换表（负差）时退回「无数据」。"""
    if len(zongs) >= 2:
        delta = round(zongs[-1][1] - zongs[0][1], 3)
        if delta < 0:
            # 换表 / 计数器重置：不编数字，让调用方回落到历史均值
            return None, 0
        return delta, len(zongs)
    if len(zongs) == 1:
        return None, 1
    return None, 0


def _days_in_month(year: int, month: int) -> int:
    """当月天数（不 import calendar 也能算：下月 1 号减一天）。"""
    first = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return (next_month - first).days


def live(*, today: date | None = None) -> dict[str, Any]:
    """``/api/live`` 的 payload（最新值 + 电表快照 + 本月预计）。"""
    room_id = coerce_str(get_str("last_room_id", ""))
    price = eqprice()

    rows = RecordRepo.query(hours=DEFAULT_HOURS)
    stat_payload = stats(rows)
    latest = rows[-1] if rows else None

    breakdown = monthly_projection(
        stat_payload, room_id=room_id, price=price, today=today
    )

    run_status: dict[str, Any] | None = None
    if room_id:
        row = RunStatusRepo.get(room_id)
        if row is not None:
            run_status = {
                "run_status": row.run_status,
                "vol": row.vol,
                "cur": row.cur,
                "yggl": row.yggl,
                "update_dt": row.update_dt,
            }

    gap = _stale_gap_seconds()
    return {
        "eqprice": price,
        "latest_ts": latest.ts if latest else None,
        "monthly_projection": breakdown["monthly_projection"],
        "monthly_breakdown": breakdown,
        "run_status": run_status,
        "scrape_status": get_str("last_scrape_status", "") or None,
        "stale": thresholds.is_stale(gap, get_int("stale_hours", 2)),
        "stats": stat_payload,
    }


def _stale_gap_seconds() -> float | None:
    """距上次成功抓取的秒数（从未成功过 → ``None``）。"""
    last = timeutil.parse_stamp(get_str("last_scrape_at", ""))
    if last is None:
        return None
    return (timeutil.now_cst() - last).total_seconds()


def days_remaining() -> float | None:
    """剩余电量还能用几天。

    📌 用 **7 天窗口**（``/api/data?hours=168``）算日均：24 小时窗口里
    跨度往往不足 1 天，日均恒为 ``None``，卡片就只能显示「—」。
    """
    latest = RecordRepo.latest()
    if latest is None:
        return None
    payload = stats(RecordRepo.query(hours=DAILY_AVG_HOURS))
    return metrics.days_remaining(coerce_float(latest.remain), payload["daily_avg"])


def cleanup_before(days: int) -> int:
    """删除 ``days`` 天前的抓取记录（数据保留策略）。"""
    cutoff = timeutil.to_stamp(timeutil.now_cst() - timedelta(days=days))
    removed = RecordRepo.delete_before(cutoff)
    if removed:
        logger.info("数据保留：清理 %d 条 %s 之前的记录", removed, cutoff)
    return removed
