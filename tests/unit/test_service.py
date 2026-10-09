"""``starwatt.scraper.service`` —— 节流 / 回填 / stale 降级（M2）。

这些用例守住四条**不能退化**的约束：

1. **F2 是 ``daily_elec`` 的唯一来源** —— F1 的 ``useEq`` 绝不落进那张表
2. **回填全成功才置位** ``backfill_done``（Round 39）—— 部分失败必须能重试
3. **抓取失败降级为 ``stale``** —— 旧数据继续可用，不清零、不向用户报错
4. **``fetch_only`` 零副作用** —— 冒烟模式不得写库、不得写状态
"""
from __future__ import annotations

from datetime import datetime

import pytest

from starwatt.config_registry import get_bool, get_str, set_many, set_state
from starwatt.scraper import service as svc
from starwatt.scraper.client import FINDUSER_PATH, ScrapeError
from starwatt.scraper.endpoints import PATH_DATA, PATH_DAY_ELECT

#: 与 conftest 的 ``FIXED_NOW`` 同一时刻（冻结时间后两者一致）
NOW = datetime(2026, 10, 6, 12, 0, 0)
STAMP_NOW = "2026-10-06 12:00:00"

THROTTLE_STATE_KEYS = ("last_daily_elect_at", "last_violation_check_at", "last_pay_at")


# ===========================================================================
# 替身
# ===========================================================================
class FakeClient:
    """最小 ``SchoolClient`` 替身：记录调用，**不发网络**。"""

    def __init__(self, finduser_html: str = "", payloads=None, fail_markers=()) -> None:
        self.finduser_html = finduser_html
        self.payloads = dict(payloads or {})
        self.fail_markers = tuple(fail_markers)
        self.calls: list[tuple] = []
        self.closed = False

    def get(self, path: str, **kwargs) -> str:
        self.calls.append(("GET", path))
        if FINDUSER_PATH in path:
            return self.finduser_html
        return ""

    def post_form(self, path: str, data: dict) -> dict:
        self.calls.append(("POST", path, dict(data)))
        for marker in self.fail_markers:
            if marker in path:
                raise ScrapeError(f"{path} 返回 HTTP 500")
        return self.payloads.get(path.split("?")[0], {})

    def close(self) -> None:
        self.closed = True


class FakeEndpoint:
    """最小 ``Endpoint`` 替身（可注入错误）。"""

    def __init__(self, key: str, *, rows=None, error: Exception | None = None,
                 interval_sec: int | None = None) -> None:
        self.key = key
        self.interval_sec = interval_sec
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = 0
        self.persisted = None

    def fetch(self, ctx) -> list:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.rows

    def persist(self, rows) -> None:
        self.persisted = rows


def _configure(**overrides) -> None:
    """写入最小可用的抓取配置（openid + room_id）。"""
    errors = set_many({"dorm_openid": "o-1", "dorm_room_id": "room-1", **overrides})
    assert errors == {}, errors


def _stamp_throttles(stamp: str = "2026-10-06 11:59:00") -> None:
    """把三个节流状态键都设成「刚刚跑过」。"""
    for key in THROTTLE_STATE_KEYS:
        set_state(key, stamp)


# ===========================================================================
# 节流判定
# ===========================================================================
class TestIsDue:
    def test_never_seen_is_due(self, tmp_db, frozen_now) -> None:
        assert svc.is_due("last_pay_at", 3600) is True

    def test_just_stamped_is_not_due(self, tmp_db, frozen_now) -> None:
        set_state("last_pay_at", STAMP_NOW)
        assert svc.is_due("last_pay_at", 3600) is False

    def test_old_stamp_is_due(self, tmp_db, frozen_now) -> None:
        set_state("last_pay_at", "2026-10-06 10:00:00")
        assert svc.is_due("last_pay_at", 3600) is True

    def test_boundary_is_inclusive(self, tmp_db, frozen_now) -> None:
        """恰好等于间隔 → 该跑了（``>=`` 而不是 ``>``）。"""
        set_state("last_pay_at", "2026-10-06 11:00:00")
        assert svc.is_due("last_pay_at", 3600) is True

    def test_corrupt_stamp_is_due(self, tmp_db, frozen_now) -> None:
        """坏值不能把数据永久卡死 —— 宁可多抓一次。"""
        set_state("last_pay_at", "not-a-timestamp")
        assert svc.is_due("last_pay_at", 3600) is True

    def test_none_interval_always_due(self, tmp_db, frozen_now) -> None:
        set_state("last_pay_at", STAMP_NOW)
        assert svc.is_due("last_pay_at", None) is True
        assert svc.is_due("last_pay_at", 0) is True


