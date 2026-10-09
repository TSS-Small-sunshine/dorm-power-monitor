"""旧库（1.x 的 ``records.db``）能否零迁移升到 2.0（M0 契约 + Q9/Q10）。

为什么单独测这件事
==================

需求里承诺的是「零迁移」：老库直接挂载就能用。这条承诺的全部依据是：

* 新代码 ``init()`` 先跑 ``SCHEMA_SQL``（``CREATE TABLE IF NOT EXISTS``）
* 再跑 ``migrations.migrate()``（只加列 / 加表 / 加索引，**不删不改**）

所以「旧库能不能用」不是一个说法问题，而是一个**可测的事实**：
拿 M0 阶段在旧代码上抓下来的 ``schema.json``（真实的旧表结构）造一个库，
塞进数据，跑一次真实的启动路径，然后检查数据还在不在。

同时测 ``scripts/check_db.py`` —— 用户升级前用它回答「我手上这个是哪一版」。
它最重要的性质不是「报告好看」，而是**绝不写用户的原库**。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from scripts import check_db

PROJ = Path(__file__).resolve().parents[2]
FIXTURE = PROJ / "tests" / "regression" / "fixtures" / "schema.json"

#: 旧库相对新库唯一缺的两列（D2：users 表新增）
LEGACY_MISSING_COLUMNS = ("disabled", "must_change_password")


def _legacy_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _schema_of(path: Path) -> dict[str, list[str]]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return check_db._read_schema(conn)
    finally:
        conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_legacy_db(path: Path) -> None:
    """造一个**旧库**：结构与 M0 快照一致，且带一点真实数据。

    做法是「先建新库，再去掉那 2 列、删掉 schema_version」——
    这样索引与列类型都是真的，只把版本状态退回到 1.x。
    """
    from starwatt import db

    db.init()

    conn = sqlite3.connect(str(path))
    try:
        for column in LEGACY_MISSING_COLUMNS:
            conn.execute(f"ALTER TABLE users DROP COLUMN {column}")
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'")
        # 塞点数据（升级后必须一行不少）
        conn.execute(
            "INSERT INTO records (ts, read_time, remain) VALUES (?, ?, ?)",
            ("2026-10-01 08:00:00", "2026-10-01 07:58:00", 63.5),
        )
        conn.execute(
            "INSERT INTO records (ts, read_time, remain) VALUES (?, ?, ?)",
            ("2026-10-02 08:00:00", "2026-10-02 07:57:00", 58.25),
        )
        conn.execute(
            "INSERT INTO daily_elec (roomId, dt, total_eq, esbm, eebm, zong_eq) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("314", "2026-10-01", 4.2, 2.6, 1.6, 4.1),
        )
        conn.execute(
            "INSERT INTO violations (roomId, dt, wg_reason, wg_power) VALUES (?, ?, ?, ?)",
            ("314", "2026-09-20", "使用大功率电器", "1200W"),
        )
        conn.execute(
            "INSERT INTO pay_history (roomId, dt, pay_type, fee_type, money) "
            "VALUES (?, ?, ?, ?, ?)",
            ("314", "2026-09-25", "微信支付", "电费", 50.0),
        )
        conn.execute(
            "INSERT INTO users (username, password_hash, role, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("admin", "scrypt$legacy$hash", "admin", "2026-09-01 10:00:00"),
        )
        conn.commit()
    finally:
        conn.close()


class TestLegacyFixtureMatchesReality:
    """先确认我们的「旧库」模型没跑偏：它必须与 M0 快照逐列一致。"""

    def test_fixture_columns_match_legacy_model(self, settings_override) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        actual = _schema_of(db_file)

        fixture = _legacy_fixture()["tables"]
        assert set(actual) == set(fixture), "表集合与 M0 快照不一致"
        for table, columns in fixture.items():
            assert actual[table] == columns, f"{table} 的列与 M0 快照不一致"

    def test_only_delta_is_the_two_user_columns(self, settings_override) -> None:
        """新库 = 旧库 + users 的 2 列；除此之外不应有任何差异。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        from starwatt.db import schema as new_schema

        assert new_schema.USERS_NEW_COLUMNS == LEGACY_MISSING_COLUMNS
        assert new_schema.SCHEMA_VERSION == 3


