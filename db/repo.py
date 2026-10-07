"""db.repo - Round 61 Repository pattern for typed SQL access.

R61 introduction of the repository pattern.  Each ``*Repo`` class wraps
the same SQL strings the legacy ``db._legacy`` helpers use but returns
typed Pydantic v2 entities (``db.models``) instead of bare ``sqlite3.Row``
or untyped ``dict``s.

Why a repository layer at all:

* Centralises every raw SQL string in one place so a future schema
  bump only touches ``db.repo`` + the migration in ``db._legacy.init``
  rather than grep-and-replacing SQL across ``web.py``,
  ``dorm_power.py``, ``feishu_bot.py``.
* New code gets typed accessors with IntelliSense
  (``RecordRepo.latest().remain`` instead of ``row["remain"]``).
* Legacy code keeps its existing call sites unchanged - the package
  re-exports both surfaces from ``db.__init__``.

Database connection model:

* ``get_conn()`` is re-imported from ``db._legacy`` at call time (NOT
  at module import time) so monkey-patching during tests is honoured
  even after the import line executes.  Each repo method opens a
  fresh connection via the helper and closes it via the ``with``
  context manager - SQLite in autocommit / WAL mode tolerates that.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable, Optional

from db.models import (
    DailyElec,
    MetaEntry,
    Pay,
    Record,
    RunStatus,
    Violation,
)


# ---------------------------------------------------------------------------
# Connection helper - re-exported here so callers can ``from db.repo
# import get_conn`` without depending on the legacy submodule path.
# Imported lazily so monkey-patching ``db._legacy.get_conn`` in tests
# takes effect at the call site.
# ---------------------------------------------------------------------------
def get_conn():
    """Open a connection.

    Returns the same connection type as ``db._legacy.get_conn``: a
    ``sqlite3.Connection`` with row-factory set to ``sqlite3.Row``
    and WAL journal mode enabled.
    """
    from db import _legacy  # local import: see module docstring

    return _legacy.get_conn()


# ===========================================================================
# RecordRepo - ``records`` table (R1, R49 added UNIQUE(ts))
# ===========================================================================
class RecordRepo:
    """Typed CRUD for the ``records`` scrape-snapshot table.

    Every method returns ``Record`` models.  Pre-R61 callers used
    ``db.insert(remain, read_time)`` and ``db.query(hours=...)`` which
    returned ``sqlite3.Row``; both signatures are preserved on
    ``db._legacy`` so existing callers need no change.
    """

    @staticmethod
    def insert(record: Record) -> None:
        """Persist one ``Record``.

        Uses ``INSERT OR REPLACE`` on the ``ts PRIMARY KEY`` so
        duplicate-second scrapes overwrite instead of stacking
        (R49 belt-and-suspenders; the same ts primary key already
        prevents dups via UNIQUE).
        """
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO records (ts, read_time, remain) "
                "VALUES (?, ?, ?)",
                (record.ts, record.read_time, record.remain),
            )

    @staticmethod
    def latest() -> Optional[Record]:
        """Most-recent scrape row, or ``None`` if the table is empty."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id, ts, read_time, remain FROM records "
                "ORDER BY ts DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return Record(
                id=row[0],
                ts=row[1],
                read_time=row[2],
                remain=row[3],
            )

    @staticmethod
    def query(
        hours: Optional[int] = None,
        start_dt: Optional[str] = None,
        end_dt: Optional[str] = None,
    ) -> list[Record]:
        """Windowed lookup of scrape rows.

        Three call shapes mirror the legacy ``db.query`` semantics:

        1. ``RecordRepo.query(hours=N)`` - last N hours (windowed on
           ``ts``, not row-bounded; pre-R34 LIMIT-bound query silently
           returned wrong rows once the table had >N rows).
        2. ``RecordRepo.query(start_dt=..., end_dt=...)`` - explicit
           absolute range; both sides optional.
        3. ``RecordRepo.query()`` - no filter; returns everything in
           ascending order.  Used by the dashboard ``/api/data`` root
           endpoint when no range is supplied.
        """
        with get_conn() as conn:
            # Branch 2: explicit range takes precedence over hours
            if start_dt is not None or end_dt is not None:
                # R46 defence - normalise T-separator to space so a
                # caller passing "2026-09-15T00:00:00" still matches
                # rows whose ts is stored as "2026-09-15 00:00:00".
                if start_dt and "T" in start_dt:
                    start_dt = start_dt.replace("T", " ")
                if end_dt and "T" in end_dt:
                    end_dt = end_dt.replace("T", " ")
                clauses: list[str] = []
                params: list = []
                if start_dt:
                    clauses.append("ts >= ?")
                    params.append(start_dt)
                if end_dt:
                    clauses.append("ts <= ?")
                    params.append(end_dt)
                sql = (
                    "SELECT id, ts, read_time, remain FROM records "
                    f"WHERE {' AND '.join(clauses)} "
                    "ORDER BY ts DESC LIMIT 1000"
                )
                cur = conn.execute(sql, params)
            elif hours is not None and hours > 0:
                cur = conn.execute(
                    "SELECT id, ts, read_time, remain FROM records "
                    "WHERE ts >= datetime('now', ?) ORDER BY ts ASC",
                    (f"-{int(hours)} hours",),
                )
            else:
                cur = conn.execute(
                    "SELECT id, ts, read_time, remain FROM records "
                    "ORDER BY ts ASC"
                )
            return [
                Record(id=r[0], ts=r[1], read_time=r[2], remain=r[3])
                for r in cur
            ]