class TestThrottleSeconds:
    def test_defaults_come_from_the_registry(self, tmp_db) -> None:
        assert svc.throttle_seconds("F2") == 24 * 3600
        assert svc.throttle_seconds("F3") == 3600
        assert svc.throttle_seconds("F5") == 24 * 3600

    def test_every_run_endpoints_have_no_throttle(self, tmp_db) -> None:
        assert svc.throttle_seconds("F1") is None
        assert svc.throttle_seconds("F4") is None

    def test_config_override_wins(self, tmp_db) -> None:
        set_many({"scrape_f2_throttle_hours": 2})
        assert svc.throttle_seconds("F2") == 2 * 3600

    def test_hours_are_floored_at_one(self, tmp_db) -> None:
        """配置校验是 1..720；这里兜底防止 0 小时导致疯狂抓取。"""
        set_many({"scrape_f3_throttle_hours": 1})
        assert svc.throttle_seconds("F3") == 3600

    def test_mapping_covers_exactly_the_throttled_endpoints(self) -> None:
        assert set(svc.THROTTLES) == {"F2", "F3", "F5"}
        assert set(svc.BACKFILL_KEYS) == {"F2", "F5"}


# ===========================================================================
# 编排：每次抓取 vs 节流
# ===========================================================================
class TestRunOnceCadence:
    def test_f1_f4_every_run_and_f2_f3_f5_throttled(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        _stamp_throttles()

        endpoints = [FakeEndpoint(k) for k in ("F1", "F4", "F2", "F3", "F5")]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert report.ran == ["F1", "F4"]
        assert report.skipped == ["F2", "F3", "F5"]
        assert report.ok is True
        assert [ep.calls for ep in endpoints] == [1, 1, 0, 0, 0]

    def test_throttled_endpoints_run_when_window_expired(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        _stamp_throttles("2026-10-04 00:00:00")  # 远超 24h

        endpoints = [FakeEndpoint(k) for k in ("F1", "F2", "F5")]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert report.ran == ["F1", "F2", "F5"]
        assert report.skipped == []

    def test_successful_throttled_run_stamps_the_gate(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1"), FakeEndpoint("F2")],
            now=NOW,
        )
        assert get_str("last_daily_elect_at") == STAMP_NOW

    def test_failed_throttled_run_still_stamps(self, tmp_db, frozen_now) -> None:
        """legacy 冻结行为：失败也打点，避免 10 分钟一次打爆学校接口。"""
        _configure()
        set_state("backfill_done", True)
        endpoints = [FakeEndpoint("F1"), FakeEndpoint("F2", error=ScrapeError("boom"))]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert "F2" in report.errors
        assert get_str("last_daily_elect_at") == STAMP_NOW

    def test_report_summary_has_no_openid(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        report = svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1", error=ScrapeError("x?openid=o-1 挂了"))],
            now=NOW,
        )
        assert "o-1" not in report.summary()
        assert "***" in report.errors["F1"]


# ===========================================================================
# 回填（一次性，全成功才置位）
# ===========================================================================
class TestBackfill:
    def test_first_run_forces_f2_and_f5_despite_throttle(self, tmp_db, frozen_now) -> None:
        _configure()
        _stamp_throttles()  # 三个节流窗口都还没过

        endpoints = [FakeEndpoint(k) for k in ("F1", "F4", "F2", "F3", "F5")]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert report.ran == ["F1", "F4", "F2", "F5"]  # F3 仍被节流
        assert get_bool("backfill_done") is True
        # 回填成功后顺手打点，避免下一轮立刻重复抓取
        assert get_str("last_daily_elect_at") == STAMP_NOW
        assert get_str("last_pay_at") == STAMP_NOW

    def test_partial_failure_keeps_the_flag_unset(self, tmp_db, frozen_now) -> None:
        """🔑 Round 39：F2 失败 → ``backfill_done`` 不置位，下轮整批重试。"""
        _configure()
        endpoints = [
            FakeEndpoint("F1"),
            FakeEndpoint("F2", error=ScrapeError("boom")),
            FakeEndpoint("F5"),
        ]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert report.ok is True  # F1 成功 → 核心数据没问题
        assert get_bool("backfill_done") is False
        assert get_str("last_daily_elect_at") == ""  # 失败不打点 → 立即重试
        assert get_str("last_pay_at") == STAMP_NOW  # 成功的那个正常打点

    def test_not_rerun_once_flag_is_set(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        _stamp_throttles()

        endpoints = [FakeEndpoint("F1"), FakeEndpoint("F2"), FakeEndpoint("F5")]
        report = svc.run_once(client=FakeClient(), endpoints=endpoints, now=NOW)

        assert report.skipped == ["F2", "F5"]
        assert [ep.calls for ep in endpoints] == [1, 0, 0]

    def test_fetch_only_never_sets_the_flag(self, tmp_db, frozen_now) -> None:
        _configure()
        svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1")],
            fetch_only=True,
            now=NOW,
        )
        assert get_bool("backfill_done") is False


# ===========================================================================
# stale 降级
# ===========================================================================
class TestStaleDegrade:
    def test_first_success_sets_ok_and_stamp(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        svc.run_once(client=FakeClient(), endpoints=[FakeEndpoint("F1")], now=NOW)

        assert get_str("last_scrape_status") == svc.STATUS_OK
        assert get_str("last_scrape_at") == STAMP_NOW

    def test_failure_degrades_to_stale_and_keeps_last_success(
        self, tmp_db, frozen_now
    ) -> None:
        """🔑 抓取失败降级为 stale —— 旧数据继续可用，成功时间不变。"""
        _configure()
        set_state("backfill_done", True)
        set_state("last_scrape_at", "2026-10-06 11:00:00")

        report = svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1", error=ScrapeError("boom"))],
            now=NOW,
        )

        assert report.ok is False
        assert get_str("last_scrape_status") == svc.STATUS_STALE
        assert get_str("last_scrape_at") == "2026-10-06 11:00:00"

    def test_gap_over_threshold_is_stale(self, tmp_db, frozen_now) -> None:
        set_state("last_scrape_at", "2026-10-06 09:00:00")  # 3h > 默认 2h
        assert svc.is_stale(now=NOW) is True
        assert svc.stale_gap_seconds(now=NOW) == pytest.approx(3 * 3600)

    def test_stale_hours_config_is_respected(self, tmp_db, frozen_now) -> None:
        set_many({"stale_hours": 8})
        set_state("last_scrape_at", "2026-10-06 09:00:00")
        assert svc.is_stale(now=NOW) is False

    def test_never_scraped_is_not_stale(self, tmp_db, frozen_now) -> None:
        assert svc.stale_gap_seconds(now=NOW) is None
        assert svc.is_stale(now=NOW) is False

    def test_success_after_stale_resets_to_ok(self, tmp_db, frozen_now) -> None:
        _configure()
        set_state("backfill_done", True)
        set_state("last_scrape_at", "2026-10-06 05:00:00")

        svc.run_once(client=FakeClient(), endpoints=[FakeEndpoint("F1")], now=NOW)

        assert get_str("last_scrape_status") == svc.STATUS_OK
        assert get_str("last_scrape_at") == STAMP_NOW


class TestNotifyWiring:
    """M3 接线：run_once 必须按 legacy 顺序调用推送钩子，且失败不影响抓取。"""

    def test_hooks_run_in_legacy_order(self, tmp_db, frozen_now, monkeypatch) -> None:
        _configure()
        set_state("backfill_done", True)
        calls: list[tuple] = []
        hooks = {
            name: (lambda *args, name=name: calls.append((name, args)))
            for name in ("stale", "low_battery", "violations", "offline")
        }
        monkeypatch.setattr(svc, "_notify_hooks", lambda: hooks)

        svc.run_once(client=FakeClient(), endpoints=[FakeEndpoint("F1")], now=NOW)

        assert [name for name, _ in calls] == [
            "stale",  # 抓取**之前**
            "low_battery",
            "violations",
            "offline",
        ]
        assert calls[0][1] == ()
        assert calls[1][1] == ()
        assert calls[2][1] == ("room-1",)  # 违规 / 离线需要 room_id
        assert calls[3][1] == ("room-1",)

    def test_fetch_only_skips_every_push(self, tmp_db, frozen_now, monkeypatch) -> None:
        _configure()
        calls: list[tuple] = []
        monkeypatch.setattr(
            svc,
            "_notify_hooks",
            lambda: {"stale": lambda *a: calls.append(a)},
        )
        svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1")],
            fetch_only=True,
            now=NOW,
        )
        assert calls == []

    def test_notify_false_keeps_data_but_skips_push(
        self, tmp_db, frozen_now, monkeypatch
    ) -> None:
        """E13 手动刷新：**落库但不推送**。

        为什么这条重要：用户在页面上点一下「刷新」，不该让群里收到一条
        L1 低电 / 违规告警 —— 那是自动巡检的职责，不是手动操作。
        但也**不能**退化成 ``fetch_only``（那个连库都不写），否则刷新完
        页面还是旧数据，用户会以为按钮坏了。
        """
        _configure()
        calls: list[tuple] = []
        monkeypatch.setattr(
            svc,
            "_notify_hooks",
            lambda: {"stale": lambda *a: calls.append(a)},
        )

        report = svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1")],
            notify=False,
            now=NOW,
        )

        assert calls == []  # 没推送
        assert report.ok is True
        assert get_str("last_scrape_status") == svc.STATUS_OK  # 但状态写了
        assert get_str("last_room_id") == "room-1"  # 房间号也发布了

    def test_hook_exception_never_breaks_the_scrape(
        self, tmp_db, frozen_now, monkeypatch
    ) -> None:
        """K7：推送是「单点」，失败不得影响抓取主流程。"""
        _configure()
        set_state("backfill_done", True)

        def _boom(*_args):
            raise RuntimeError("webhook 挂了")

        monkeypatch.setattr(
            svc,
            "_notify_hooks",
            lambda: dict.fromkeys(
                ("stale", "low_battery", "violations", "offline"), _boom
            ),
        )
        report = svc.run_once(
            client=FakeClient(), endpoints=[FakeEndpoint("F1")], now=NOW
        )

        assert report.ok is True
        assert get_str("last_scrape_status") == svc.STATUS_OK


