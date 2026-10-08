"""``starwatt.domain.metrics`` —— 纯计算（零 IO，可 100% 单测）。

这些算法决定用户看到的「今日用电」「还能用几天」，错了不会报错、只会
悄悄给出一个假数字 —— 所以边界必须钉死。
"""
from __future__ import annotations

import pytest

from starwatt.domain import metrics


def _row(dt: str, total: float | None = None, zong: float | None = None) -> dict:
    return {"dt": dt, "total_eq": total, "zong_eq": zong}


class TestCumulativeOf:
    def test_prefers_total_eq(self) -> None:
        assert metrics.cumulative_of(_row("2026-10-06", 12.5, 99.0)) == 12.5

    def test_falls_back_to_zong_eq(self) -> None:
        assert metrics.cumulative_of(_row("2026-10-06", None, 99.0)) == 99.0

    @pytest.mark.parametrize("value", [None, "", "abc", True])
    def test_unusable_values(self, value) -> None:
        assert metrics.cumulative_of({"dt": "x", "total_eq": value}) is None

    def test_string_numbers_are_coerced(self) -> None:
        assert metrics.cumulative_of(_row("x", "12.34")) == 12.34


class TestSortUsable:
    def test_drops_rows_without_dt_or_cumulative(self) -> None:
        rows = [
            _row("2026-10-06", 10.0),
            {"total_eq": 11.0},  # 没有 dt
            _row("2026-10-05", None),  # 没有累积值
        ]
        assert [r["dt"] for r in metrics.sort_usable(rows)] == ["2026-10-06"]

    def test_sorts_ascending(self) -> None:
        rows = [_row("2026-10-06", 10.0), _row("2026-10-04", 8.0), _row("2026-10-05", 9.0)]
        assert [r["dt"] for r in metrics.sort_usable(rows)] == [
            "2026-10-04",
            "2026-10-05",
            "2026-10-06",
        ]


class TestAvgDaily:
    def test_empty(self) -> None:
        assert metrics.avg_daily_from_cumulative([]) is None

    def test_single_row_has_no_delta(self) -> None:
        assert metrics.avg_daily_from_cumulative([_row("2026-10-06", 10.0)]) is None

    def test_two_rows(self) -> None:
        rows = [_row("2026-10-05", 10.0), _row("2026-10-06", 12.5)]
        assert metrics.avg_daily_from_cumulative(rows) == pytest.approx(2.5)

    def test_averages_multiple_deltas(self) -> None:
        rows = [_row("2026-10-04", 0.0), _row("2026-10-05", 2.0), _row("2026-10-06", 6.0)]
        assert metrics.avg_daily_from_cumulative(rows) == pytest.approx(3.0)

    def test_meter_reset_is_skipped_and_does_not_poison_next_delta(self) -> None:
        """🔑 重置后必须**不更新 prev**，否则重置前后的差值会算成一个巨大的负数。"""
        rows = [
            _row("2026-10-04", 100.0),
            _row("2026-10-05", 1.0),  # 重置：delta = -99 → 跳过
            _row("2026-10-06", 3.0),  # delta = 2（相对重置后的 1.0）
        ]
        assert metrics.avg_daily_from_cumulative(rows) == pytest.approx(2.0)

    def test_outlier_is_skipped(self) -> None:
        """学校后台改数造成的跳变不能把日均拉到 200+。"""
        rows = [_row("2026-10-04", 0.0), _row("2026-10-05", 500.0), _row("2026-10-06", 502.0)]
        assert metrics.avg_daily_from_cumulative(rows) == pytest.approx(2.0)

    def test_all_outliers_returns_none(self) -> None:
        rows = [_row("2026-10-04", 0.0), _row("2026-10-05", 500.0)]
        assert metrics.avg_daily_from_cumulative(rows) is None

    def test_threshold_is_configurable(self) -> None:
        rows = [_row("2026-10-04", 0.0), _row("2026-10-05", 500.0)]
        assert metrics.avg_daily_from_cumulative(rows, max_daily_kwh=1000) == 500.0

    def test_uses_zong_eq_when_total_eq_missing(self) -> None:
        rows = [_row("2026-10-05", None, 10.0), _row("2026-10-06", None, 12.0)]
        assert metrics.avg_daily_from_cumulative(rows) == pytest.approx(2.0)


class TestDeltas:
    def test_step_delta_default_is_last_pair(self) -> None:
        rows = [_row("2026-10-04", 1.0), _row("2026-10-05", 3.0), _row("2026-10-06", 6.0)]
        assert metrics.step_delta(rows) == pytest.approx(3.0)

    def test_step_delta_back_two(self) -> None:
        rows = [_row("2026-10-04", 1.0), _row("2026-10-05", 3.0), _row("2026-10-06", 6.0)]
        assert metrics.step_delta(rows, back=2) == pytest.approx(2.0)

    @pytest.mark.parametrize("rows", [[], [_row("2026-10-06", 1.0)]])
    def test_step_delta_needs_two_rows(self, rows) -> None:
        assert metrics.step_delta(rows) == 0.0

    def test_step_delta_rejects_bad_back(self) -> None:
        rows = [_row("2026-10-05", 1.0), _row("2026-10-06", 2.0)]
        assert metrics.step_delta(rows, back=0) == 0.0

    def test_span_delta(self) -> None:
        rows = [_row("2026-10-01", 10.0), _row("2026-10-03", 14.0), _row("2026-10-06", 20.0)]
        assert metrics.span_delta(rows) == pytest.approx(10.0)

    def test_span_delta_needs_two_rows(self) -> None:
        assert metrics.span_delta([_row("2026-10-06", 1.0)]) == 0.0
        assert metrics.span_delta([]) == 0.0


class TestDaysRemaining:
    def test_normal(self) -> None:
        assert metrics.days_remaining(30.0, 2.0) == pytest.approx(15.0)

    def test_missing_remain(self) -> None:
        assert metrics.days_remaining(None, 2.0) is None

    def test_negative_remain_is_zero(self) -> None:
        assert metrics.days_remaining(-1.0, 2.0) == 0.0

    @pytest.mark.parametrize("avg", [None, 0.0, -1.0])
    def test_no_meaningful_average(self, avg) -> None:
        """显示「—」而不是「∞ 天」。"""
        assert metrics.days_remaining(30.0, avg) is None


class TestSumRecharges:
    def test_sums_money(self) -> None:
        rows = [{"fee_type": "电费", "money": 50.0}, {"fee_type": "电费", "money": "25.5"}]
        assert metrics.sum_recharges(rows) == pytest.approx(75.5)

    def test_refunds_are_excluded(self) -> None:
        rows = [{"fee_type": "电费", "money": 50.0}, {"fee_type": "电费退费", "money": -30.0}]
        assert metrics.sum_recharges(rows) == pytest.approx(50.0)

    def test_missing_money_is_skipped(self) -> None:
        assert metrics.sum_recharges([{"fee_type": "电费", "money": None}]) == 0.0

    def test_empty(self) -> None:
        assert metrics.sum_recharges([]) == 0.0
