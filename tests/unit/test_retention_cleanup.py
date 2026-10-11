"""保留期清理 job（🩺 审计 stability 缺陷：数据只涨不删）。

修复前 ``delete_before`` / ``purge_expired`` / ``purge_before`` 三个删除函数
齐全但**没有任何生产调用者**，``VACUUM`` / ``wal_checkpoint`` 全仓库命中 0 次。

这里逐条钉住新 job 的语义：

* 只删**早于**保留窗口的数据，窗口内的数据必须原样保留
* ``retention_enabled=false`` → 一行都不删（可关闭）
* 过期会话 / 失败登录记录也在同一次清理里收掉
* WAL 回收每次都跑；VACUUM **按累计删除行数阈值**触发，不是每天跑
* 任何一步抛错都**不外抛**（调度线程与其它 job 必须活着）
"""
from __future__ import annotations

from datetime import datetime, timedelta

from starwatt import scheduler as sch
from starwatt.config_registry import get_int, set_many
from starwatt.db import repositories as repo
from starwatt.db.models import Record
from starwatt.timeutil import to_stamp

NOW = datetime(2026, 10, 6, 9, 0, 0)


def _freeze(monkeypatch) -> None:
    """把「现在」钉死在 NOW（scheduler 与 dashboard_service 两条路径都钉）。"""
    from starwatt.services import dashboard_service

    monkeypatch.setattr(sch, "now_cst", lambda: NOW)
    monkeypatch.setattr(dashboard_service.timeutil, "now_cst", lambda: NOW)


def _insert(ts: str, remain: float = 50.0) -> None:
    repo.RecordRepo.insert(Record(ts=ts, read_time=None, remain=remain))


def _timestamps() -> list[str]:
    return [r.ts for r in repo.RecordRepo.query(hours=24 * 365 * 5)]


def _user_id() -> int:
    """建一个用户 —— ``sessions.user_id`` 有外键约束（``foreign_keys=ON``）。"""
    return repo.UserRepo.create("tester", "x" * 32).id


class TestRetentionWindow:
    def test_deletes_only_outside_the_window(self, tmp_db, monkeypatch) -> None:
        """🔑 核心验收：窗口外的删掉，窗口内的留着。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": True, "records_retention_days": 30})

        _insert(to_stamp(NOW - timedelta(days=45)))  # 窗外 → 删
        _insert(to_stamp(NOW - timedelta(days=31)))  # 窗外 → 删
        _insert(to_stamp(NOW - timedelta(days=29)))  # 窗内 → 留
        _insert(to_stamp(NOW - timedelta(days=1)))  # 窗内 → 留
        _insert(to_stamp(NOW))  # 此刻 → 留

        sch.maintenance_job()

        assert _timestamps() == [
            to_stamp(NOW - timedelta(days=29)),
            to_stamp(NOW - timedelta(days=1)),
            to_stamp(NOW),
        ]

    def test_boundary_row_is_kept(self, tmp_db, monkeypatch) -> None:
        """``ts == cutoff`` 必须保留（判定是严格的 ``ts < cutoff``）。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": True, "records_retention_days": 30})

        _insert(to_stamp(NOW - timedelta(days=30)))  # 正好在边界上 → 留
        _insert(to_stamp(NOW - timedelta(days=30, seconds=1)))  # 差一秒 → 删

        sch.maintenance_job()

        assert _timestamps() == [to_stamp(NOW - timedelta(days=30))]

    def test_disabled_flag_deletes_nothing(self, tmp_db, monkeypatch) -> None:
        """可关闭：``retention_enabled=false`` → 一行都不删。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": False, "records_retention_days": 30})

        _insert(to_stamp(NOW - timedelta(days=999)))

        sch.maintenance_job()

        assert _timestamps() == [to_stamp(NOW - timedelta(days=999))]

    def test_retention_days_is_honoured(self, tmp_db, monkeypatch) -> None:
        """保留天数来自配置（不是硬编码）：改成 365 天后 90 天前的记录不再被删。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": True, "records_retention_days": 365})

        _insert(to_stamp(NOW - timedelta(days=90)))
        _insert(to_stamp(NOW - timedelta(days=400)))

        sch.maintenance_job()

        assert _timestamps() == [to_stamp(NOW - timedelta(days=90))]


class TestOtherTables:
    def test_expired_sessions_are_purged(self, tmp_db, monkeypatch) -> None:
        _freeze(monkeypatch)
        set_many({"retention_enabled": True})

        uid = _user_id()
        repo.SessionRepo.create(uid, "hash-old", to_stamp(NOW - timedelta(days=1)))
        repo.SessionRepo.create(uid, "hash-new", to_stamp(NOW + timedelta(days=1)))
        assert repo.SessionRepo.count() == 2

        sch.maintenance_job()

        assert repo.SessionRepo.count() == 1

    def test_old_failed_attempts_are_purged(self, tmp_db, monkeypatch) -> None:
        _freeze(monkeypatch)
        set_many({"retention_enabled": True, "failed_attempts_retention_days": 30})

        repo.FailedAttemptRepo.record("bob", "10.0.0.1", to_stamp(NOW - timedelta(days=90)))
        repo.FailedAttemptRepo.record("alice", "10.0.0.2")

        sch.maintenance_job()

        with repo.connect() as conn:
            left = [
                r["username"]
                for r in conn.execute("SELECT username FROM failed_attempts")
            ]
        assert left == ["alice"]