# ===========================================================================
# openid 注入 / 房间发现
# ===========================================================================
class TestOpenidAndDiscovery:
    def test_missing_openid_raises_and_marks_error(self, tmp_db, frozen_now) -> None:
        with pytest.raises(ScrapeError, match="openid"):
            svc.run_once(client=FakeClient(), endpoints=[], now=NOW)
        assert get_str("last_scrape_status") == svc.STATUS_ERROR

    def test_room_discovered_from_finduser_page(self, tmp_db, frozen_now) -> None:
        _configure(dorm_room_id="")
        html = '<input type="hidden" id="roomId" value="uuid-9">'
        client = FakeClient(finduser_html=html)

        report = svc.run_once(
            client=client, endpoints=[FakeEndpoint("F1")], now=NOW
        )

        assert report.room_id == "uuid-9"
        assert get_str("last_room_id") == "uuid-9"
        assert client.calls[0][0] == "GET"
        assert FINDUSER_PATH in client.calls[0][1]

    def test_room_id_config_skips_discovery(self, tmp_db, frozen_now) -> None:
        _configure()
        client = FakeClient()
        svc.run_once(client=client, endpoints=[FakeEndpoint("F1")], now=NOW)
        assert all(call[0] == "POST" for call in client.calls)

    def test_discovery_failure_raises_and_marks_error(self, tmp_db, frozen_now) -> None:
        _configure(dorm_room_id="")
        with pytest.raises(ScrapeError, match="roomId"):
            svc.run_once(client=FakeClient(), endpoints=[], now=NOW)
        assert get_str("last_scrape_status") == svc.STATUS_ERROR

    def test_openid_is_appended_to_the_url(self) -> None:
        ctx = svc.ScrapeContext(client=FakeClient(), openid="o-1", room_id="r")
        assert ctx.url("/x") == "/x?openid=o-1"
        assert ctx.url("/x?a=1") == "/x?a=1&openid=o-1"

    def test_openid_is_percent_encoded(self) -> None:
        """``&`` / ``#`` 出现在凭据里时不能破坏 URL 结构。"""
        ctx = svc.ScrapeContext(client=FakeClient(), openid="a&b", room_id="r")
        assert ctx.url("/x") == "/x?openid=a%26b"

    def test_room_id_is_passed_to_endpoints(self, tmp_db, frozen_now) -> None:
        _configure()
        seen: list[str] = []

        class _CtxProbe(FakeEndpoint):
            def fetch(self, ctx):
                seen.append(ctx.room_id)
                return super().fetch(ctx)

        svc.run_once(client=FakeClient(), endpoints=[_CtxProbe("F1")], now=NOW)
        assert seen == ["room-1"]


