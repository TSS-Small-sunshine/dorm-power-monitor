"""用电指标计算 —— **纯函数**（零 IO，见 ``starwatt/domain/__init__.py``）。

三个算法，全部来自 legacy（``dorm_power.py`` / ``web.py``），行为逐条对齐。

1. 日均用电（``avg_daily_from_cumulative``）
==========================================

``daily_elec.total_eq`` 是**累积表码**（单调递增），日用量 = 相邻两行之差。

两条过滤规则（Round 22 的教训）：

* ``delta < 0`` —— 电表重置 / 换表：跳过，**并且不更新 prev**
  （否则重置前后的差值会被算成一个巨大的负数/正数）
* ``delta > max_daily_kwh``（默认 50）—— 学校后台改数 / 重装造成的跳变：
  跳过。一个这样的 delta 就能把日均拉到 200+，让「还能用几天」永远显示 0

2. 剩余天数（``days_remaining``）
===============================

* ``remain`` 为 ``None`` → ``None``
* ``remain < 0`` → ``0.0``（已经欠费）
* 日均缺失 / ``<= 0`` → ``None``
  （**不是** ``inf`` —— 卡片上显示「—」比显示「∞ 天」诚实）

3. 累计差值（``step_delta`` / ``span_delta``）
===========================================

L3 报表要「今日 / 昨日 / 上周 / 本月」四个数字，本质都是累计表码的差：

* ``step_delta(rows, back=1)`` —— 第 ``back`` 个相邻差值（今日 / 昨日）
* ``span_delta(rows)`` —— 最后一行 − 第一行（区间累计）

⚠️ 本模块**不 import** ``starwatt.db.coerce``（R1 禁止）—— 所以自带一个
极小的 ``_as_float``。这是有意的取舍：让 domain 保持「零依赖」比复用
一个 3 行函数更重要。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "MAX_DAILY_KWH",
    "avg_daily_from_cumulative",
    "daily_usage_series",
    "days_remaining",
    "sort_usable",
    "span_delta",
    "step_delta",
    "sum_recharges",
]

#: 单日用量上限（度）—— 超过它几乎一定是上游事件，不是真实消耗
MAX_DAILY_KWH = 50.0

#: 累积值的候选字段名（``total_eq`` 优先，``zong_eq`` 兜底）
_CUMULATIVE_KEYS = ("total_eq", "zong_eq")


def _as_float(value: Any) -> float | None:
    """转 float；``None`` / 空串 / 不可解析 → ``None``（**不抛**）。"""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cumulative_of(row: dict[str, Any]) -> float | None:
    """取一行的累积表码（``total_eq`` → ``zong_eq``）。"""
    for key in _CUMULATIVE_KEYS:
        value = _as_float(row.get(key))
        if value is not None:
            return value
    return None


def sort_usable(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """过滤出「有 dt 且有累积值」的行，并按 ``dt`` 升序。

    legacy 在三个报表函数里各写了一遍这段逻辑（还各自 ``try/except``），
    重写收敛成一处。
    """
    usable = [r for r in rows if r.get("dt") and cumulative_of(r) is not None]
    return sorted(usable, key=lambda r: str(r["dt"]))


def avg_daily_from_cumulative(
    rows: list[dict[str, Any]], max_daily_kwh: float = MAX_DAILY_KWH
) -> float | None:
    """把累积表码序列转成**日均用电**（度/天）。

    Returns:
        平均日用量；有效 delta 少于 1 个时返回 ``None``。
    """
    cleaned = sort_usable(rows)
    if not cleaned:
        return None

    deltas: list[float] = []
    prev: float | None = None
    for row in cleaned:
        current = cumulative_of(row)
        if prev is not None and current is not None:
            delta = current - prev
            if delta < 0:
                pass  # 重置：跳过，且**不更新 prev**（见模块 docstring）
            elif delta <= max_daily_kwh:
                deltas.append(delta)
        prev = current

    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def daily_usage_series(
    rows: list[dict[str, Any]], max_daily_kwh: float = MAX_DAILY_KWH
) -> list[tuple[str, float]]:
    """累积表码序列 → **每日用量**序列（``[(dt, 度数), ...]``，供趋势图用）。

    与 :func:`avg_daily_from_cumulative` 用**同一套 delta 规则**，否则会出现
    「图上某天 8 度、但日均显示 6 度」这种自相矛盾（用户一定会发现）：

    * ``delta < 0`` —— 换表 / 充值重置：**跳过该点**，且不更新 ``prev``
      （下一段从新的基准重新开始算）
    * ``delta > max_daily_kwh`` —— 上游事件（补录、跨月重置）：跳过
    * 首行没有前一行 → 不产生点（它只能作为下一行的基准）

    Returns:
        升序的 ``(日期, 用量)``；有效点少于 1 个时返回空列表。
    """
    cleaned = sort_usable(rows)

    series: list[tuple[str, float]] = []
    prev: float | None = None
    for row in cleaned:
        current = cumulative_of(row)
        if prev is not None and current is not None:
            delta = current - prev
            if delta < 0:
                pass  # 重置：跳过，且不更新 prev（与 avg_daily 一致）
            elif delta <= max_daily_kwh:
                series.append((str(row["dt"]), delta))
        prev = current
    return series


def step_delta(rows: list[dict[str, Any]], back: int = 1) -> float:
    """第 ``back`` 个**相邻差值**；数据不足 → ``0.0``。

    ``back=1`` → 最新一天的用量（``rows[-1] - rows[-2]``）
    ``back=2`` → 再往前一天（``rows[-2] - rows[-3]``）

    📌 语义是「第 n 个台阶」，不是「与最后一行的跨度」—— 日报的「昨日」
    是 ``back=2``，正是这个含义。
    """
    cleaned = sort_usable(rows)
    if back < 1 or len(cleaned) < back + 1:
        return 0.0
    newer = cumulative_of(cleaned[-back])
    older = cumulative_of(cleaned[-back - 1])
    if newer is None or older is None:
        return 0.0
    return newer - older


def span_delta(rows: list[dict[str, Any]]) -> float:
    """区间累计（最后一行 − 第一行）；数据不足 → ``0.0``。"""
    cleaned = sort_usable(rows)
    if len(cleaned) < 2:
        return 0.0
    first = cumulative_of(cleaned[0])
    last = cumulative_of(cleaned[-1])
    if first is None or last is None:
        return 0.0
    return last - first


def days_remaining(
    remain_kwh: float | None, avg_daily_kwh: float | None
) -> float | None:
    """剩余电量还能用几天（见模块 docstring 的三条规则）。"""
    if remain_kwh is None:
        return None
    if remain_kwh < 0:
        return 0.0
    if avg_daily_kwh is None or avg_daily_kwh <= 0:
        return None
    return remain_kwh / avg_daily_kwh


def sum_recharges(pay_rows: list[dict[str, Any]]) -> float:
    """缴费合计（**只算充值，不含退费**）。

    legacy 判断的是 ``fee_type`` 里有没有「退」字 —— 保留这个宽松判定
    （门户的取值是「电费」/「电费退费」这类中文标签）。
    """
    total = 0.0
    for row in pay_rows:
        fee_type = str(row.get("fee_type") or "").strip()
        if "退" in fee_type:
            continue
        money = _as_float(row.get("money"))
        if money is not None:
            total += money
    return total
