"""``starwatt.notify.reports`` + L3 卡片 + ``push_l3_if_due``（M3）。

三份报表是「到点自动发」的内容，没人会盯着看 —— 数字错了只会悄悄发出去。
所以这里逐字段钉死算法，并且专门验证「库是空的」这条最容易被忽略的路径。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from starwatt.config_registry import get_str, set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import DailyElecRepo, PayRepo, RecordRepo, ViolationRepo
from starwatt.notify import card_builder as cb
from starwatt.notify import policies, reports

#: 与 conftest 的 FIXED_NOW 同一时刻
NOW = datetime(2026, 10, 6, 12, 0, 0)
ROOM = "room-1"


@pytest.fixture(autouse=True)
def _freeze_time(frozen_now):
    """报表的取数窗口（``recent(days=N)``）由 ``now_cst()`` 决定 → 必须冻结。"""
    return frozen_now


def _seed_room() -> None:
    set_state("last_room_id", ROOM)
    set_state("room_label", "6号楼-1-119")


def _seed_daily(rows: list[tuple[str, float]]) -> None:
    DailyElecRepo.upsert_many(
        ROOM,
        [
            {"roomId": ROOM, "dt": dt, "total_eq": value, "zong_eq": value}
            for dt, value in rows
        ],
    )


def _seed_pay(rows: list[tuple[str, str, float]]) -> None:
    PayRepo.upsert_many(
        ROOM,
        [
            {"roomId": ROOM, "dt": dt, "pay_type": "充值", "fee_type": fee, "money": money}
            for dt, fee, money in rows
        ],
    )


def _seed_violations(dts: list[str]) -> None:
    ViolationRepo.upsert_many(
        ROOM,
        [
            {"roomId": ROOM, "dt": dt, "wg_reason": "大功率", "wg_power": 1.2}
            for dt in dts
        ],
    )


def _seed_record(remain: float = 42.0, read_time: str = "2026-10-06 11:00:00") -> None:
    RecordRepo.insert(
        Record(ts="2026-10-06 11:59:00", read_time=read_time, remain=remain)
    )


# ===========================================================================
# 空库：绝不抛异常（报表不能因为某张表为空就让调度器崩）
# ===========================================================================
class TestEmptyDatabase:
    @pytest.mark.parametrize("kind", ["daily", "weekly", "monthly"])
    def test_summary_degrades_to_zeros(self, tmp_db, kind) -> None:
        summary = reports.build_summary(kind)
        assert summary["kind"] == kind
        assert summary["remain"] is None
        assert summary["dt"] is None

    def test_no_room_id_is_handled(self, tmp_db) -> None:
        assert reports.daily_summary()["today_kwh"] == 0.0
        assert reports.weekly_summary()["recharge_total"] == 0.0
        assert reports.monthly_summary()["violations_total"] == 0

    def test_missing_meta_table_is_handled(self, settings_override) -> None:
        """首次启动（``meta`` 表还没建）时也不能抛。"""
        assert reports.daily_summary()["kind"] == "daily"


# ===========================================================================
# 日报
# ===========================================================================
class TestDailySummary:
    def test_fields(self, tmp_db) -> None:
        _seed_room()
        _seed_record(remain=42.0)
        _seed_daily(
            [("2026-10-04", 100.0), ("2026-10-05", 103.0), ("2026-10-06", 108.0)]
        )
        summary = reports.daily_summary(now=NOW)

        assert summary["remain"] == 42.0
        assert summary["dt"] == "2026-10-06 11:00:00"
        assert summary["today_kwh"] == pytest.approx(5.0)  # 108 - 103
        assert summary["yesterday_kwh"] == pytest.approx(3.0)  # 103 - 100
        assert summary["avg7_kwh"] == pytest.approx(4.0)  # (3 + 5) / 2
        assert summary["month_total_kwh"] == pytest.approx(108.0)

    def test_violations_today_counts_today_only(self, tmp_db) -> None:
        """「今日违规」只算今天 —— 同月的**昨天**不算。

        📌 卡片上写的是「⚠ 今日违规」（见 ``card_builder``），但实现原来按
        **月份前缀**过滤，而 ``ViolationRepo.recent(days=1)`` 的截止是
        「昨天」这个日期 —— 于是昨天的违规被算进了今天。
        这条测试原本叫 ``test_violations_today_counts_current_month``，
        只种了今天的数据，两种写法都能通过，等于把错误行为写成了规范。
        """
        _seed_room()
        _seed_violations(
            ["2026-10-06 09:00:00", "2026-10-06 10:00:00", "2026-10-05 22:00:00"]
        )
        assert reports.daily_summary(now=NOW)["violations_today"] == 2

    def test_single_row_has_no_delta(self, tmp_db) -> None:
        _seed_room()
        _seed_daily([("2026-10-06", 108.0)])
        summary = reports.daily_summary(now=NOW)
        assert summary["today_kwh"] == 0.0
        assert summary["yesterday_kwh"] == 0.0
        assert summary["month_total_kwh"] == pytest.approx(108.0)


# ===========================================================================
# 周报 / 月报
# ===========================================================================
class TestWeeklySummary:
    def test_fields(self, tmp_db) -> None:
        _seed_room()
        _seed_record()
        _seed_daily([(f"2026-09-{day:02d}", float(day)) for day in range(20, 28)])
        _seed_pay([("2026-10-01", "电费", 50.0), ("2026-10-02", "电费退费", -10.0)])
        _seed_violations(["2026-10-05 09:00:00"])

        summary = reports.weekly_summary(now=NOW)

        assert summary["last_week_kwh"] == pytest.approx(7.0)  # 27 - 20
        assert summary["month_total_kwh"] == 0.0  # 9 月的数据不算 10 月
        assert summary["recharge_total"] == pytest.approx(50.0)  # 退费不计
        assert summary["violations_week"] == 1


class TestMonthlySummary:
    def test_fields(self, tmp_db) -> None:
        _seed_room()
        _seed_record()
        _seed_daily(
            [
                ("2026-09-01", 10.0),
                ("2026-09-30", 40.0),  # 上月累计 = 30
                ("2026-10-01", 41.0),
                ("2026-10-06", 46.0),  # 本月累计 = 5
            ]
        )
        _seed_pay([("2026-09-10", "电费", 20.0), ("2026-10-03", "电费", 30.0)])
        _seed_violations(["2026-09-20 09:00:00", "2026-10-05 09:00:00"])

        summary = reports.monthly_summary(now=NOW)

        assert summary["last_month_kwh"] == pytest.approx(30.0)
        assert summary["this_month_kwh"] == pytest.approx(5.0)
        assert summary["recharge_total"] == pytest.approx(50.0)
        assert summary["violations_total"] == 2

    def test_this_month_single_row_falls_back_to_cumulative(self, tmp_db) -> None:
        """本月只有一行 → 用该行的累积值（legacy 的兜底）。"""
        _seed_room()
        _seed_daily([("2026-10-06", 46.0)])
        assert reports.monthly_summary(now=NOW)["this_month_kwh"] == pytest.approx(46.0)


class TestBuildSummaryDispatch:
    def test_dispatches(self, tmp_db) -> None:
        for kind in reports.KINDS:
            assert reports.build_summary(kind)["kind"] == kind

    def test_unknown_kind_raises(self, tmp_db) -> None:
        with pytest.raises(KeyError):
            reports.build_summary("yearly")


# ===========================================================================
# L3 卡片
# ===========================================================================
class TestL3Card:
    @staticmethod
    def _card(kind: str, **summary) -> dict:
        base = {"remain": 42.0, "dt": "2026-10-06 11:00:00", "month_total_kwh": 100.0}
        base.update(summary)
        return cb.build_l3_card(
            kind,
            base,
            "6号楼-1-119",
            "note-fixture",
            title_prefix=reports.L3_TITLES[kind],
            header_template=reports.L3_TEMPLATES[kind],
        )

    @pytest.mark.parametrize("kind", reports.KINDS)
    def test_header_uses_report_title_and_color(self, tmp_db, kind) -> None:
        card = self._card(kind)
        assert card["card"]["header"]["title"]["content"] == (
            f"{reports.L3_TITLES[kind]} 6号楼-1-119"
        )
        assert card["card"]["header"]["template"] == reports.L3_TEMPLATES[kind]

    def test_daily_narrative(self, tmp_db) -> None:
        card = self._card("daily", today_kwh=5.0, yesterday_kwh=3.0, avg7_kwh=4.0)
        narrative = card["card"]["elements"][-2]["text"]["content"]
        assert "今日：**5.00** kW·h" in narrative
        assert "昨日：**3.00** kW·h" in narrative
        assert "7 日均值：**4.00** kW·h/天" in narrative

    def test_daily_violations_line_only_when_nonzero(self, tmp_db) -> None:
        without = self._card("daily")["card"]["elements"][-2]["text"]["content"]
        with_violation = self._card("daily", violations_today=3)["card"]["elements"][-2][
            "text"
        ]["content"]
        assert "违规" not in without
        assert "今日违规：**3** 次" in with_violation

    def test_weekly_narrative(self, tmp_db) -> None:
        card = self._card("weekly", last_week_kwh=12.5, recharge_total=50.0)
        narrative = card["card"]["elements"][-2]["text"]["content"]
        assert "上周累计：**12.50** kW·h" in narrative
        assert "近 30 天充值：**¥50.00**" in narrative

    def test_monthly_narrative(self, tmp_db) -> None:
        card = self._card("monthly", last_month_kwh=30.0, this_month_kwh=5.0)
        narrative = card["card"]["elements"][-2]["text"]["content"]
        assert "上月累计：**30.00** kW·h" in narrative
        assert "本月累计：**5.00** kW·h" in narrative
        assert "近 60 天充值" in narrative

    def test_narrative_sits_before_the_note(self, tmp_db) -> None:
        elements = self._card("daily")["card"]["elements"]
        assert elements[-1]["tag"] == "note"
        assert elements[-2]["tag"] == "div"

    def test_narrative_never_uses_top_of_hour_prefix(self, tmp_db) -> None:
        title = self._card("daily")["card"]["header"]["title"]["content"]
        assert "整点播报" not in title

    def test_monthly_total_eq_uses_this_month(self, tmp_db) -> None:
        card = self._card("monthly", this_month_kwh=5.0, month_total_kwh=100.0)
        assert "累计用电 **5.00**" in card["card"]["elements"][0]["text"]["content"]

    def test_daily_total_eq_uses_month_total(self, tmp_db) -> None:
        card = self._card("daily", this_month_kwh=5.0, month_total_kwh=100.0)
        assert "累计用电 **100.00**" in card["card"]["elements"][0]["text"]["content"]


# ===========================================================================
# push_l3_if_due
# ===========================================================================
class TestPushL3:
    @staticmethod
    def _fake_post(monkeypatch, result: bool) -> list[dict]:
        sent: list[dict] = []

        def _post(card, **_kwargs):
            sent.append(card)
            return result

        monkeypatch.setattr(policies.transport, "post_card", _post)
        return sent

    def test_not_due_skips(self, tmp_db, monkeypatch) -> None:
        sent = self._fake_post(monkeypatch, True)
        assert policies.push_l3_if_due("daily", now=NOW) is False  # 12:00 ≠ 09:00
        assert sent == []

    def test_due_and_sent_stamps(self, tmp_db, monkeypatch) -> None:
        _seed_room()
        sent = self._fake_post(monkeypatch, True)
        nine = datetime(2026, 10, 6, 9, 0)

        assert policies.push_l3_if_due("daily", now=nine) is True
        assert sent and "每日用电报告" in sent[0]["card"]["header"]["title"]["content"]
        assert get_str(policies.STATE_KEYS["daily"]) == "2026-10-06"
        # 同一天不会重复发
        assert policies.push_l3_if_due("daily", now=nine) is False

    def test_send_failure_does_not_stamp(self, tmp_db, monkeypatch) -> None:
        _seed_room()
        self._fake_post(monkeypatch, False)
        nine = datetime(2026, 10, 6, 9, 0)

        assert policies.push_l3_if_due("daily", now=nine) is False
        assert get_str(policies.STATE_KEYS["daily"]) == ""

    def test_disabled_layer_skips(self, tmp_db, monkeypatch) -> None:
        _seed_room()
        sent = self._fake_post(monkeypatch, True)
        set_many({"push_daily_enable": False})

        assert policies.push_l3_if_due("daily", now=datetime(2026, 10, 6, 9, 0)) is False
        assert sent == []

    def test_weekly_and_monthly_use_their_own_layers(self, tmp_db, monkeypatch) -> None:
        _seed_room()
        sent = self._fake_post(monkeypatch, True)
        set_many({"push_weekly_enable": False})

        assert (
            policies.push_l3_if_due("weekly", now=datetime(2026, 10, 5, 9, 0)) is False
        )
        assert (
            policies.push_l3_if_due("monthly", now=datetime(2026, 11, 1, 9, 0)) is True
        )
        assert len(sent) == 1

    def test_room_label_is_used_in_title(self, tmp_db, monkeypatch) -> None:
        _seed_room()
        sent = self._fake_post(monkeypatch, True)
        policies.push_l3_if_due("daily", now=datetime(2026, 10, 6, 9, 0))
        assert "6号楼-1-119" in sent[0]["card"]["header"]["title"]["content"]

    def test_room_label_helper(self, tmp_db) -> None:
        assert policies.room_label() is None
        set_state("room_label", "6号楼-1-119")
        assert policies.room_label() == "6号楼-1-119"
