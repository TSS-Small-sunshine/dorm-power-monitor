"""``starwatt.notify.policies`` —— 告警判定 / 冷却 / 幂等打点 / L3 节奏。

这些规则是「不打扰用户」与「不漏报」之间的平衡点，改错一个就会：
要么每 10 分钟轰炸一次，要么静默吞掉一整天的告警。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from starwatt.config_registry import get_str, set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import RecordRepo, RunStatusRepo, ViolationRepo
from starwatt.notify import policies

NOW = datetime(2026, 10, 6, 12, 0, 0)
STAMP = "2026-10-06 12:00:00"


@pytest.fixture(autouse=True)
def _freeze_time(frozen_now):
    """本模块所有用例都跑在 ``FIXED_NOW``（2026-10-06 12:00）上。

    为什么必须：``RecordRepo.query(hours=24)`` 这类查询的**窗口**由
    ``timeutil.now_cst()`` 决定，不冻结的话插进去的记录会落在窗口之外，
    用例会因为「查不到数据」而通过 —— 那是假通过。
    """
    return frozen_now


def _insert_record(ts: str, remain: float | None) -> None:
    RecordRepo.insert(Record(ts=ts, read_time=ts, remain=remain))


def _insert_violation(dt: str, reason: str = "大功率", power: float = 1.2) -> None:
    ViolationRepo.upsert_many(
        "room-1", [{"roomId": "room-1", "dt": dt, "wg_reason": reason, "wg_power": power}]
    )


def _no_quiet_hours() -> None:
    """关掉静默时段（``start == end`` 视为不静默）—— 让 L1/L2 断言与真实时钟无关。"""
    set_many({"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"})


# ===========================================================================
# 离线判定
# ===========================================================================
class TestIsOffline:
    @pytest.mark.parametrize("label", ["在线", "正常", "通讯正常"])
    def test_online_labels(self, tmp_db, label) -> None:
        assert policies.is_offline({"run_status": label}) is False

    @pytest.mark.parametrize("label", ["离线", "未知", "", None])
    def test_other_labels_are_offline(self, tmp_db, label) -> None:
        assert policies.is_offline({"run_status": label}) is True

    def test_missing_row_is_offline(self, tmp_db) -> None:
        """从未成功抓过 F4 → 视为离线（宁可误报也不能静默失败，B6）。"""
        assert policies.is_offline(None) is True
        assert policies.is_offline({}) is True

    def test_reads_entity_attribute(self, tmp_db) -> None:
        RunStatusRepo.upsert("room-1", {"runStatus": "在线"})
        assert policies.is_offline(RunStatusRepo.get("room-1")) is False

    def test_online_values_are_configurable(self, tmp_db) -> None:
        set_many({"offline_status_values": ["ONLINE"]})
        assert policies.is_offline({"run_status": "ONLINE"}) is False
        assert policies.is_offline({"run_status": "在线"}) is True


class TestOfflineCardPayload:
    def test_maps_snake_case_to_camel_case(self, tmp_db) -> None:
        """🔑 修掉 legacy 的 bug：DB 是蛇形键、卡片只认驼峰 → 以前永远显示 —。"""
        RunStatusRepo.upsert(
            "room-1",
            {
                "runStatus": "离线",
                "workStatus": "通讯中断",
                "stopReason": "欠费",
                "updateDt": "2026-10-06 11:00:00",
            },
        )
        payload = policies.offline_card_payload(RunStatusRepo.get("room-1"))
        assert payload == {
            "runStatus": "离线",
            "workStatus": "通讯中断",
            "stopReason": "欠费",
            "updateDt": "2026-10-06 11:00:00",
        }

    def test_none_becomes_empty(self, tmp_db) -> None:
        assert policies.offline_card_payload(None) == {}


# ===========================================================================
# L1 低电
# ===========================================================================
class TestLowBatteryDue:
    def test_no_records(self, tmp_db) -> None:
        assert policies.low_battery_due(now=NOW) is False

    def test_not_low(self, tmp_db) -> None:
        _insert_record("2026-10-06 11:50:00", 100.0)
        assert policies.low_battery_due(now=NOW) is False

    def test_first_ever_low_reading_alerts(self, tmp_db) -> None:
        _insert_record("2026-10-06 11:50:00", 10.0)
        assert policies.low_battery_due(now=NOW) is True

    def test_transition_alerts_once(self, tmp_db) -> None:
        _insert_record("2026-10-06 11:40:00", 100.0)
        _insert_record("2026-10-06 11:50:00", 10.0)
        assert policies.low_battery_due(now=NOW) is True

    def test_still_low_stays_quiet(self, tmp_db) -> None:
        """同一段低电期不重复轰炸（legacy 的核心行为）。"""
        _insert_record("2026-10-06 11:40:00", 10.0)
        _insert_record("2026-10-06 11:50:00", 9.0)
        assert policies.low_battery_due(now=NOW) is False

    def test_missing_remain_does_not_alert(self, tmp_db) -> None:
        _insert_record("2026-10-06 11:50:00", None)
        assert policies.low_battery_due(now=NOW) is False

    def test_rearm_requires_recovery_above_threshold(self, tmp_db) -> None:
        """已告警过 → 只有回升到 ``low_battery_rearm`` 以上才算重新武装。"""
        _insert_record("2026-10-06 11:00:00", 20.0)
        set_state(policies.STATE_KEYS["low_battery"], "2026-10-06 11:05:00")
        _insert_record("2026-10-06 11:50:00", 10.0)  # 更低，未重新武装
        assert policies.low_battery_due(now=NOW) is False

    def test_rearm_after_recovery(self, tmp_db) -> None:
        _insert_record("2026-10-06 11:00:00", 20.0)
        set_state(policies.STATE_KEYS["low_battery"], "2026-10-06 11:05:00")
        _insert_record("2026-10-06 11:30:00", 60.0)  # 充值 → 重新武装
        _insert_record("2026-10-06 11:50:00", 12.0)  # 再次跌破
        assert policies.low_battery_due(now=NOW) is True

    def test_threshold_follows_config(self, tmp_db) -> None:
        set_many({"threshold_red": 5})
        _insert_record("2026-10-06 11:50:00", 10.0)
        assert policies.low_battery_due(now=NOW) is False


# ===========================================================================
# 违规
# ===========================================================================
class TestViolationRowsToPush:
    def test_no_rows(self, tmp_db) -> None:
        assert policies.violation_rows_to_push("room-1", now=NOW) == []

    def test_empty_room(self, tmp_db) -> None:
        _insert_violation("2026-10-06 11:00:00")
        assert policies.violation_rows_to_push("", now=NOW) == []

    def test_first_time_pushes_everything(self, tmp_db) -> None:
        _insert_violation("2026-10-06 11:00:00")
        _insert_violation("2026-10-06 10:00:00")
        assert len(policies.violation_rows_to_push("room-1", now=NOW)) == 2

    def test_only_newer_than_last_alert(self, tmp_db) -> None:
        _insert_violation("2026-10-06 11:00:00")
        _insert_violation("2026-10-06 09:00:00")
        set_state(policies.STATE_KEYS["violation"], "2026-10-06 10:00:00")
        rows = policies.violation_rows_to_push("room-1", now=NOW)
        assert [r["dt"] for r in rows] == ["2026-10-06 11:00:00"]

    def test_cooldown_blocks(self, tmp_db) -> None:
        """30 分钟冷却期内一律不推（即使有新记录）。"""
        _insert_violation("2026-10-06 11:59:00")
        set_state(policies.STATE_KEYS["violation"], "2026-10-06 11:45:00")
        assert policies.violation_rows_to_push("room-1", now=NOW) == []

    def test_cooldown_expires(self, tmp_db) -> None:
        _insert_violation("2026-10-06 11:59:00")
        set_state(policies.STATE_KEYS["violation"], "2026-10-06 11:00:00")
        assert len(policies.violation_rows_to_push("room-1", now=NOW)) == 1

    def test_capped_at_eight(self, tmp_db) -> None:
        for hour in range(10):
            _insert_violation(f"2026-10-06 {hour:02d}:00:00")
        assert len(policies.violation_rows_to_push("room-1", now=NOW)) == 8

    def test_unparseable_dt_counts_as_new(self, tmp_db) -> None:
        ViolationRepo.upsert_many(
            "room-1",
            [{"roomId": "room-1", "dt": "not-a-date", "wg_reason": "x", "wg_power": 1.0}],
        )
        set_state(policies.STATE_KEYS["violation"], "2026-10-06 11:00:00")
        assert len(policies.violation_rows_to_push("room-1", now=NOW)) == 1

    def test_returns_plain_dicts(self, tmp_db) -> None:
        _insert_violation("2026-10-06 11:00:00")
        row = policies.violation_rows_to_push("room-1", now=NOW)[0]
        assert isinstance(row, dict) and row["wg_reason"] == "大功率"


# ===========================================================================
# stale
# ===========================================================================
class TestStaleDueGap:
    def test_never_scraped(self, tmp_db) -> None:
        assert policies.stale_due_gap(now=NOW) is None

    def test_within_threshold(self, tmp_db) -> None:
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 11:00:00")
        assert policies.stale_due_gap(now=NOW) is None

    def test_over_threshold(self, tmp_db) -> None:
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        assert policies.stale_due_gap(now=NOW) == pytest.approx(3 * 3600)

    def test_already_alerted_for_this_period(self, tmp_db) -> None:
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        set_state(policies.STATE_KEYS["stale"], "2026-10-06 10:00:00")
        assert policies.stale_due_gap(now=NOW) is None

    def test_new_stale_period_alerts_again(self, tmp_db) -> None:
        """上次告警**早于**上次成功抓取 → 属于新的陈旧期。"""
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        set_state(policies.STATE_KEYS["stale"], "2026-10-06 08:00:00")
        assert policies.stale_due_gap(now=NOW) is not None

    def test_threshold_follows_config(self, tmp_db) -> None:
        set_many({"stale_hours": 8})
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        assert policies.stale_due_gap(now=NOW) is None


# ===========================================================================
# L3 节奏
# ===========================================================================
class TestReadPushTime:
    def test_default_when_unset(self, tmp_db) -> None:
        assert policies.read_push_time("push_daily_time", "09:00") == (9, 0)

    def test_reads_config(self, tmp_db) -> None:
        set_many({"push_daily_time": "08:30"})
        assert policies.read_push_time("push_daily_time", "09:00") == (8, 30)

    @pytest.mark.parametrize("bad", ["abc", "25:00", "09:99", "0900"])
    def test_malformed_falls_back(self, tmp_db, bad) -> None:
        set_many({"push_daily_time": bad})
        assert policies.read_push_time("push_daily_time", "09:00") == (9, 0)


class TestL3Due:
    def test_unknown_kind_raises(self, tmp_db) -> None:
        with pytest.raises(KeyError):
            policies.l3_due("yearly")

    def test_daily_at_configured_time(self, tmp_db) -> None:
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is True

    def test_daily_wrong_minute(self, tmp_db) -> None:
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 1)) is False

    def test_daily_stamped_today(self, tmp_db) -> None:
        set_state(policies.STATE_KEYS["daily"], "2026-10-06")
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is False

    def test_daily_stamped_yesterday(self, tmp_db) -> None:
        set_state(policies.STATE_KEYS["daily"], "2026-10-05")
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is True

    def test_weekly_only_on_monday(self, tmp_db) -> None:
        monday = datetime(2026, 10, 5, 9, 0)
        assert monday.isoweekday() == 1
        assert policies.l3_due("weekly", now=monday) is True
        assert policies.l3_due("weekly", now=datetime(2026, 10, 6, 9, 0)) is False

    def test_weekly_stamp_key_is_iso_week(self, tmp_db) -> None:
        monday = datetime(2026, 10, 5, 9, 0)
        policies.stamp_l3("weekly", now=monday)
        iso = monday.isocalendar()
        assert get_str(policies.STATE_KEYS["weekly"]) == f"{iso.year}-W{iso.week:02d}"
        assert policies.l3_due("weekly", now=monday) is False

    def test_monthly_only_on_first_day(self, tmp_db) -> None:
        assert policies.l3_due("monthly", now=datetime(2026, 11, 1, 9, 0)) is True
        assert policies.l3_due("monthly", now=datetime(2026, 11, 2, 9, 0)) is False

    def test_monthly_stamp_key_is_year_month(self, tmp_db) -> None:
        policies.stamp_l3("monthly", now=datetime(2026, 11, 1, 9, 0))
        assert get_str(policies.STATE_KEYS["monthly"]) == "2026-11"

    def test_disabled_layer_is_never_due(self, tmp_db) -> None:
        set_many({"push_daily_enable": False})
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is False

    def test_master_switch_off_disables_all_l3(self, tmp_db) -> None:
        """Q16：总开关关闭时不必逐个关分项。"""
        set_many({"push_group_enabled": False})
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is False

    def test_push_time_follows_config(self, tmp_db) -> None:
        set_many({"push_daily_time": "21:30"})
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 21, 30)) is True
        assert policies.l3_due("daily", now=datetime(2026, 10, 6, 9, 0)) is False

    def test_stamp_l3_rejects_unknown_kind(self, tmp_db) -> None:
        with pytest.raises(KeyError):
            policies.stamp_l3("yearly")


# ===========================================================================
# 发送 + 打点（幂等铁律：**发送成功才打点**）
# ===========================================================================
class TestPushStamping:
    """用假 ``post_card`` 观察「发没发出去」与「有没有打点」。"""

    @staticmethod
    def _fake_post(monkeypatch, result: bool) -> list[dict]:
        sent: list[dict] = []

        def _post(card, **_kwargs):
            sent.append(card)
            return result

        monkeypatch.setattr(policies.transport, "post_card", _post)
        return sent

    def test_stale_success_stamps(self, tmp_db, monkeypatch) -> None:
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        sent = self._fake_post(monkeypatch, True)

        assert policies.push_stale_if_due(now=NOW) is True
        assert sent and get_str(policies.STATE_KEYS["stale"]) == STAMP

    def test_stale_failure_does_not_stamp(self, tmp_db, monkeypatch) -> None:
        """🔑 发失败不打点 → 下个 tick 还能重试（否则一天告警被一次抖动吞掉）。"""
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        self._fake_post(monkeypatch, False)

        assert policies.push_stale_if_due(now=NOW) is False
        assert get_str(policies.STATE_KEYS["stale"]) == ""

    def test_low_battery_success_stamps(self, tmp_db, monkeypatch) -> None:
        _no_quiet_hours()
        _insert_record("2026-10-06 11:50:00", 10.0)
        sent = self._fake_post(monkeypatch, True)

        assert policies.push_low_battery_if_due(now=NOW) is True
        assert sent[0]["card"]["header"]["template"] == "red"
        assert get_str(policies.STATE_KEYS["low_battery"]) == STAMP

    def test_low_battery_layer_disabled_skips(self, tmp_db, monkeypatch) -> None:
        _no_quiet_hours()
        _insert_record("2026-10-06 11:50:00", 10.0)
        sent = self._fake_post(monkeypatch, True)
        set_many({"push_l1_enable": False})

        assert policies.push_low_battery_if_due(now=NOW) is False
        assert sent == []  # 开关闸门在发送之前

    def test_violations_success_stamps(self, tmp_db, monkeypatch) -> None:
        _insert_violation("2026-10-06 11:00:00")
        sent = self._fake_post(monkeypatch, True)

        assert policies.push_violations_if_due("room-1", now=NOW) is True
        assert sent and get_str(policies.STATE_KEYS["violation"]) == STAMP

    def test_violations_nothing_new_skips(self, tmp_db, monkeypatch) -> None:
        sent = self._fake_post(monkeypatch, True)
        assert policies.push_violations_if_due("room-1", now=NOW) is False
        assert sent == []

    def test_offline_success_sends_card(self, tmp_db, monkeypatch) -> None:
        RunStatusRepo.upsert(
            "room-1", {"runStatus": "离线", "updateDt": "2026-10-06 11:00:00"}
        )
        sent = self._fake_post(monkeypatch, True)

        assert policies.push_offline_if_due("room-1") is True
        content = sent[0]["card"]["elements"][0]["text"]["content"]
        assert "离线" in content and "2026-10-06 11:00:00" in content

    def test_offline_online_meter_skips(self, tmp_db, monkeypatch) -> None:
        RunStatusRepo.upsert("room-1", {"runStatus": "在线"})
        sent = self._fake_post(monkeypatch, True)
        assert policies.push_offline_if_due("room-1") is False
        assert sent == []

    def test_summary_success(self, tmp_db, monkeypatch) -> None:
        _no_quiet_hours()
        sent = self._fake_post(monkeypatch, True)
        body = {"remainEq": "12.34", "totalEq": "100.0", "dt": "2026-10-06 12:00:00"}

        assert policies.push_summary(body, "6号楼-1-119", now=NOW) is True
        assert sent[0]["card"]["header"]["subtitle"]["content"] == "剩余 12.34 kW·h"

    def test_summary_respects_stale_flag(self, tmp_db, monkeypatch) -> None:
        _no_quiet_hours()
        sent = self._fake_post(monkeypatch, True)
        policies.push_summary({"remainEq": "12.34"}, "房", stale=True, now=NOW)
        assert sent[0]["card"]["header"]["template"] == "yellow"