# ===========================================================================
# fetch_only —— 零副作用冒烟
# ===========================================================================
class TestFetchOnly:
    def test_no_persist_and_no_state_writes(self, tmp_db, frozen_now) -> None:
        _configure()
        endpoints = [
            FakeEndpoint("F1", rows=[{"ts": STAMP_NOW}]),
            FakeEndpoint("F2", rows=[{"dt": "2026-10-06"}]),
        ]
        report = svc.run_once(
            client=FakeClient(), endpoints=endpoints, fetch_only=True, now=NOW
        )

        assert report.ran == ["F1", "F2"]  # 全部抓了
        assert all(ep.persisted is None for ep in endpoints)  # 一个都没落库
        assert get_str("last_scrape_at") == ""
        assert get_str("last_room_id") == ""
        assert get_str("last_daily_elect_at") == ""

    def test_ok_reflects_f1(self, tmp_db, frozen_now) -> None:
        _configure()
        report = svc.run_once(
            client=FakeClient(),
            endpoints=[FakeEndpoint("F1", error=ScrapeError("boom"))],
            fetch_only=True,
            now=NOW,
        )
        assert report.ok is False


class TestReportOk:
    def test_ok_requires_f1_to_have_run(self, tmp_db, frozen_now) -> None:
        """F1 是核心数据 —— 它没跑（或缺席）就不算成功。"""
        _configure()
        set_state("backfill_done", True)
        report = svc.run_once(
            client=FakeClient(), endpoints=[FakeEndpoint("F4")], now=NOW
        )
        assert report.ok is False
        assert get_str("last_scrape_status") == svc.STATUS_STALE
        assert get_str("last_scrape_at") == ""


