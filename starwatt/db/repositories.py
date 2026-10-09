"""Repository —— **唯一 SQL 出口**（消除旧代码的「三份真相」，L3）。

设计要点
========

* 每个方法自带连接（``with connect()``），**保证关闭** —— 旧代码的
  ``with get_conn() as conn`` 其实不关闭连接，长期运行会累积 fd
* 返回 :mod:`starwatt.db.models` 的 **dataclass 实体**，不是裸 ``sqlite3.Row``
* 所有时间比较用 **CST 常量** 而不是 SQL 的 ``datetime('now')``
  —— 见下方「L22 修复」

L22 修复：时间窗口的 8 小时偏差
===============================

旧代码::

    WHERE ts >= datetime('now', '-24 hours')

而 SQLite 的 ``datetime('now')`` 是 **UTC**，``ts`` 却是**朴素 CST** ——
两者相差 8 小时，于是 ``hours=24`` 的窗口**实际是 32 小时**。
旧代码的图表与日均统计因此一直偏大。

重写改为在 Python 侧用 :func:`starwatt.timeutil.now_cst` 算出 CST 截止时间，
作为**参数**传给 SQL::

    cutoff = to_stamp(timeutil.now_cst() - timedelta(hours=hours))
    WHERE ts >= ?

这是**有意的行为修正**。contract fixture 只固化字段名与算法向量
（算法向量是直接喂 ``_stats(rows)``，不经过 SQL），因此不受影响。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from typing import Any

from starwatt import timeutil
from starwatt.db.coerce import coerce_float, coerce_int, coerce_str
from starwatt.db.connection import connect
from starwatt.db.models import (
    AuditLog,
    DailyElec,
    MetaEntry,
    Pay,
    Record,
    RunStatus,
    Session,
    User,
    Violation,
)
from starwatt.timeutil import to_stamp

logger = logging.getLogger("starwatt.db.repos")

__all__ = [
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


def _cutoff(hours: int) -> str:
    """CST 截止时间戳（含），用于 ``ts >= ?`` 比较。"""
    return to_stamp(timeutil.now_cst() - timedelta(hours=hours))


def _cutoff_days(days: int) -> str:
    """按天算的 CST 截止**日期**（``YYYY-MM-DD``）。"""
    return (timeutil.now_cst() - timedelta(days=days)).strftime("%Y-%m-%d")


def hash_token(raw: str) -> str:
    """会话 token 的入库形式（L17 修复：不存明文）。"""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ===========================================================================
# records —— F1 抓取快照
# ===========================================================================
class RecordRepo:
    """``records`` 表。``ts`` 有 UNIQUE 约束 → 同秒重复抓取会 **REPLACE**（R49）。"""

    @staticmethod
    def insert(record: Record) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO records (ts, read_time, remain) VALUES (?, ?, ?)",
                (record.ts, record.read_time, record.remain),
            )

    @staticmethod
    def latest() -> Record | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT id, ts, read_time, remain FROM records ORDER BY ts DESC LIMIT 1"
            ).fetchone()
        return Record.from_row(row) if row else None

    @staticmethod
    def query(
        hours: int = 24,
        start_dt: str | None = None,
        end_dt: str | None = None,
    ) -> list[Record]:
        """按时间窗取记录（升序）。

        ``start_dt`` / ``end_dt`` 显式给出时**优先于** ``hours``
        （沿用旧 ``/api/data?start=&end=`` 语义）。
        """
        sql = "SELECT id, ts, read_time, remain FROM records"
        params: list[Any] = []
        where: list[str] = []
        if start_dt:
            where.append("ts >= ?")
            params.append(start_dt)
        if end_dt:
            where.append("ts <= ?")
            params.append(end_dt)
        if not start_dt and not end_dt:
            where.append("ts >= ?")
            params.append(_cutoff(hours))
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ts ASC"

        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Record.from_row(r) for r in rows]

    @staticmethod
    def recent(limit: int = 10) -> list[Record]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, ts, read_time, remain FROM records ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Record.from_row(r) for r in rows]

    @staticmethod
    def count() -> int:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM records").fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def delete_before(cutoff_ts: str) -> int:
        """删除 ``ts < cutoff_ts`` 的行，返回删除数（数据保留策略用）。"""
        with connect() as conn:
            cur = conn.execute("DELETE FROM records WHERE ts < ?", (cutoff_ts,))
        return int(cur.rowcount or 0)


# ===========================================================================
# daily_elec —— F2
# ===========================================================================
class DailyElecRepo:
    """``daily_elec`` 表。PK ``(roomId, dt)`` → upsert。"""

    @staticmethod
    def upsert_many(room_id: str, rows: list[dict[str, Any]]) -> int:
        """批量 upsert，返回写入行数。接受门户原始 dict。"""
        if not rows:
            return 0
        payload = [
            (
                room_id,
                coerce_str(r.get("dt")),
                coerce_float(r.get("total_eq")),
                coerce_float(r.get("esbm")),
                coerce_float(r.get("eebm")),
                coerce_float(r.get("zong_eq")),
            )
            for r in rows
            if coerce_str(r.get("dt"))
        ]
        if not payload:
            return 0
        with connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO daily_elec "
                "(roomId, dt, total_eq, esbm, eebm, zong_eq) VALUES (?, ?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    @staticmethod
    def recent(room_id: str, days: int = 30) -> list[DailyElec]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT roomId, dt, total_eq, esbm, eebm, zong_eq FROM daily_elec "
                "WHERE roomId = ? AND dt >= ? ORDER BY dt ASC",
                (room_id, _cutoff_days(days)),
            ).fetchall()
        return [DailyElec.from_row(r) for r in rows]

    @staticmethod
    def latest(room_id: str) -> DailyElec | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT roomId, dt, total_eq, esbm, eebm, zong_eq FROM daily_elec "
                "WHERE roomId = ? ORDER BY dt DESC LIMIT 1",
                (room_id,),
            ).fetchone()
        return DailyElec.from_row(row) if row else None


# ===========================================================================
# violations —— F3
# ===========================================================================
class ViolationRepo:
    """``violations`` 表。PK ``(roomId, dt, wg_reason)`` → upsert。"""

    @staticmethod
    def upsert_many(room_id: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = [
            (
                room_id,
                coerce_str(r.get("dt")),
                coerce_str(r.get("wg_reason")) or "违规",
                coerce_float(r.get("wg_power")),
            )
            for r in rows
            if coerce_str(r.get("dt"))
        ]
        if not payload:
            return 0
        with connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO violations "
                "(roomId, dt, wg_reason, wg_power) VALUES (?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    @staticmethod
    def recent(room_id: str, days: int = 30) -> list[Violation]:
        with connect() as conn:
            rows = conn.execute(
                "SELECT roomId, dt, wg_reason, wg_power FROM violations "
                "WHERE roomId = ? AND dt >= ? ORDER BY dt DESC",
                (room_id, _cutoff_days(days)),
            ).fetchall()
        return [Violation.from_row(r) for r in rows]


# ===========================================================================
# pay_history —— F5
# ===========================================================================
class PayRepo:
    """``pay_history`` 表。PK ``(roomId, dt, pay_type, fee_type)`` → upsert。"""

    @staticmethod
    def upsert_many(room_id: str, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        payload = []
        for r in rows:
            dt = coerce_str(r.get("dt"))
            if not dt:
                continue
            payload.append(
                (
                    room_id,
                    dt,
                    coerce_str(r.get("pay_type")) or "—",
                    coerce_str(r.get("fee_type")) or "—",
                    coerce_float(r.get("money")),
                )
            )
        if not payload:
            return 0
        with connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO pay_history "
                "(roomId, dt, pay_type, fee_type, money) VALUES (?, ?, ?, ?, ?)",
                payload,
            )
        return len(payload)

    @staticmethod
    def recent(room_id: str, days: int = 90, limit: int | None = None) -> list[Pay]:
        sql = (
            "SELECT roomId, dt, pay_type, fee_type, money FROM pay_history "
            "WHERE roomId = ? AND dt >= ? ORDER BY dt DESC"
        )
        params: list[Any] = [room_id, _cutoff_days(days)]
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [Pay.from_row(r) for r in rows]


# ===========================================================================
# run_status —— F4；每房间一行
# ===========================================================================
class RunStatusRepo:
    """``run_status`` 表。PK ``roomId`` → 天然 upsert。"""

    _COLUMNS = (
        "roomId",
        "meter_no",
        "dt",
        "run_status",
        "work_status",
        "update_dt",
        "vol",
        "cur",
        "yggl",
        "stop_reason",
    )

    @staticmethod
    def upsert(room_id: str, data: dict[str, Any]) -> None:
        """写入一行（覆盖同房间旧值）。

        ``data`` 是门户原始 dict（键为 ``meterNo`` / ``runStatus`` /
        ``updateDt`` 等驼峰形式），也接受下划线形式。
        """
        values = (
            room_id,
            coerce_str(data.get("meterNo") or data.get("meter_no")),
            coerce_str(data.get("dt")),
            coerce_str(data.get("runStatus") or data.get("run_status")),
            coerce_str(data.get("workStatus") or data.get("work_status")),
            coerce_str(data.get("updateDt") or data.get("update_dt")),
            coerce_float(data.get("vol")),
            coerce_float(data.get("cur")),
            coerce_float(data.get("yggl")),
            coerce_str(data.get("stopReason") or data.get("stop_reason")),
        )
        placeholders = ", ".join("?" * len(RunStatusRepo._COLUMNS))
        with connect() as conn:
            conn.execute(
                f"INSERT OR REPLACE INTO run_status "
                f"({', '.join(RunStatusRepo._COLUMNS)}) VALUES ({placeholders})",
                values,
            )

    @staticmethod
    def get(room_id: str) -> RunStatus | None:
        if not room_id:
            return None
        with connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(RunStatusRepo._COLUMNS)} FROM run_status "
                f"WHERE roomId = ?",
                (room_id,),
            ).fetchone()
        return RunStatus.from_row(row) if row else None


# ===========================================================================
# meta —— 通用键值（配置 + 状态 + 缓存）
# ===========================================================================
class MetaRepo:
    """``meta`` 表。``value`` 恒为 TEXT，提供类型化读写。

    kind 三类划分（config / state / cache）由 ``starwatt.config_registry``
    声明 —— 本 Repo 只管存取，不做语义判断。
    """

    @staticmethod
    def get(key: str) -> str | None:
        with connect() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        value = row["value"]
        return None if value is None else str(value)

    @staticmethod
    def set(key: str, value: Any) -> None:
        text = "" if value is None else str(value)
        with connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, text)
            )

    @staticmethod
    def get_float(key: str, default: float | None = None) -> float | None:
        value = coerce_float(MetaRepo.get(key))
        return default if value is None else value

    @staticmethod
    def get_int(key: str, default: int | None = None) -> int | None:
        value = coerce_int(MetaRepo.get(key))
        return default if value is None else value

    @staticmethod
    def get_bool(key: str, default: bool = False) -> bool:
        from starwatt.db.coerce import coerce_bool

        return coerce_bool(MetaRepo.get(key), default)

    @staticmethod
    def delete(key: str) -> None:
        with connect() as conn:
            conn.execute("DELETE FROM meta WHERE key = ?", (key,))

    @staticmethod
    def all() -> dict[str, str]:
        with connect() as conn:
            rows = conn.execute("SELECT key, value FROM meta").fetchall()
        return {r["key"]: (r["value"] or "") for r in rows}

    @staticmethod
    def entries() -> list[MetaEntry]:
        with connect() as conn:
            rows = conn.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return [MetaEntry.from_row(r) for r in rows]

    @staticmethod
    def set_many(pairs: dict[str, Any]) -> None:
        """批量写（一次连接、一个事务）。"""
        if not pairs:
            return
        payload = [(k, "" if v is None else str(v)) for k, v in pairs.items()]
        with connect() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", payload
            )


# ===========================================================================
# users —— R63 + R69 新列
# ===========================================================================
_USER_COLS = (
    "id",
    "username",
    "password_hash",
    "role",
    "created_at",
    "last_login_at",
    "disabled",
    "must_change_password",
)


class UserRepo:
    """``users`` 表。密码散列由 ``starwatt.auth.password`` 负责，本 Repo 只存值。"""

    @staticmethod
    def create(
        username: str,
        password_hash: str,
        role: str = "viewer",
        created_at: str | None = None,
        must_change_password: bool = False,
    ) -> User:
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO users "
                "(username, password_hash, role, created_at, must_change_password) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    username,
                    password_hash,
                    role,
                    created_at or to_stamp(timeutil.now_cst()),
                    1 if must_change_password else 0,
                ),
            )
            new_id = int(cur.lastrowid or 0)
        user = UserRepo.get_by_id(new_id)
        if user is None:  # pragma: no cover — 刚插入的行不可能查不到
            raise RuntimeError("UserRepo.create: 刚插入的行查不到")
        return user

    @staticmethod
    def get_by_id(user_id: int) -> User | None:
        with connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_USER_COLS)} FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return User.from_row(row) if row else None

    @staticmethod
    def get_by_username(username: str) -> User | None:
        if not username:
            return None
        with connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_USER_COLS)} FROM users WHERE username = ?",
                (username,),
            ).fetchone()
        return User.from_row(row) if row else None

    @staticmethod
    def list_all() -> list[User]:
        with connect() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_USER_COLS)} FROM users ORDER BY id"
            ).fetchall()
        return [User.from_row(r) for r in rows]

    @staticmethod
    def count() -> int:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def mark_login(user_id: int, at: str | None = None) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE users SET last_login_at = ? WHERE id = ?",
                (at or to_stamp(timeutil.now_cst()), user_id),
            )

    @staticmethod
    def update_password(
        user_id: int, password_hash: str, must_change: bool = False
    ) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, must_change_password = ? WHERE id = ?",
                (password_hash, 1 if must_change else 0, user_id),
            )

    @staticmethod
    def update_role(user_id: int, role: str) -> None:
        with connect() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    @staticmethod
    def set_disabled(user_id: int, disabled: bool) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE users SET disabled = ? WHERE id = ?",
                (1 if disabled else 0, user_id),
            )

    @staticmethod
    def delete(user_id: int) -> None:
        with connect() as conn:
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ===========================================================================
# sessions —— token 只存 sha256（L17 修复）
# ===========================================================================
class SessionRepo:
    """``sessions`` 表。``token`` 列存 **sha256 十六进制串**，不是原始 token。"""

    _COLS = ("id", "user_id", "token", "expires_at", "created_at", "ip", "user_agent")

    @staticmethod
    def create(
        user_id: int,
        token_hash: str,
        expires_at: str,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO sessions "
                "(user_id, token, expires_at, created_at, ip, user_agent) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    token_hash,
                    expires_at,
                    to_stamp(timeutil.now_cst()),
                    ip or "",
                    user_agent or "",
                ),
            )

    @staticmethod
    def get_by_token_hash(token_hash: str) -> Session | None:
        if not token_hash:
            return None
        with connect() as conn:
            row = conn.execute(
                f"SELECT {', '.join(SessionRepo._COLS)} FROM sessions WHERE token = ?",
                (token_hash,),
            ).fetchone()
        return Session.from_row(row) if row else None

    @staticmethod
    def touch(token_hash: str, expires_at: str) -> None:
        """滑动过期：把到期时间前推。"""
        with connect() as conn:
            conn.execute(
                "UPDATE sessions SET expires_at = ? WHERE token = ?",
                (expires_at, token_hash),
            )

    @staticmethod
    def delete(token_hash: str) -> None:
        with connect() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token_hash,))

    @staticmethod
    def delete_by_user(user_id: int) -> int:
        with connect() as conn:
            cur = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        return int(cur.rowcount or 0)

    @staticmethod
    def purge_expired(now: str | None = None) -> int:
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM sessions WHERE expires_at < ?",
                (now or to_stamp(timeutil.now_cst()),),
            )
        return int(cur.rowcount or 0)

    @staticmethod
    def count() -> int:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()
        return int(row["n"]) if row else 0


# ===========================================================================
# audit_log —— 只追加，不更新、不删除
# ===========================================================================
class AuditRepo:
    """``audit_log`` 表。**只追加**（不可篡改语义）。"""

    _COLS = (
        "id",
        "user_id",
        "action",
        "target",
        "ip",
        "user_agent",
        "created_at",
        "details",
    )

    @staticmethod
    def append(
        action: str,
        user_id: int | None = None,
        target: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        details: dict[str, Any] | None = None,
        created_at: str | None = None,
    ) -> None:
        """写一条审计。

        ``details`` 会被 ``json.dumps``；调用方**必须**保证其中不含
        密码 / token / openid（Q20 铁律）。
        """
        payload = json.dumps(details, ensure_ascii=False) if details else None
        with connect() as conn:
            conn.execute(
                "INSERT INTO audit_log "
                "(user_id, action, target, ip, user_agent, created_at, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    action,
                    target,
                    ip,
                    user_agent,
                    created_at or to_stamp(timeutil.now_cst()),
                    payload,
                ),
            )

    @staticmethod
    def recent(limit: int = 100, action: str | None = None) -> list[AuditLog]:
        sql = f"SELECT {', '.join(AuditRepo._COLS)} FROM audit_log"
        params: list[Any] = []
        if action:
            sql += " WHERE action = ?"
            params.append(action)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [AuditLog.from_row(r) for r in rows]

    @staticmethod
    def count() -> int:
        with connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()
        return int(row["n"]) if row else 0


# ===========================================================================
# failed_attempts —— 登录失败（供锁定判定）
# ===========================================================================
class FailedAttemptRepo:
    """``failed_attempts`` 表。锁定窗口由 ``starwatt.auth`` 判定。"""

    @staticmethod
    def record(username: str, ip: str | None = None, at: str | None = None) -> None:
        with connect() as conn:
            conn.execute(
                "INSERT INTO failed_attempts "
                "(username, ip, attempted_at) VALUES (?, ?, ?)",
                (username, ip or "", at or to_stamp(timeutil.now_cst())),
            )

    @staticmethod
    def count_since(username: str, since: str, ip: str | None = None) -> int:
        sql = (
            "SELECT COUNT(*) AS n FROM failed_attempts "
            "WHERE username = ? AND attempted_at >= ?"
        )
        params: list[Any] = [username, since]
        if ip:
            sql += " AND ip = ?"
            params.append(ip)
        with connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return int(row["n"]) if row else 0

    @staticmethod
    def clear(username: str, ip: str | None = None) -> int:
        sql = "DELETE FROM failed_attempts WHERE username = ?"
        params: list[Any] = [username]
        if ip:
            sql += " AND ip = ?"
            params.append(ip)
        with connect() as conn:
            cur = conn.execute(sql, params)
        return int(cur.rowcount or 0)

    @staticmethod
    def purge_before(cutoff: str) -> int:
        with connect() as conn:
            cur = conn.execute(
                "DELETE FROM failed_attempts WHERE attempted_at < ?", (cutoff,)
            )
        return int(cur.rowcount or 0)
