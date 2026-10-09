"""``starwatt.db`` 数据层单元测试。

覆盖
====

* schema 契约（10 表 / 11 显式索引 / ``users`` 的 2 个新列）
* 迁移（幂等 / 老库升级 / 版本号）
* Repository 语义（REPLACE / upsert / 类型化 meta / sha256 会话）
* L22 修复（时间窗口用 CST 而非 SQL UTC）
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest

from starwatt import db, timeutil


# ===========================================================================
# schema 契约
# ===========================================================================
class TestSchemaContract:
    def test_exactly_ten_tables(self, tmp_db) -> None:
        with db.connect() as conn:
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert names == set(db.TABLE_NAMES)

    def test_eleven_explicit_indexes(self, tmp_db) -> None:
        with db.connect() as conn:
            names = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            }
        assert names == set(db.INDEX_NAMES)
        assert len(names) == 11

    def test_users_gains_exactly_two_columns(self, tmp_db) -> None:
        """Q9 / D2：唯一有意的 schema delta。"""
        with db.connect() as conn:
            cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        assert cols == [
            "id",
            "username",
            "password_hash",
            "role",
            "created_at",
            "last_login_at",
            "disabled",
            "must_change_password",
        ]

    def test_legacy_tables_unchanged(self, tmp_db) -> None:
        """``records`` / ``daily_elec`` 等列名必须与旧库完全一致（零迁移）。"""
        expected = {
            "records": ["id", "ts", "read_time", "remain"],
            "daily_elec": ["roomId", "dt", "total_eq", "esbm", "eebm", "zong_eq"],
            "violations": ["roomId", "dt", "wg_reason", "wg_power"],
            "pay_history": ["roomId", "dt", "pay_type", "fee_type", "money"],
            "meta": ["key", "value"],
        }
        with db.connect() as conn:
            for table, cols in expected.items():
                actual = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
                assert actual == cols, f"{table} 列名漂移"


# ===========================================================================
# 迁移
# ===========================================================================
class TestMigrations:
    def test_fresh_db_reaches_target_version(self, tmp_db) -> None:
        with db.connect() as conn:
            assert db.current_version(conn) == db.SCHEMA_VERSION

    def test_init_is_idempotent(self, tmp_db) -> None:
        db.init()
        db.init()
        db.init()
        with db.connect() as conn:
            assert db.current_version(conn) == db.SCHEMA_VERSION
        assert db.RecordRepo.count() == 0

    def test_migration_adds_columns_to_legacy_db(self, tmp_path) -> None:
        """模拟「老库」：users 只有 6 列 → migrate 后补上 2 列。"""
        from starwatt import config

        legacy_file = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(legacy_file), isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.executescript(
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE users ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  username TEXT NOT NULL UNIQUE,"
            "  password_hash TEXT NOT NULL,"
            "  role TEXT NOT NULL DEFAULT 'viewer',"
            "  created_at TEXT NOT NULL DEFAULT (datetime('now','+8 hours')),"
            "  last_login_at TEXT);"
            "INSERT INTO meta (key, value) VALUES ('schema_version', '1');"
        )
        before = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        assert "disabled" not in before

        version = db.migrate(conn)
        after = [r["name"] for r in conn.execute("PRAGMA table_info(users)")]
        conn.close()

        assert version == db.SCHEMA_VERSION
        assert "disabled" in after
        assert "must_change_password" in after
        assert config is not None  # 保证 config 已被导入（fixture 依赖）

    def test_has_column_helper(self, tmp_db) -> None:
        with db.connect() as conn:
            assert db.migrations.has_column(conn, "users", "disabled") is True
            assert db.migrations.has_column(conn, "users", "nope") is False


# ===========================================================================
# RecordRepo —— REPLACE 语义 + L22 时间窗口
# ===========================================================================
class TestRecordRepo:
    def test_same_ts_replaces(self, tmp_db) -> None:
        """🔑 records.ts UNIQUE + INSERT OR REPLACE：同秒两次插入不增行。"""
        ts = "2026-10-06 12:00:00"
        db.RecordRepo.insert(db.Record(ts=ts, read_time=ts, remain=42.0))
        db.RecordRepo.insert(db.Record(ts=ts, read_time=ts, remain=43.0))
        assert db.RecordRepo.count() == 1
        latest = db.RecordRepo.latest()
        assert latest is not None and latest.remain == 43.0

    def test_latest_returns_none_when_empty(self, tmp_db) -> None:
        assert db.RecordRepo.latest() is None

    def test_query_window_is_cst_not_utc(self, tmp_db) -> None:
        """🔑 L22 修复：``hours=24`` 的窗口按 **CST** 算，不是 SQL 的 UTC。

        构造 3 个点（相对 CST 现在）：
          * 1 小时前  → 必须在窗口内
          * 23 小时前 → 必须在窗口内
          * 25 小时前 → 必须**在窗口外**（旧代码的 UTC 比较会错误纳入）
        """
        now = timeutil.now_cst()
        inside_new = timeutil.to_stamp(now - timedelta(hours=1))
        inside_old = timeutil.to_stamp(now - timedelta(hours=23))
        outside = timeutil.to_stamp(now - timedelta(hours=25))
        for ts in (inside_new, inside_old, outside):
            db.RecordRepo.insert(db.Record(ts=ts, read_time=ts, remain=1.0))

        got = {r.ts for r in db.RecordRepo.query(hours=24)}
        assert inside_new in got
        assert inside_old in got
        assert outside not in got, "25 小时前的数据不应出现在 24h 窗口（L22）"

    def test_query_explicit_range_beats_hours(self, tmp_db) -> None:
        for ts in ("2026-10-01 00:00:00", "2026-10-05 00:00:00"):
            db.RecordRepo.insert(db.Record(ts=ts, read_time=ts, remain=1.0))
        got = db.RecordRepo.query(
            hours=24, start_dt="2026-10-04 00:00:00", end_dt="2026-10-06 00:00:00"
        )
        assert [r.ts for r in got] == ["2026-10-05 00:00:00"]

    def test_delete_before(self, tmp_db) -> None:
        db.RecordRepo.insert(db.Record(ts="2026-01-01 00:00:00", remain=1.0))
        db.RecordRepo.insert(db.Record(ts="2026-10-06 00:00:00", remain=1.0))
        assert db.RecordRepo.delete_before("2026-06-01 00:00:00") == 1
        assert db.RecordRepo.count() == 1


# ===========================================================================
# MetaRepo
# ===========================================================================
class TestMetaRepo:
    def test_get_missing_returns_none(self, tmp_db) -> None:
        assert db.MetaRepo.get("nope") is None

    def test_set_and_get(self, tmp_db) -> None:
        db.MetaRepo.set("eqprice", "0.5")
        assert db.MetaRepo.get("eqprice") == "0.5"

    def test_typed_accessors(self, tmp_db) -> None:
        db.MetaRepo.set("eqprice", "0.5000")
        db.MetaRepo.set("count", "7")
        db.MetaRepo.set("flag", "1")
        assert db.MetaRepo.get_float("eqprice") == 0.5
        assert db.MetaRepo.get_int("count") == 7
        assert db.MetaRepo.get_bool("flag") is True
        assert db.MetaRepo.get_bool("missing") is False
        assert db.MetaRepo.get_bool("missing", default=True) is True

    def test_typed_accessors_fall_back_to_default(self, tmp_db) -> None:
        db.MetaRepo.set("bad", "not-a-number")
        assert db.MetaRepo.get_float("bad", 1.5) == 1.5
        assert db.MetaRepo.get_int("bad", 9) == 9

    def test_set_many_and_all(self, tmp_db) -> None:
        db.MetaRepo.set_many({"a": 1, "b": "x", "c": None})
        all_meta = db.MetaRepo.all()
        assert all_meta["a"] == "1"
        assert all_meta["b"] == "x"
        assert all_meta["c"] == ""

    def test_delete(self, tmp_db) -> None:
        db.MetaRepo.set("k", "v")
        db.MetaRepo.delete("k")
        assert db.MetaRepo.get("k") is None

    def test_entries_returns_models(self, tmp_db) -> None:
        db.MetaRepo.set("k", "v")
        entries = {e.key: e.value for e in db.MetaRepo.entries()}
        assert entries.get("k") == "v"


# ===========================================================================
# UserRepo / SessionRepo / AuditRepo / FailedAttemptRepo
# ===========================================================================
class TestUserRepo:
    def test_create_and_fetch(self, tmp_db) -> None:
        user = db.UserRepo.create("admin", "hash-x", role="admin")
        assert user.id > 0
        assert user.username == "admin"
        assert user.is_admin is True
        assert user.is_active is True
        assert user.needs_password_change is False
        assert db.UserRepo.get_by_username("admin") is not None
        assert db.UserRepo.get_by_id(user.id) is not None

    def test_must_change_password_flag(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h", must_change_password=True)
        assert user.needs_password_change is True

    def test_set_disabled(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h")
        db.UserRepo.set_disabled(user.id, True)
        refreshed = db.UserRepo.get_by_id(user.id)
        assert refreshed is not None and refreshed.is_active is False

    def test_update_password_clears_must_change(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h", must_change_password=True)
        db.UserRepo.update_password(user.id, "new-hash", must_change=False)
        refreshed = db.UserRepo.get_by_id(user.id)
        assert refreshed is not None
        assert refreshed.password_hash == "new-hash"
        assert refreshed.needs_password_change is False

    def test_count_and_delete(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h")
        assert db.UserRepo.count() == 1
        db.UserRepo.delete(user.id)
        assert db.UserRepo.count() == 0

    def test_duplicate_username_raises(self, tmp_db) -> None:
        db.UserRepo.create("dup", "h")
        with pytest.raises(sqlite3.IntegrityError):
            db.UserRepo.create("dup", "h2")


class TestSessionRepo:
    def test_token_stored_as_sha256(self, tmp_db) -> None:
        """🔑 L17 修复：DB 里存 sha256，原始 token 查不到。"""
        user = db.UserRepo.create("u", "h")
        raw = "raw-session-token"
        digest = db.hash_token(raw)
        assert len(digest) == 64
        assert digest != raw

        db.SessionRepo.create(user.id, digest, "2030-01-01 00:00:00")
        assert db.SessionRepo.get_by_token_hash(digest) is not None
        assert db.SessionRepo.get_by_token_hash(raw) is None, "明文 token 不应能查到"

    def test_purge_expired(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h")
        db.SessionRepo.create(user.id, "h1", "2000-01-01 00:00:00")
        db.SessionRepo.create(user.id, "h2", "2099-01-01 00:00:00")
        assert db.SessionRepo.purge_expired() == 1
        assert db.SessionRepo.count() == 1

    def test_delete_by_user(self, tmp_db) -> None:
        user = db.UserRepo.create("u", "h")
        db.SessionRepo.create(user.id, "h1", "2099-01-01 00:00:00")
        db.SessionRepo.create(user.id, "h2", "2099-01-01 00:00:00")
        assert db.SessionRepo.delete_by_user(user.id) == 2
        assert db.SessionRepo.count() == 0


class TestAuditRepo:
    def test_append_and_recent(self, tmp_db) -> None:
        db.AuditRepo.append("login.success", user_id=1, ip="127.0.0.1", details={"a": 1})
        db.AuditRepo.append("login.failed", target="bob")
        assert db.AuditRepo.count() == 2
        rows = db.AuditRepo.recent(10)
        assert rows[0].action == "login.failed"  # 倒序
        assert rows[1].details == '{"a": 1}'

    def test_filter_by_action(self, tmp_db) -> None:
        db.AuditRepo.append("a")
        db.AuditRepo.append("b")
        assert [r.action for r in db.AuditRepo.recent(action="a")] == ["a"]


class TestFailedAttemptRepo:
    def test_record_and_count_since(self, tmp_db) -> None:
        db.FailedAttemptRepo.record("bob", "1.1.1.1", at="2026-10-07 12:00:00")
        assert db.FailedAttemptRepo.count_since("bob", "2026-10-07 00:00:00") == 1
        assert db.FailedAttemptRepo.count_since("bob", "2026-10-08 00:00:00") == 0

    def test_clear(self, tmp_db) -> None:
        db.FailedAttemptRepo.record("bob", at="2026-10-07 12:00:00")
        assert db.FailedAttemptRepo.clear("bob") == 1
        assert db.FailedAttemptRepo.count_since("bob", "2026-10-01 00:00:00") == 0

    def test_purge_before(self, tmp_db) -> None:
        db.FailedAttemptRepo.record("bob", at="2026-01-01 00:00:00")
        db.FailedAttemptRepo.record("bob", at="2026-10-07 00:00:00")
        assert db.FailedAttemptRepo.purge_before("2026-06-01 00:00:00") == 1


# ===========================================================================
# 其余 Repo
# ===========================================================================
class TestOtherRepos:
    def test_daily_elec_upsert_and_recent(self, tmp_db) -> None:
        rows = [
            {
                "dt": timeutil.today_cst().isoformat(),
                "esbm": 1.0,
                "eebm": 2.0,
                "zong_eq": 3.0,
                "total_eq": 1.0,
            }
        ]
        assert db.DailyElecRepo.upsert_many("room-1", rows) == 1
        assert db.DailyElecRepo.upsert_many("room-1", rows) == 1  # upsert 不重复
        got = db.DailyElecRepo.recent("room-1", days=7)
        assert len(got) == 1 and got[0].zong_eq == 3.0
        assert db.DailyElecRepo.latest("room-1") is not None

    def test_daily_elec_skips_rows_without_dt(self, tmp_db) -> None:
        assert db.DailyElecRepo.upsert_many("r", [{"esbm": 1.0}]) == 0

    def test_violation_upsert_and_recent(self, tmp_db) -> None:
        rows = [
            {
                "dt": timeutil.today_cst().isoformat(),
                "wg_reason": "大功率",
                "wg_power": 1200.0,
            }
        ]
        assert db.ViolationRepo.upsert_many("room-1", rows) == 1
        got = db.ViolationRepo.recent("room-1", days=30)
        assert got[0].wg_reason == "大功率" and got[0].wg_power == 1200.0

    def test_pay_upsert_and_recent(self, tmp_db) -> None:
        rows = [
            {
                "dt": timeutil.today_cst().isoformat(),
                "pay_type": "充值",
                "fee_type": "电费",
                "money": 50.0,
            }
        ]
        assert db.PayRepo.upsert_many("room-1", rows) == 1
        got = db.PayRepo.recent("room-1", days=90)
        assert got[0].money == 50.0
        assert got[0].kind == "充值" and got[0].amount == 50.0

    def test_run_status_upsert_and_get(self, tmp_db) -> None:
        db.RunStatusRepo.upsert(
            "room-1",
            {
                "vol": 220.1,
                "cur": 0.45,
                "yggl": 12.3,
                "runStatus": "在线",
                "updateDt": "2026-10-06 11:59:00",
            },
        )
        rs = db.RunStatusRepo.get("room-1")
        assert rs is not None
        assert rs.run_status == "在线" and rs.vol == 220.1
        db.RunStatusRepo.upsert("room-1", {"runStatus": "离线"})
        assert db.RunStatusRepo.get("room-1").run_status == "离线"

    def test_run_status_get_empty_room(self, tmp_db) -> None:
        assert db.RunStatusRepo.get("") is None
        assert db.RunStatusRepo.get("nope") is None


# ===========================================================================
# models
# ===========================================================================
class TestModels:
    def test_from_row_ignores_extra_columns(self, tmp_db) -> None:
        rec = db.Record.from_row(
            {"ts": "x", "read_time": "y", "remain": "1.5", "extra": 9}
        )
        assert rec.ts == "x" and rec.remain == 1.5

    def test_from_row_coerces_bad_numbers(self, tmp_db) -> None:
        rec = db.Record.from_row({"ts": "x", "remain": "not-a-number"})
        assert rec.remain is None

    def test_as_dict_roundtrip(self, tmp_db) -> None:
        rec = db.Record(ts="t", read_time="r", remain=1.0)
        assert rec.as_dict()["remain"] == 1.0

    def test_field_names(self, tmp_db) -> None:
        assert db.Record.field_names() == ("ts", "read_time", "remain", "id")

    def test_frozen(self, tmp_db) -> None:
        import dataclasses

        rec = db.Record(ts="t")
        with pytest.raises(dataclasses.FrozenInstanceError):
            rec.ts = "other"  # type: ignore[misc]

    def test_user_properties(self, tmp_db) -> None:
        u = db.User(
            id=1,
            username="a",
            password_hash="h",
            role="admin",
            created_at="t",
            disabled=0,
            must_change_password=1,
        )
        assert u.is_admin and u.is_active and u.needs_password_change

    def test_pay_aliases(self, tmp_db) -> None:
        p = db.Pay(dt="t", pay_type="充值", money=5.0)
        assert p.kind == "充值" and p.amount == 5.0

    def test_run_status_float_coercion(self, tmp_db) -> None:
        rs = db.RunStatus.from_row(
            {"roomId": "r", "vol": "220.10", "cur": "", "yggl": None}
        )
        assert rs.vol == 220.10
        assert rs.cur is None and rs.yggl is None


# ===========================================================================
# coerce
# ===========================================================================
class TestCoerce:
    def test_float(self, tmp_db) -> None:
        assert db.coerce_float("1.5") == 1.5
        assert db.coerce_float(1) == 1.0
        assert db.coerce_float(None) is None
        assert db.coerce_float("") is None
        assert db.coerce_float("abc") is None
        assert db.coerce_float(True) is None  # bool 不是数值

    def test_str(self, tmp_db) -> None:
        assert db.coerce_str("  x  ") == "x"
        assert db.coerce_str("   ") is None
        assert db.coerce_str(None) is None
        assert db.coerce_str(0) == "0"

    def test_int(self, tmp_db) -> None:
        assert db.coerce_int("7") == 7
        assert db.coerce_int("7.9") == 7
        assert db.coerce_int("abc") is None

    def test_bool(self, tmp_db) -> None:
        for truthy in ("1", "true", "YES", "on", "y", "t"):
            assert db.coerce_bool(truthy) is True, truthy
        for falsy in ("0", "false", "NO", "off", "n", "f"):
            assert db.coerce_bool(falsy) is False, falsy
        assert db.coerce_bool(None) is False
        assert db.coerce_bool("", default=True) is True
        assert db.coerce_bool("weird", default=True) is True
