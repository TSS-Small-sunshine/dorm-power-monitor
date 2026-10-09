"""``starwatt.db`` —— 持久化层（唯一实现，消除旧代码的「三份真相」L3）。

包结构
======

| 文件 | 职责 |
|---|---|
| ``schema.py``       | ``SCHEMA_SQL`` —— 表/索引 DDL 的唯一真相 |
| ``migrations.py``   | 版本化迁移（幂等 / 可重入 / 只加不删） |
| ``connection.py``   | 连接生命周期（WAL / foreign_keys / 正确关闭）+ ``init()`` |
| ``coerce.py``       | 类型强制转换（收敛旧代码的两份实现，L9） |
| ``models.py``       | 领域实体（**dataclass**，非 Pydantic —— D1） |
| ``repositories.py`` | **唯一 SQL 出口** |

与旧代码的差异
==============

* ❌ 不再有 ``db/_legacy.py``（「冻结副本」）与并行的 ``db/repo.py``
* ❌ 不再导出函数式 API（``db.insert`` / ``db.query`` …）——
  统一走 ``RecordRepo`` / ``MetaRepo`` 等
* ✅ ``sessions.token`` 存 **sha256 摘要**（L17）
* ✅ ``users`` 新增 ``disabled`` / ``must_change_password`` 两列（Q9 / D2）
* ✅ 时间窗口用 **CST 常量**而非 SQL ``datetime('now')``（L22 修复）
"""
from __future__ import annotations

from starwatt.db.coerce import coerce_bool, coerce_float, coerce_int, coerce_str
from starwatt.db.connection import connect, db_path, get_conn, init
from starwatt.db.migrations import MIGRATIONS, Migration, current_version, migrate
from starwatt.db.models import (
    AuditLog,
    DailyElec,
    MetaEntry,
    Pay,
    Record,
    RunStatus,
    Session,
    StatsCard,
    User,
    Violation,
)
from starwatt.db.repositories import (
    AuditRepo,
    DailyElecRepo,
    FailedAttemptRepo,
    MetaRepo,
    PayRepo,
    RecordRepo,
    RunStatusRepo,
    SessionRepo,
    UserRepo,
    ViolationRepo,
    hash_token,
)
from starwatt.db.schema import (
    INDEX_NAMES,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    TABLE_NAMES,
    USERS_NEW_COLUMNS,
)

__all__ = [
    # 连接 / 初始化
    "connect",
    "db_path",
    "get_conn",
    "init",
    # schema
    "INDEX_NAMES",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "TABLE_NAMES",
    "USERS_NEW_COLUMNS",
    # 迁移
    "MIGRATIONS",
    "Migration",
    "current_version",
    "migrate",
    # 强制转换
    "coerce_bool",
    "coerce_float",
    "coerce_int",
    "coerce_str",
    # 实体
    "AuditLog",
    "DailyElec",
    "MetaEntry",
    "Pay",
    "Record",
    "RunStatus",
    "Session",
    "StatsCard",
    "User",
    "Violation",
    # Repository
    "AuditRepo",
    "DailyElecRepo",
    "FailedAttemptRepo",
    "MetaRepo",
    "PayRepo",
    "RecordRepo",
    "RunStatusRepo",
    "SessionRepo",
    "UserRepo",
    "ViolationRepo",
    "hash_token",
]