class TestZeroMigration:
    """核心：旧库跑一次真实启动路径后，数据一行不少、结构补齐。"""

    def test_data_survives_and_columns_are_added(self, settings_override) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        from starwatt import db

        db.init()  # ← 这就是生产启动时做的事

        conn = sqlite3.connect(str(db_file))
        conn.row_factory = sqlite3.Row
        try:
            # 1) 数据一行不少
            assert conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()["n"] == 2
            row = conn.execute(
                "SELECT remain FROM records WHERE ts = '2026-10-02 08:00:00'"
            ).fetchone()
            assert row["remain"] == 58.25
            assert conn.execute("SELECT COUNT(*) AS n FROM daily_elec").fetchone()["n"] == 1
            assert conn.execute("SELECT COUNT(*) AS n FROM violations").fetchone()["n"] == 1
            assert conn.execute("SELECT COUNT(*) AS n FROM pay_history").fetchone()["n"] == 1
            assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1

            # 2) 两列补上了，且旧用户拿到安全默认值
            user = conn.execute(
                "SELECT disabled, must_change_password FROM users WHERE username = 'admin'"
            ).fetchone()
            assert user["disabled"] == 0
            assert user["must_change_password"] == 0

            # 3) 版本号写到了当前值
            version = conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()["value"]
            assert int(version) == 3
        finally:
            conn.close()

    def test_result_matches_a_fresh_database(self, settings_override) -> None:
        """升级后的结构应与全新库完全一致（表 + 列，逐项）。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        from starwatt import db

        db.init()
        upgraded = _schema_of(db_file)

        fresh_path = Path(db_file).parent / "fresh.db"
        with check_db._settings_scope(Path(db_file).parent, fresh_path):
            from starwatt.db.connection import init as fresh_init

            fresh_init()
        assert upgraded == _schema_of(fresh_path)

    def test_init_is_idempotent(self, settings_override) -> None:
        """重复启动不会重复加列（那会让 SQLite 报 duplicate column）。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        from starwatt import db

        for _ in range(3):
            db.init()
        conn = sqlite3.connect(str(db_file))
        try:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(users)")]
        finally:
            conn.close()
        assert columns.count("disabled") == 1
        assert columns.count("must_change_password") == 1

    def test_legacy_indexes_are_kept(self, settings_override) -> None:
        """旧库已有的索引不该被弄丢（趋势图查询靠它们）。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        from starwatt import db

        db.init()
        conn = sqlite3.connect(str(db_file))
        try:
            names = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
        finally:
            conn.close()
        for index in _legacy_fixture()["indexes"]:
            assert index in names, index


class TestCreateAppMigratesTheDatabase:
    """``create_app()`` 自己就得把库结构补齐。

    演练时发现的真 bug：``db.init()`` 原本只在 ``web.py:startup_once()`` 里跑，
    而那是 **gunicorn 的 post_fork** 调用的 —— 于是只有 gunicorn 这条启动路径
    会迁移库结构。拿旧库配新代码、用别的入口启动（`flask run`、waitress、
    或测试里直接 `create_app()` + 简单 WSGI 服务器）就会：

        sqlite3.OperationalError: no such column: disabled  →  登录 HTTP 500

    看起来像「新版本坏了」，实际是结构没升。切换时最怕这种失败。
    """

    def test_create_app_upgrades_a_legacy_db(self, settings_override) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        assert "disabled" not in _schema_of(db_file)["users"], "前置条件：应该是旧结构"

        from starwatt.web.factory import create_app

        create_app()  # ← 只建 app；不调用 startup_once

        users = _schema_of(db_file)["users"]
        assert "disabled" in users
        assert "must_change_password" in users

    def test_create_app_does_not_create_accounts(self, settings_override) -> None:
        """但它不该越界：**建号**仍归 startup_once。

        否则每个测试建一次 app 就会凭空多出一个 admin，测试之间互相污染。
        """
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        conn = sqlite3.connect(str(db_file))
        conn.execute("DELETE FROM users")
        conn.commit()
        conn.close()

        from starwatt.web.factory import create_app

        create_app()

        conn = sqlite3.connect(str(db_file))
        try:
            count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            assert count == 0, f"create_app() 不该建号，但 users 里有 {count} 行"
        finally:
            conn.close()


class TestCheckerTool:
    """``scripts/check_db.py`` —— 用户升级前回答「我这是哪一版」。"""

    def test_reports_legacy_db_as_usable(self, settings_override, capsys) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        code = check_db.main([str(db_file)])
        out = capsys.readouterr().out

        assert code == check_db.EXIT_OK
        assert "schema_version：0" in out
        assert "老库" in out
        assert "disabled" in out and "must_change_password" in out
        assert "可以直接用" in out
        assert "2 行" in out  # records 的行数

    def test_reports_fresh_db_as_clean(self, settings_override, capsys) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        from starwatt import db

        db.init()  # 先升级成最新

        code = check_db.main([str(db_file)])
        out = capsys.readouterr().out
        assert code == check_db.EXIT_OK
        assert "已是最新" in out
        assert "结构与当前代码一致" in out

    def test_never_writes_to_the_target(self, settings_override, capsys) -> None:
        """🔑 最重要的性质：体检（含 --fix）不得改动目标库一个字节。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        before = _digest(db_file)

        assert check_db.main([str(db_file)]) == check_db.EXIT_OK
        assert check_db.main([str(db_file), "--fix"]) == check_db.EXIT_OK
        assert check_db.main([str(db_file), "--quiet"]) == check_db.EXIT_OK
        capsys.readouterr()

        assert _digest(db_file) == before, "自检工具改动了目标库！"

    def test_fix_reports_clean_upgrade(self, settings_override, capsys) -> None:
        db_file = settings_override.db_path
        _make_legacy_db(db_file)

        code = check_db.main([str(db_file), "--fix"])
        out = capsys.readouterr().out
        assert code == check_db.EXIT_OK
        assert "schema_version：0 → 3" in out
        assert "补上的列：users.disabled、users.must_change_password" in out
        assert "✅ records" in out
        assert "副本升级干净" in out
        # 原库仍然是旧的（没被升级）—— 再看一次，结论应仍是「会自动补齐」
        assert check_db.main([str(db_file), "--quiet"]) == check_db.EXIT_OK
        assert "自动补齐" in capsys.readouterr().out

    def test_rejects_a_foreign_database(self, tmp_path, settings_override, capsys) -> None:
        other = tmp_path / "other.db"
        conn = sqlite3.connect(str(other))
        conn.execute("CREATE TABLE hello (id INTEGER)")
        conn.commit()
        conn.close()

        code = check_db.main([str(other)])
        out = capsys.readouterr().out
        assert code == check_db.EXIT_NOT_OURS
        assert "认不出" in out

    def test_handles_a_missing_file(self, tmp_path, capsys) -> None:
        code = check_db.main([str(tmp_path / "nope.db")])
        assert code == check_db.EXIT_NEEDS_WORK
        assert "文件不存在" in capsys.readouterr().out

    def test_detects_a_missing_table(self, settings_override, capsys) -> None:
        """整张表被删掉（早期版本可能没有 audit_log）也能认出来并说会自动建。"""
        db_file = settings_override.db_path
        _make_legacy_db(db_file)
        conn = sqlite3.connect(str(db_file))
        conn.execute("DROP TABLE audit_log")
        conn.commit()
        conn.close()

        code = check_db.main([str(db_file)])
        out = capsys.readouterr().out
        assert code == check_db.EXIT_OK
        assert "audit_log" in out
        assert "整张表缺失" in out
