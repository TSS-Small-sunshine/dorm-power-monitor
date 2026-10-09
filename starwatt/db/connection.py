"""SQLite 连接与初始化。

设计要点
========

* **WAL 模式** —— 让抓取器（写）与 Flask（读）真正并发
  （旧代码就是 WAL，保留）
* **``foreign_keys=ON``** —— SQLite 默认**关闭**外键约束，必须每个连接显式打开，
  否则 ``sessions.user_id → users.id`` 的 ``ON DELETE CASCADE`` 形同虚设
* **``isolation_level=None``**（autocommit）—— 与旧代码一致；
  需要事务时由调用方显式 ``BEGIN``
* :func:`connect` 是**正确关闭连接**的上下文管理器 ——
  旧代码用的 ``with get_conn() as conn`` 其实**不关闭**连接（只是 commit），
  长期运行会累积 fd
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from starwatt.config import get_settings
from starwatt.db import migrations
from starwatt.db.schema import SCHEMA_SQL

logger = logging.getLogger("starwatt.db")

__all__ = ["connect", "db_path", "get_conn", "init"]


def db_path() -> str:
    """当前 DB 文件路径（字符串，供 ``sqlite3.connect`` 使用）。"""
    return get_settings().db_file


def get_conn() -> sqlite3.Connection:
    """打开一个新连接（调用方负责关闭；推荐用 :func:`connect`）。

    会自动创建 DB 文件所在目录 —— 生产上 ``/var/lib/...`` 可能尚不存在。
    """
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(settings.db_file, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """连接上下文管理器 —— **保证关闭**。

    用法::

        with connect() as conn:
            conn.execute(...)
    """
    conn = get_conn()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    """幂等初始化：建表（``CREATE IF NOT EXISTS``）+ 补齐 schema 增量。

    可在每次进程启动、每个测试用例前安全重复调用。
    """
    with connect() as conn:
        conn.executescript(SCHEMA_SQL)
        version = migrations.migrate(conn)
    logger.debug("db.init: schema_version=%d path=%s", version, db_path())
