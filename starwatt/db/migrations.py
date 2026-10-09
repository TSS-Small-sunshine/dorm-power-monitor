"""版本化迁移框架（M4 修复）。

背景
====

Q9 说「零迁移」，但 D2 同时要求给 ``users`` 加 2 列 —— **加列就是 schema 变更**。
所以准确表述是「**零数据迁移**」：现有 ``records.db`` 的**数据行**不需要搬动，
但 schema 需要一个自动化的加列机制，否则第二次升级就会出问题。

四条要求
========

1. **幂等**   —— 每条迁移自身检查 ``PRAGMA table_info``，重复执行无副作用
2. **可重入** —— 中断后重跑从 ``meta.schema_version`` 断点继续
3. **不删列** —— 只加列 / 加表 / 加索引（SQLite 的 ``DROP COLUMN`` 限制多）
4. **可观测** —— 每次迁移记 ``INFO`` 日志（Q20 的 ``db`` 类别）

执行时机
========

``starwatt.db.connection.init()`` 先跑 ``SCHEMA_SQL``（``CREATE IF NOT EXISTS``，
让全新库一次到位），再调 :func:`migrate`（让老库补齐增量）。两者幂等，可反复执行。
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from starwatt.db import schema as _schema

logger = logging.getLogger("starwatt.db.migrations")

__all__ = [
    "MIGRATIONS",
    "Migration",
    "current_version",
    "has_column",
    "migrate",
]


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """``table`` 是否已有 ``column``（SQLite 无 ``IF NOT EXISTS`` 加列语法）。"""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row["name"] == column for row in rows)


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    if has_column(conn, table, column):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    logger.info("migration: %s.%s added", table, column)


# ---------------------------------------------------------------------------
# 迁移清单 —— 只追加，不改历史条目
# ---------------------------------------------------------------------------
def _m1_baseline(conn: sqlite3.Connection) -> None:
    """v1 = 基线：10 张表由 ``SCHEMA_SQL`` 建立，此处无需额外动作。

    存在的意义是让 ``schema_version`` 有一个明确起点，
    这样 v2 起的迁移能正确判断「老库需要补什么」。
    """


def _m2_users_disabled(conn: sqlite3.Connection) -> None:
    """v2 —— ``users.disabled``（Q9：支持禁用用户，L18）。"""
    _add_column_if_missing(conn, "users", "disabled", "INTEGER NOT NULL DEFAULT 0")


def _m3_users_must_change_password(conn: sqlite3.Connection) -> None:
    """v3 —— ``users.must_change_password``（Q19：首登强制改密）。"""
    _add_column_if_missing(
        conn, "users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"
    )


@dataclass(frozen=True, slots=True)
class Migration:
    """一条迁移。``apply`` 必须自身幂等。"""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "baseline", _m1_baseline),
    Migration(2, "users.disabled", _m2_users_disabled),
    Migration(3, "users.must_change_password", _m3_users_must_change_password),
)

#: 框架与 schema.py 的目标版本必须一致（防止改了 DDL 忘了加迁移）
assert MIGRATIONS[-1].version == _schema.SCHEMA_VERSION, (
    "migrations.py 与 schema.SCHEMA_VERSION 不同步"
)


def current_version(conn: sqlite3.Connection) -> int:
    """读 ``meta.schema_version``；缺失或损坏时按 0 处理。"""
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    if row is None or not row["value"]:
        return 0
    try:
        return int(row["value"])
    except (TypeError, ValueError):
        logger.warning("migration: meta.schema_version 不可解析，按 0 处理")
        return 0


def migrate(conn: sqlite3.Connection) -> int:
    """把库升到最新版本，返回升级后的版本号。

    每一步成功后立刻写 ``meta.schema_version``，因此中断可安全重跑。
    """
    start = current_version(conn)
    for migration in MIGRATIONS:
        if migration.version <= start:
            continue
        logger.info("migration: applying v%d (%s)", migration.version, migration.name)
        migration.apply(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(migration.version),),
        )
    end = current_version(conn)
    if end != start:
        logger.info("migration: schema_version %d -> %d", start, end)
    return end