# ===========================================================================
# DailyElecRepo - ``daily_elec`` table (R2)
# ===========================================================================
class DailyElecRepo:
    """Typed CRUD for the ``daily_elec`` cumulative-meter table."""

    @staticmethod
    def upsert_batch(room_id: str, rows: list[DailyElec]) -> int:
        """Idempotent batch upsert keyed on ``(roomId, dt)``.

        Returns the number of rows actually written.  Mirrors the
        legacy ``db.record_daily_elec`` semantics: any row whose
        ``dt`` is empty / ``None`` is silently skipped (matches the
        legacy ``_coerce_str(dt) or continue`` behaviour).
        """
        if not room_id or not rows:
            return 0
        written = 0
        with get_conn() as conn:
            for r in rows:
                if not r.dt:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO daily_elec "
                    "(roomId, dt, total_eq, esbm, eebm, zong_eq) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        room_id,
                        r.dt,
                        r.total_eq,
                        r.esbm,
                        r.eebm,
                        r.zong_eq,
                    ),
                )
                written += 1
        return written

    @staticmethod
    def recent(days: int = 30, room_id: Optional[str] = None) -> list[DailyElec]:
        """Return up to ``days`` of rows; optional ``room_id`` filter.

        Differs from the legacy ``recent_daily_elec`` in two ways:

        1. Returns ``DailyElec`` models, not ``dict``.
        2. ``room_id`` is optional; ``None`` returns rows for every
           room (used by future global admin dashboards).
        """
        if days <= 0:
            return []
        with get_conn() as conn:
            if room_id:
                cur = conn.execute(
                    "SELECT roomId, dt, total_eq, esbm, eebm, zong_eq "
                    "FROM daily_elec "
                    "WHERE roomId = ? AND dt >= datetime('now', ?) "
                    "ORDER BY dt ASC",
                    (room_id, f"-{int(days)} days"),
                )
            else:
                cur = conn.execute(
                    "SELECT roomId, dt, total_eq, esbm, eebm, zong_eq "
                    "FROM daily_elec "
                    "WHERE dt >= datetime('now', ?) "
                    "ORDER BY dt ASC",
                    (f"-{int(days)} days",),
                )
            return [
                DailyElec(
                    roomId=r[0],
                    dt=r[1],
                    total_eq=r[2],
                    esbm=r[3],
                    eebm=r[4],
                    zong_eq=r[5],
                )
                for r in cur
            ]


