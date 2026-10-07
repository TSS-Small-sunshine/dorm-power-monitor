"""数据库 schema —— **唯一真相**（消除旧代码的「三份真相」，L3）。

设计要点
========

* 表名 / 字段名与旧库 **100% 一致** → **零数据迁移**（Q9）
* **唯一**的 schema delta 是 ``users`` 表新增 2 列
  （``disabled`` / ``must_change_password``，Q9 / D2）；
  由 ``starwatt.db.migrations`` 负责老库升级，本文件的 DDL 让全新安装一次到位
* **不新增索引**：``daily_elec`` / ``violations`` / ``pay_history`` 的主键
  分别是 ``(roomId, dt)`` / ``(roomId, dt, wg_reason)`` /
  ``(roomId, dt, pay_type, fee_type)`` —— **PK 索引已覆盖所有按房间的查询**，
  再加 ``idx_*_room`` 是纯冗余（写放大）。``users`` 行数 ≤10，不需要 role 索引。
* ``created_at`` 的 SQL 默认值用 ``datetime('now', '+8 hours')`` ——
  SQLite 的 ``datetime('now')`` **恒为 UTC**，+8h 即北京时间，
  **与服务器时区无关**（Q10 的双保险）

⚠️ 冻结契约（AGENTS.md 硬约束 #6）
==================================

表名与字段名是下游（抓取器 / 飞书渲染 / Admin API / 前端）的共同依赖，
**不得改名、不得删列**。改动必须走 ``migrations.py``。
"""
from __future__ import annotations

__all__ = [
    "INDEX_NAMES",
    "SCHEMA_SQL",
    "SCHEMA_VERSION",
    "TABLE_NAMES",
]

#: 目标 schema 版本（与 migrations.py 的最后一条对齐）
SCHEMA_VERSION = 3

#: 10 张表的规范名
TABLE_NAMES: tuple[str, ...] = (
    "audit_log",
    "daily_elec",
    "failed_attempts",
    "meta",
    "pay_history",
    "records",
    "run_status",
    "sessions",
    "users",
    "violations",
)

#: 11 个**显式**索引（不含 SQLite 自动为主键/UNIQUE 建的 ``sqlite_autoindex_*``）
INDEX_NAMES: tuple[str, ...] = (
    "idx_audit_action",
    "idx_audit_time",
    "idx_audit_user",
    "idx_daily_elec_dt",
    "idx_failed_attempts_time",
    "idx_failed_attempts_username",
    "idx_pay_history_dt",
    "idx_records_ts",
    "idx_sessions_expires",
    "idx_sessions_token",
    "idx_violations_dt",
)

#: ``users`` 表在基线之上**刻意**新增的 2 列（Q9 / D2）
USERS_NEW_COLUMNS: tuple[str, ...] = ("disabled", "must_change_password")


# ---------------------------------------------------------------------------
# DDL —— 全部 CREATE IF NOT EXISTS（幂等，每次启动安全执行）
# ---------------------------------------------------------------------------
SCHEMA_SQL = """
-- ===== 抓取快照（F1）——  R1 建立，R49 加 UNIQUE(ts) ======================
CREATE TABLE IF NOT EXISTS records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL UNIQUE,
    read_time  TEXT,
    remain     REAL
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);

-- ===== 每日用电（F2）—— R2 =============================================
-- PK (roomId, dt) 的索引已覆盖所有按房间 + 日期的查询
CREATE TABLE IF NOT EXISTS daily_elec (
    roomId   TEXT NOT NULL,
    dt       TEXT NOT NULL,
    total_eq REAL,
    esbm     REAL,
    eebm     REAL,
    zong_eq  REAL,
    PRIMARY KEY (roomId, dt)
);
CREATE INDEX IF NOT EXISTS idx_daily_elec_dt ON daily_elec(dt);

-- ===== 违规记录（F3）—— R2 =============================================
CREATE TABLE IF NOT EXISTS violations (
    roomId    TEXT NOT NULL,
    dt        TEXT NOT NULL,
    wg_reason TEXT NOT NULL,
    wg_power  REAL,
    PRIMARY KEY (roomId, dt, wg_reason)
);
CREATE INDEX IF NOT EXISTS idx_violations_dt ON violations(dt);

-- ===== 缴费历史（F5）—— R2 =============================================
CREATE TABLE IF NOT EXISTS pay_history (
    roomId    TEXT NOT NULL,
    dt        TEXT NOT NULL,
    pay_type  TEXT NOT NULL,
    fee_type  TEXT NOT NULL,
    money     REAL,
    PRIMARY KEY (roomId, dt, pay_type, fee_type)
);
CREATE INDEX IF NOT EXISTS idx_pay_history_dt ON pay_history(dt);

-- ===== 电表实时状态（F4）—— R2；每房间一行（upsert 语义）===============
CREATE TABLE IF NOT EXISTS run_status (
    roomId      TEXT PRIMARY KEY,
    meter_no    TEXT,
    dt          TEXT,
    run_status  TEXT,
    work_status TEXT,
    update_dt   TEXT,
    vol         REAL,
    cur         REAL,
    yggl        REAL,
    stop_reason TEXT
);

-- ===== 通用键值（配置 + 运行时状态）—— R2 / R34A =======================
-- kind 三类划分（config / state / cache）见 starwatt/config_registry
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- ===== 认证与审计 —— R63；R69 给 users 加 2 列（Q9 / D2）===============
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE,
    password_hash        TEXT    NOT NULL,
    role                 TEXT    NOT NULL DEFAULT 'viewer',
    created_at           TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours')),
    last_login_at        TEXT,
    disabled             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0
);

-- sessions.token 存 **sha256(token) 的十六进制串**（L17 修复），
-- 列名保持 token 不变 → 零迁移；旧行因无法验证而自然失效（用户重登一次）。
CREATE TABLE IF NOT EXISTS sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    token      TEXT    NOT NULL UNIQUE,
    expires_at TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours')),
    ip         TEXT,
    user_agent TEXT,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sessions_token   ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_sessions_expires ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS failed_attempts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT    NOT NULL,
    ip           TEXT,
    attempted_at TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours'))
);
CREATE INDEX IF NOT EXISTS idx_failed_attempts_username ON failed_attempts(username);
CREATE INDEX IF NOT EXISTS idx_failed_attempts_time     ON failed_attempts(attempted_at);

CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    action     TEXT    NOT NULL,
    target     TEXT,
    ip         TEXT,
    user_agent TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now', '+8 hours')),
    details    TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_user   ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_time   ON audit_log(created_at);
"""