class TestVacuumPolicy:
    def test_wal_checkpoint_runs_on_every_cleanup(self, tmp_db, monkeypatch) -> None:
        """WAL 回收代价极小 → 每次清理都跑。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": True})

        calls: list[str] = []
        monkeypatch.setattr(repo, "wal_checkpoint", lambda mode="TRUNCATE": calls.append(mode))

        sch.maintenance_job()

        assert calls == ["TRUNCATE"]

    def test_vacuum_not_run_below_threshold(self, tmp_db, monkeypatch) -> None:
        """📌 VACUUM 不每天跑：删除行数没到阈值就跳过。"""
        _freeze(monkeypatch)
        set_many(
            {"retention_enabled": True, "records_retention_days": 30,
             "vacuum_min_deleted_rows": 1000}
        )
        _insert(to_stamp(NOW - timedelta(days=45)))

        called: list[int] = []
        monkeypatch.setattr(repo, "vacuum", lambda: called.append(1))

        sch.maintenance_job()

        assert called == []
        assert get_int("vacuum_pending_rows", -1) == 1  # 计数累计

    def test_vacuum_runs_at_threshold_and_resets_counter(self, tmp_db, monkeypatch) -> None:
        _freeze(monkeypatch)
        set_many(
            {"retention_enabled": True, "records_retention_days": 30,
             "vacuum_min_deleted_rows": 3}
        )
        for days in (45, 46, 47, 48):
            _insert(to_stamp(NOW - timedelta(days=days)))

        called: list[int] = []
        monkeypatch.setattr(repo, "vacuum", lambda: called.append(1))

        sch.maintenance_job()

        assert called == [1]
        assert get_int("vacuum_pending_rows", -1) == 0

    def test_pending_rows_accumulate_across_runs(self, tmp_db, monkeypatch) -> None:
        """计数跨运行累计：两次各删 1 行，阈值 2 → 第二次才 VACUUM。"""
        _freeze(monkeypatch)
        set_many(
            {"retention_enabled": True, "records_retention_days": 30,
             "vacuum_min_deleted_rows": 2}
        )

        called: list[int] = []
        monkeypatch.setattr(repo, "vacuum", lambda: called.append(1))

        _insert(to_stamp(NOW - timedelta(days=45)))
        sch.maintenance_job()
        assert called == []

        _insert(to_stamp(NOW - timedelta(days=46)))
        sch.maintenance_job()
        assert called == [1]
        assert get_int("vacuum_pending_rows", -1) == 0

    def test_vacuum_failure_is_swallowed(self, tmp_db, monkeypatch) -> None:
        """VACUUM 拿到锁失败 → 只记日志，绝不外抛。"""
        _freeze(monkeypatch)
        set_many(
            {"retention_enabled": True, "records_retention_days": 30,
             "vacuum_min_deleted_rows": 1}
        )
        _insert(to_stamp(NOW - timedelta(days=45)))

        def _boom() -> None:
            raise RuntimeError("database is locked")

        monkeypatch.setattr(repo, "vacuum", _boom)

        sch.maintenance_job()  # 不应抛


class TestFailureIsolation:
    def test_cleanup_error_is_swallowed(self, tmp_db, monkeypatch) -> None:
        """线程级兜底：任何一步炸掉都不能把调度线程弄死（RK4）。

        把 ``now_cst`` 换成会抛异常的版本 —— ``cleanup_before`` 与
        ``purge_before`` 都会踩到，job 仍须正常返回。
        """

        def _boom():
            raise RuntimeError("clock exploded")

        monkeypatch.setattr(sch, "now_cst", _boom)
        from starwatt.services import dashboard_service

        monkeypatch.setattr(dashboard_service.timeutil, "now_cst", _boom)

        sch.maintenance_job()  # 不应抛

    def test_records_failure_does_not_skip_sessions(self, tmp_db, monkeypatch) -> None:
        """逐项独立兜底：records 清理失败时，会话清理照样跑。"""
        _freeze(monkeypatch)
        set_many({"retention_enabled": True})

        from starwatt.services import dashboard_service

        def _boom(_days):
            raise RuntimeError("records table gone")

        monkeypatch.setattr(dashboard_service, "cleanup_before", _boom)
        repo.SessionRepo.create(_user_id(), "hash-old", to_stamp(NOW - timedelta(days=1)))

        sch.maintenance_job()

        assert repo.SessionRepo.count() == 0
