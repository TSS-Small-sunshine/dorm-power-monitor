"""``starwatt.scheduler`` —— 进程内调度（Q14 + B3）。

调度器是「静默失败」的重灾区（多 worker 重复抓、preload 后线程丢失、
job 抛异常拖垮线程），所以这里逐条钉住：

* 任务确实注册了、间隔来自**配置**（Q15 可改）
* 单实例 / 合并 / misfire 三个默认值符合设计（替代旧 flock）
* ``scrape_job`` **吞掉**异常（线程级兜底）
"""
from __future__ import annotations

from starwatt import scheduler as sch
from starwatt.config_registry import set_many


class _LogRecorder:
    """最小 logger 替身（记录 warning / exception 调用）。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def warning(self, msg, *args, **_kwargs) -> None:
        self.calls.append(("warning", msg % args if args else msg))

    def exception(self, msg, *args, **_kwargs) -> None:
        self.calls.append(("exception", msg % args if args else msg))

    def info(self, *_args, **_kwargs) -> None:  # pragma: no cover - 记录不到也无需断言
        pass

    def debug(self, *_args, **_kwargs) -> None:  # pragma: no cover
        pass


class TestBuildScheduler:
    def test_registers_scrape_and_l3_gate(self, tmp_db) -> None:
        sched = sch.build_scheduler()
        assert [job.id for job in sched.get_jobs()] == [sch.JOB_SCRAPE, sch.JOB_L3_GATE]

    def test_l3_gate_runs_every_minute(self, tmp_db) -> None:
        sched = sch.build_scheduler()
        gate = next(job for job in sched.get_jobs() if job.id == sch.JOB_L3_GATE)
        fields = {f.name: str(f) for f in gate.trigger.fields}
        assert fields["minute"] == "*"
        assert fields["second"] == "0"

    def test_interval_comes_from_config(self, tmp_db) -> None:
        set_many({"scrape_interval_sec": 1200})
        sched = sch.build_scheduler()
        job = sched.get_jobs()[0]
        assert job.trigger.interval.total_seconds() == 1200

    def test_interval_is_floored(self, tmp_db) -> None:
        """配置下限是 60s（注册表校验 60..86400），这里再兜一层。"""
        set_many({"scrape_interval_sec": 60})
        sched = sch.build_scheduler()
        job = sched.get_jobs()[0]
        assert job.trigger.interval.total_seconds() == sch.MIN_INTERVAL_SEC

    def test_job_defaults_match_the_design(self, tmp_db) -> None:
        """📌 APScheduler 3.x 不在 Job 上暴露生效后的默认值（它们在调度时才应用），
        所以直接断言调度器持有的默认值表；``requirements.txt`` 已锁 ``<4.0``。"""
        sched = sch.build_scheduler()
        assert sched._job_defaults == {
            "max_instances": 1,  # 防重入（替代旧 flock）
            "coalesce": True,
            "misfire_grace_time": sch.MISFIRE_GRACE_SEC,
        }

    def test_timezone_is_cst(self, tmp_db) -> None:
        """Q10：与 timeutil 同一时区，不受服务器 TZ 影响。"""
        from starwatt.timeutil import CST

        sched = sch.build_scheduler()
        assert str(sched.timezone) == str(CST) == "Asia/Shanghai"

    def test_build_does_not_start(self, tmp_db) -> None:
        """启动必须在 worker 内由 ``web.py`` 负责（B3 防线 2/3）。"""
        sched = sch.build_scheduler()
        assert sched.running is False

    def test_missing_db_falls_back_to_default_interval(self, settings_override) -> None:
        """``meta`` 表还不存在时（首次启动）必须回落默认间隔，而不是起不来。"""
        sched = sch.build_scheduler()
        job = sched.get_jobs()[0]
        assert job.trigger.interval.total_seconds() == sch.DEFAULT_INTERVAL_SEC


class TestScrapeJob:
    def test_swallows_exceptions(self, monkeypatch) -> None:
        """线程级兜底：run_once 抛异常时 job 不能把异常放出去（RK4）。"""
        import starwatt.scraper.service as svc

        def _boom(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(svc, "run_once", _boom)
        sch.scrape_job()  # 不应抛

    def test_scrape_error_logs_warning_not_traceback(self, monkeypatch) -> None:
        """配置问题（未配置 openid）→ WARNING，不给用户看 traceback。"""
        import starwatt.scraper.service as svc
        from starwatt.scraper.client import ScrapeError

        recorder = _LogRecorder()
        monkeypatch.setattr(sch, "logger", recorder)

        def _boom(**_kwargs):
            raise ScrapeError("未配置 dorm_openid")

        monkeypatch.setattr(svc, "run_once", _boom)
        sch.scrape_job()

        assert [kind for kind, _ in recorder.calls] == ["warning"]

    def test_unexpected_error_logs_traceback(self, monkeypatch) -> None:
        import starwatt.scraper.service as svc

        recorder = _LogRecorder()
        monkeypatch.setattr(sch, "logger", recorder)

        def _boom(**_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(svc, "run_once", _boom)
        sch.scrape_job()

        assert [kind for kind, _ in recorder.calls] == ["exception"]

    def test_calls_run_once_exactly_once(self, monkeypatch) -> None:
        import starwatt.scraper.service as svc
        from starwatt.scraper.service import ScrapeReport

        calls: list[dict] = []

        def _fake(**kwargs):
            calls.append(kwargs)
            return ScrapeReport(room_id="room-1", ok=True)

        monkeypatch.setattr(svc, "run_once", _fake)
        sch.scrape_job()

        assert len(calls) == 1
        assert calls[0] == {}  # 定时任务走全部默认值（不落库、不 fetch_only）

    def test_failure_report_is_not_raised(self, monkeypatch) -> None:
        import starwatt.scraper.service as svc
        from starwatt.scraper.service import ScrapeReport

        monkeypatch.setattr(
            svc, "run_once", lambda **_kw: ScrapeReport(room_id="room-1", ok=False)
        )
        sch.scrape_job()  # 不应抛


class TestL3GateJob:
    def test_pushes_all_three_kinds(self, monkeypatch) -> None:
        import starwatt.notify.policies as notify_policies

        pushed: list[str] = []
        monkeypatch.setattr(
            notify_policies, "push_l3_if_due", lambda kind, **_kw: pushed.append(kind)
        )
        sch.l3_gate_job()
        assert pushed == ["daily", "weekly", "monthly"]

    def test_swallows_per_kind_failure(self, monkeypatch) -> None:
        """某一个报表失败不能影响另外两个，也不能把调度线程弄死。"""
        import starwatt.notify.policies as notify_policies

        pushed: list[str] = []

        def _maybe(kind, **_kw):
            if kind == "weekly":
                raise RuntimeError("boom")
            pushed.append(kind)

        monkeypatch.setattr(notify_policies, "push_l3_if_due", _maybe)
        sch.l3_gate_job()
        assert pushed == ["daily", "monthly"]

    def test_notify_layer_unavailable_is_swallowed(self, monkeypatch) -> None:
        import builtins

        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name.startswith("starwatt.notify"):
                raise ImportError("no notify")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _boom)
        sch.l3_gate_job()  # 不应抛


class TestShutdown:
    def test_shutdown_none_is_noop(self) -> None:
        sch.shutdown_scheduler(None)  # 不应抛

    def test_shutdown_running_scheduler(self, tmp_db) -> None:
        sched = sch.build_scheduler()
        sched.start()
        assert sched.running is True
        sch.shutdown_scheduler(sched)
        assert sched.running is False

    def test_shutdown_is_idempotent(self, tmp_db) -> None:
        sched = sch.build_scheduler()
        sched.start()
        sch.shutdown_scheduler(sched)
        sch.shutdown_scheduler(sched)  # 第二次不应抛


def test_interval_config_key_is_registered(tmp_db) -> None:
    from starwatt.config_registry import REGISTRY

    assert "scrape_interval_sec" in REGISTRY