# ===========================================================================
# 与**真实端点**联跑（假 client）—— 守住两条硬约束
# ===========================================================================
class TestRealEndpointContract:
    def test_f1_never_writes_daily_elec(self, tmp_db, frozen_now) -> None:
        """📌 F1 的 ``useEq`` 是「装机以来累计」，**不得**进 ``daily_elec``。"""
        from starwatt.db.repositories import DailyElecRepo, RecordRepo

        _configure()
        set_state("backfill_done", True)
        _stamp_throttles()  # 让 F2/F3/F5 都跳过 → 只剩 F1 写库

        payloads = {
            PATH_DATA: {
                "status": "0",
                "remainEq": 42.5,
                "dt": "2026-10-06 11:00:00",
                "useEq": 999.0,  # ← 绝不能落进 daily_elec
            }
        }
        report = svc.run_once(client=FakeClient(payloads=payloads), now=NOW)

        assert report.ran == ["F1", "F4"]
        assert DailyElecRepo.recent("room-1", days=365) == []
        latest = RecordRepo.latest()
        assert latest is not None and latest.remain == 42.5

    def test_f2_is_the_daily_elec_source(self, tmp_db, frozen_now) -> None:
        from starwatt.db.repositories import DailyElecRepo

        _configure()
        set_state("backfill_done", True)
        payloads = {
            PATH_DAY_ELECT: {
                "status": "0",
                "rows": [{"dt": "2026-10-06", "esbm": 1.0, "eebm": 30.83}],
            }
        }
        report = svc.run_once(client=FakeClient(payloads=payloads), now=NOW)

        assert "F2" in report.ran
        rows = DailyElecRepo.recent("room-1", days=365)
        assert len(rows) == 1
        assert rows[0].zong_eq == 30.83  # eebm 优先

    def test_injected_client_is_not_closed(self, tmp_db, frozen_now) -> None:
        _configure()
        client = FakeClient()
        svc.run_once(client=client, endpoints=[FakeEndpoint("F1")], now=NOW)
        assert client.closed is False

    def test_own_client_is_closed(self, tmp_db, frozen_now, monkeypatch) -> None:
        """自己构造的 client 必须关掉（否则长期运行累积 fd）。"""
        _configure()
        client = FakeClient()
        monkeypatch.setattr(svc, "build_client", lambda: client)
        svc.run_once(endpoints=[FakeEndpoint("F1")], now=NOW)
        assert client.closed is True