# ===========================================================================
# ViolationRepo - ``violations`` table (R2)
# ===========================================================================
class ViolationRepo:
    """Typed CRUD for the ``violations`` violation ledger."""

    @staticmethod
    def upsert_batch(room_id: str, rows: list[Violation]) -> int:
        """Idempotent upsert on ``(roomId, dt, wg_reason)`` primary key."""
        if not room_id or not rows:
            return 0
        written = 0
        with get_conn() as conn:
            for r in rows:
                if not r.dt or not r.wg_reason:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO violations "
                    "(roomId, dt, wg_reason, wg_power) "
                    "VALUES (?, ?, ?, ?)",
                    (room_id, r.dt, r.wg_reason, r.wg_power),
                )
                written += 1
        return written

    @staticmethod
    def recent(days: int = 30, room_id: Optional[str] = None) -> list[Violation]:
        """Most-recent violations in the last ``days`` days, newest-first."""
        if days <= 0:
            return []
        with get_conn() as conn:
            if room_id:
                cur = conn.execute(
                    "SELECT dt, wg_reason, wg_power "
                    "FROM violations "
                    "WHERE roomId = ? AND dt >= datetime('now', ?) "
                    "ORDER BY dt DESC",
                    (room_id, f"-{int(days)} days"),
                )
            else:
                cur = conn.execute(
                    "SELECT dt, wg_reason, wg_power "
                    "FROM violations "
                    "WHERE dt >= datetime('now', ?) "
                    "ORDER BY dt DESC",
                    (f"-{int(days)} days",),
                )
            return [
                Violation(dt=r[0], wg_reason=r[1], wg_power=r[2])
                for r in cur
            ]


# ===========================================================================
# PayRepo - ``pay_history`` table (R2)
# ===========================================================================
class PayRepo:
    """Typed CRUD for the ``pay_history`` recharge / refund table."""

    @staticmethod
    def upsert_batch(room_id: str, rows: list[Pay]) -> int:
        """Idempotent batch upsert on ``(roomId, dt, pay_type, fee_type)``."""
        if not room_id or not rows:
            return 0
        written = 0
        with get_conn() as conn:
            for r in rows:
                if not (r.dt and r.payType):
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO pay_history "
                    "(roomId, dt, pay_type, fee_type, money) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (room_id, r.dt, r.payType, r.feeType, r.money),
                )
                written += 1
        return written

    @staticmethod
    def recent(days: int = 30, room_id: Optional[str] = None) -> list[Pay]:
        """Most-recent payment rows; newest-first."""
        if days <= 0:
            return []
        with get_conn() as conn:
            if room_id:
                cur = conn.execute(
                    "SELECT dt, pay_type, fee_type, money "
                    "FROM pay_history "
                    "WHERE roomId = ? AND dt >= datetime('now', ?) "
                    "ORDER BY dt DESC",
                    (room_id, f"-{int(days)} days"),
                )
            else:
                cur = conn.execute(
                    "SELECT dt, pay_type, fee_type, money "
                    "FROM pay_history "
                    "WHERE dt >= datetime('now', ?) "
                    "ORDER BY dt DESC",
                    (f"-{int(days)} days",),
                )
            return [
                Pay(
                    dt=r[0],
                    payType=r[1],  # alias for pay_type, see db.models.Pay
                    feeType=r[2],  # alias for fee_type
                    money=r[3],
                )
                for r in cur
            ]


