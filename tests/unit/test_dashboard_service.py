"""``starwatt.services.dashboard_service`` —— 冻结的 API 契约与算法向量。

``tests/regression/fixtures/{api,algorithms}.json`` 是在 **legacy 代码上真实
运行**产出的（M0 步骤 0.4），所以这些断言是「重写没有偷偷改变 API」的唯一证据。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from starwatt.config_registry import set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import DailyElecRepo, RecordRepo, RunStatusRepo
from starwatt.services import dashboard_service as ds

FIXTURES = Path(__file__).resolve().parents[1] / "regression" / "fixtures"

ROOM = "room-1"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


#: 与 ``generate_fixtures.snap_algorithms`` 的构造方式**完全一致**
STATS_CASES: dict[str, list[tuple[str, float | None, str | None]]] = {
    "empty": [],
    "single": [("2026-10-06 11:00:00", 10.5, "2026-10-06 11:00:00")],
    "two_1h_apart": [
        ("2026-10-06 11:00:00", 11.0, "2026-10-06 11:00:00"),
        ("2026-10-06 12:00:00", 10.5, "2026-10-06 12:00:00"),
    ],
    "two_30min_apart": [
        ("2026-10-06 11:30:00", 11.0, "2026-10-06 11:30:00"),
        ("2026-10-06 12:00:00", 10.5, "2026-10-06 12:00:00"),
    ],
    "seven_days": [
        ("2026-09-29 12:00:00", 20.0, "2026-09-29 12:00:00"),
        ("2026-10-06 12:00:00", 13.0, "2026-10-06 12:00:00"),
    ],
    "null_remain": [("2026-10-06 12:00:00", None, None)],
    "remain_increase": [
        ("2026-10-06 11:00:00", 40.0, "2026-10-06 11:00:00"),
        ("2026-10-06 12:00:00", 50.0, "2026-10-06 12:00:00"),
    ],
    "no_read_time": [
        ("2026-10-06 11:00:00", 11.0, None),
        ("2026-10-06 12:00:00", 10.5, None),
    ],
}


def _records(case: str) -> list[Record]:
    return [
        Record(ts=ts, read_time=read_time, remain=remain)
        for ts, remain, read_time in STATS_CASES[case]
    ]


# ===========================================================================
# _stats —— 8 个向量逐字段一致
# ===========================================================================
class TestStatsContract:
    @pytest.mark.parametrize("case", sorted(STATS_CASES))
    def test_matches_fixture(self, case) -> None:
        expected = _fixture("algorithms")["web._stats"][case]
        got = ds.stats(_records(case))
        for key, value in expected.items():
            assert got[key] == value, f"{case}.{key}"

    def test_hourly_used_requires_3500s_gap(self) -> None:
        """🔑 30 分钟的间隔不算「每小时用量」—— 否则会把 10 分钟的差值当成 1 小时。"""
        assert ds.stats(_records("two_1h_apart"))["hourly_used"] == 0.5
        assert ds.stats(_records("two_30min_apart"))["hourly_used"] is None

    def test_recharge_is_not_consumption(self) -> None:
        assert ds.stats(_records("remain_increase"))["hourly_used"] is None

    def test_daily_avg_is_never_negative(self) -> None:
        """充值让 ``remain`` 上升 —— 日均必须是 ``None``，不能是负数。

        📌 真实数据实测：52 度 → 充值 → 152 度，7 天窗口算出 **-15.57**。
        legacy 的 ``remain_increase`` 向量对日均返回 ``None``（见
        ``fixtures/algorithms.json``），重写漏了这个判断 —— ``hourly_used``
        一直有，``daily_avg`` 没有。
        """
        rows = [
            Record(ts="2026-09-29 12:00:00", read_time=None, remain=52.0),
            Record(ts="2026-10-06 12:00:00", read_time=None, remain=152.0),
        ]
        assert ds.stats(rows)["daily_avg"] is None
        # 净下降时仍然照常计算（别把正常路径一起关掉）
        rows[1] = Record(ts="2026-10-06 12:00:00", read_time=None, remain=45.0)
        assert ds.stats(rows)["daily_avg"] == 1.0

    def test_daily_avg_needs_a_full_day(self) -> None:
        assert ds.stats(_records("two_1h_apart"))["daily_avg"] is None
        assert ds.stats(_records("seven_days"))["daily_avg"] == 1.0

    def test_empty(self) -> None:
        assert ds.stats([]) == {
            "remain": None,
            "hourly_used": None,
            "read_time": None,
            "daily_avg": None,
        }

    def test_stats_keys_match_fixture(self) -> None:
        expected = sorted(_fixture("api")["/api/data?hours=24"]["stats_keys"])
        assert sorted(ds.stats([])) == expected


# ===========================================================================
# /api/data
# ===========================================================================
class TestHistory:
    def test_payload_shape(self, tmp_db) -> None:
        expected = _fixture("api")["/api/data?hours=24"]
        payload = ds.history(hours=24)

        assert sorted(payload) == sorted(expected["keys"])
        assert payload["hours"] == 24
        assert payload["rows"] == []
        assert sorted(payload["stats"]) == sorted(expected["stats_keys"])

    def test_rows_shape(self, tmp_db, frozen_now) -> None:
        expected = _fixture("api")["/api/data?hours=24"]
        RecordRepo.insert(
            Record(ts="2026-10-06 11:00:00", read_time="2026-10-06 10:59:00", remain=42.5)
        )
        row = ds.history(hours=24)["rows"][0]
        assert sorted(row) == sorted(expected["row_keys"])
        assert row["remain"] == 42.5 and row["read_time"] == "2026-10-06 10:59:00"

    def test_default_hours(self, tmp_db) -> None:
        assert ds.history()["hours"] == ds.DEFAULT_HOURS == 24


# ===========================================================================
# monthly_projection —— 3 个向量
# ===========================================================================
class TestMonthlyProjectionContract:
    @pytest.mark.parametrize("case", ["empty_stats", "no_daily_avg", "normal"])
    def test_matches_fixture(self, tmp_db, case) -> None:
        expected = _fixture("algorithms")["web._compute_monthly_projection"][case]
        stats_payload = {"daily_avg": 2.0} if case == "normal" else {"daily_avg": None}

        got = ds.monthly_projection(
            stats_payload, room_id=None, price=0.5, today=date(2026, 10, 7)
        )
        for key, value in expected.items():
            assert got[key] == value, f"{case}.{key}"

    def test_formula(self, tmp_db) -> None:
        """``(used_kwh + avg_daily × days_left) × eqprice``。"""
        got = ds.monthly_projection(
            {"daily_avg": 2.0}, room_id=None, price=0.5, today=date(2026, 10, 7)
        )
        assert got["days_left"] == 24  # 10 月 31 天 − 7 号
        assert got["monthly_projection"] == 24.0  # (0 + 2.0 × 24) × 0.5

    def test_month_to_date_average_wins(self, tmp_db) -> None:
        """本月有 2+ 天数据时，用「首尾差 / (天数−1)」而不是历史窗口均值。"""
        DailyElecRepo.upsert_many(
            ROOM,
            [
                {"roomId": ROOM, "dt": "2026-10-01", "zong_eq": 100.0},
                {"roomId": ROOM, "dt": "2026-10-06", "zong_eq": 110.0},
            ],
        )
        got = ds.monthly_projection(
            {"daily_avg": 99.0}, room_id=ROOM, price=0.5, today=date(2026, 10, 7)
        )
        assert got["used_kwh"] == 10.0
        assert got["days_observed"] == 2
        assert got["avg_daily"] == 10.0  # 10 / (2−1)，而不是 stats 的 99
        assert got["monthly_projection"] == round((10.0 + 10.0 * 24) * 0.5, 2)

    def test_meter_reset_falls_back_to_history(self, tmp_db) -> None:
        """首尾差为负（换表）→ 不编数字，用历史均值兜底。"""
        DailyElecRepo.upsert_many(
            ROOM,
            [
                {"roomId": ROOM, "dt": "2026-10-01", "zong_eq": 100.0},
                {"roomId": ROOM, "dt": "2026-10-06", "zong_eq": 5.0},
            ],
        )
        got = ds.monthly_projection(
            {"daily_avg": 2.0}, room_id=ROOM, price=0.5, today=date(2026, 10, 7)
        )
        assert got["used_kwh"] is None and got["days_observed"] == 0
        assert got["avg_daily"] == 2.0

    def test_single_day_has_no_delta(self, tmp_db) -> None:
        DailyElecRepo.upsert_many(
            ROOM, [{"roomId": ROOM, "dt": "2026-10-06", "zong_eq": 100.0}]
        )
        got = ds.monthly_projection(
            {"daily_avg": None}, room_id=ROOM, price=0.5, today=date(2026, 10, 7)
        )
        assert got["days_observed"] == 1 and got["used_kwh"] is None

    def test_no_price_means_no_projection(self, tmp_db) -> None:
        got = ds.monthly_projection(
            {"daily_avg": 2.0}, room_id=None, price=None, today=date(2026, 10, 7)
        )
        assert got["monthly_projection"] is None

    def test_days_in_month_helper(self) -> None:
        assert ds._days_in_month(2026, 10) == 31
        assert ds._days_in_month(2026, 12) == 31  # 跨年分支
        assert ds._days_in_month(2028, 2) == 29


# ===========================================================================
# /api/live
# ===========================================================================
class TestLive:
    def test_payload_keys_match_fixture(self, tmp_db) -> None:
        expected = _fixture("api")["/api/live"]
        payload = ds.live(today=date(2026, 10, 7))

        assert sorted(payload) == sorted(expected["keys"])
        assert sorted(payload["stats"]) == sorted(expected["stats_keys"])
        assert sorted(payload["monthly_breakdown"]) == sorted(expected["monthly_keys"])

    def test_run_status_shape(self, tmp_db) -> None:
        expected = _fixture("api")["/api/live"]
        set_state("last_room_id", ROOM)
        RunStatusRepo.upsert(
            ROOM, {"runStatus": "在线", "vol": 220.1, "cur": 0.45, "yggl": 12.3}
        )
        payload = ds.live(today=date(2026, 10, 7))
        assert sorted(payload["run_status"]) == sorted(expected["run_status_keys"])

    def test_empty_state(self, tmp_db) -> None:
        payload = ds.live(today=date(2026, 10, 7))
        assert payload["latest_ts"] is None
        assert payload["run_status"] is None
        assert payload["scrape_status"] is None
        assert payload["stale"] is False
        assert payload["eqprice"] == 0.5

    def test_stale_flag(self, tmp_db, frozen_now) -> None:
        set_state("last_scrape_at", "2026-10-06 09:00:00")  # 3 小时前 > 默认 2h
        assert ds.live(today=date(2026, 10, 7))["stale"] is True

    def test_scrape_status_is_published(self, tmp_db) -> None:
        set_state("last_scrape_status", "stale")
        assert ds.live(today=date(2026, 10, 7))["scrape_status"] == "stale"

    def test_eqprice_fallback_chain(self, tmp_db, monkeypatch) -> None:
        assert ds.eqprice() == 0.5  # 默认
        monkeypatch.setenv("DORM_EQPRICE", "0.62")
        assert ds.eqprice() == 0.62  # 环境变量
        set_many({"eqprice": 0.88})
        assert ds.eqprice() == 0.88  # 配置优先

    def test_daily_avg_uses_a_seven_day_window(self, tmp_db, frozen_now) -> None:
        """概览的「日均用量」必须算得出来。

        📌 ``stats.daily_avg`` 要求跨度 ≥ 1 天。``live()`` 曾经只查 24 小时
        （``DEFAULT_HOURS``），窗口里永远凑不满一天 → 卡片恒显示「—」。
        ``days_remaining()`` 早就改用 ``DAILY_AVG_HOURS`` 了，这里锁住
        ``live()`` 也必须用 7 天窗口。

        数据：3 天前 60 度 → 现在 54 度 = 6 度 / 3 天 = 2.0 度/天。
        24 小时窗口里只剩后一条 → 跨度为 0 → 修之前这里是 ``None``。
        """
        RecordRepo.insert(Record(ts="2026-10-03 12:00:00", read_time=None, remain=60.0))
        RecordRepo.insert(Record(ts="2026-10-06 12:00:00", read_time=None, remain=54.0))
        payload = ds.live(today=date(2026, 10, 7))
        assert payload["stats"]["daily_avg"] == pytest.approx(2.0)

    def test_hourly_used_stays_within_the_day(self, tmp_db, frozen_now) -> None:
        """「近一小时」不能拿 6 天前的记录去算。

        📌 ``live()`` 为了让 ``daily_avg`` 有值而查 7 天窗口，但
        ``hourly_used`` 必须仍来自 24 小时窗口 —— 否则那张写着
        「近一小时」的卡片会显示一个 6 天跨度的差值。
        """
        RecordRepo.insert(Record(ts="2026-09-30 12:00:00", read_time=None, remain=60.0))
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=54.0))
        payload = ds.live(today=date(2026, 10, 7))
        # 24 小时窗口里只剩 10-06 11:00 这一条 → 算不出「小时用量」
        assert payload["stats"]["hourly_used"] is None
        # 但 7 天窗口里的日均照常（6 度 / 5.96 天 ≈ 1.01 度/天）
        assert payload["stats"]["daily_avg"] == pytest.approx(1.01)

    def test_days_remaining(self, tmp_db, frozen_now) -> None:
        RecordRepo.insert(Record(ts="2026-09-29 12:00:00", read_time=None, remain=44.0))
        RecordRepo.insert(Record(ts="2026-10-06 12:00:00", read_time=None, remain=30.0))
        # daily_avg = (44 - 30) / 7 = 2.0 → 30 / 2.0 = 15 天
        assert ds.days_remaining() == pytest.approx(15.0)

    def test_days_remaining_without_data(self, tmp_db) -> None:
        assert ds.days_remaining() is None


class TestCleanup:
    def test_deletes_old_rows(self, tmp_db, frozen_now) -> None:
        RecordRepo.insert(Record(ts="2026-09-01 00:00:00", read_time=None, remain=1.0))
        RecordRepo.insert(Record(ts="2026-10-06 00:00:00", read_time=None, remain=2.0))
        assert ds.cleanup_before(days=30) == 1
        assert len(RecordRepo.query(hours=24 * 400)) == 1