# ===========================================================================
# RunStatusRepo - ``run_status`` table (R2)
# ===========================================================================
class RunStatusRepo:
    """Typed CRUD for the single-row-per-room ``run_status`` table."""

    @staticmethod
    def upsert(room_id: str, status: RunStatus) -> None:
        """Persist the live meter snapshot, keyed on ``roomId``."""
        if not room_id:
            return
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO run_status "
                "(roomId, meter_no, dt, run_status, work_status, update_dt, "
                " vol, cur, yggl, stop_reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    status.meter_no,
                    status.dt,
                    status.run_status,
                    status.work_status,
                    status.update_dt,
                    status.vol,
                    status.cur,
                    status.yggl,
                    status.stop_reason,
                ),
            )

    @staticmethod
    def get(room_id: Optional[str]) -> Optional[RunStatus]:
        """Return the live snapshot, or ``None`` when not yet scraped."""
        if not room_id:
            return None
        with get_conn() as conn:
            row = conn.execute(
                "SELECT roomId, meter_no, dt, run_status, work_status, "
                "       update_dt, vol, cur, yggl, stop_reason "
                "FROM run_status WHERE roomId = ?",
                (room_id,),
            ).fetchone()
        if not row:
            return None
        return RunStatus(
            roomId=row[0],
            meter_no=row[1],
            dt=row[2],
            run_status=row[3],
            work_status=row[4],
            update_dt=row[5],
            vol=row[6],
            cur=row[7],
            yggl=row[8],
            stop_reason=row[9],
        )


# ===========================================================================
# MetaRepo - ``meta`` table (R2 + R34A)
# ===========================================================================
class MetaRepo:
    """Typed CRUD for the ``meta`` key/value store.

    The ``meta`` table is the simplest possible schema (``key
    PRIMARY KEY``, ``value TEXT``), but it carries a lot of the
    project's runtime state - last scrape timestamps, push cadence
    enable flags, eqprice, OOBE step.  Typed access here keeps the
    many writers (``dorm_power`` / ``web`` / ``feishu_bot``) in sync.
    """

    @staticmethod
    def get(key: str) -> Optional[str]:
        """Return the value for ``key`` or ``None`` if not set."""
        with get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = ?", (key,)
            ).fetchone()
        return row[0] if row else None

    @staticmethod
    def set(key: str, value: Optional[str]) -> None:
        """Upsert one meta entry.  ``None`` is stored as empty string.

        Storing ``None`` as ``""`` (rather than NULL) keeps the
        "" vs NULL distinction visible to readers; ``MetaRepo.get``
        always returns ``str`` (never ``None`` from SQLite's NULL).
        Pre-R61 ``db.set_meta`` did the same ``str(value)``
        coercion; this preserves that behaviour.
        """
        stored = "" if value is None else value
        with get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                (key, stored),
            )

    @staticmethod
    def get_float(key: str) -> Optional[float]:
        """Typed accessor: parse the meta value as ``float``.

        Returns ``None`` when the row is missing OR the value fails
        to parse (e.g. legacy row containing ``"off"`` for a flag
        flag whose ``get_int`` would also return ``None``).  Mirrors
        legacy ``db.get_meta_float``.
        """
        raw = MetaRepo.get(key)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def get_int(key: str) -> Optional[int]:
        """Typed accessor: parse the meta value as ``int``."""
        raw = MetaRepo.get(key)
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def list_recent(prefix: Optional[str] = None, limit: int = 100) -> list[MetaEntry]:
        """Diagnostic dump for the admin panel.

        Optional ``prefix`` filters keys starting with that string
        (e.g. ``"push_"`` for the cadence/push config).  Limited to
        ``limit`` rows; defaults to a small 100 to keep the admin
        page cheap.
        """
        with get_conn() as conn:
            if prefix:
                cur = conn.execute(
                    "SELECT key, value FROM meta WHERE key LIKE ? "
                    "ORDER BY key ASC LIMIT ?",
                    (prefix + "%", int(limit)),
                )
            else:
                cur = conn.execute(
                    "SELECT key, value FROM meta "
                    "ORDER BY key ASC LIMIT ?",
                    (int(limit),),
                )
            return [MetaEntry(key=r[0], value=r[1]) for r in cur]


__all__ = [
    "get_conn",
    "RecordRepo",
    "DailyElecRepo",
    "ViolationRepo",
    "PayRepo",
    "RunStatusRepo",
    "MetaRepo",
]
