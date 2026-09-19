"""db.py — Round 49 documented.

SQLite persistence layer — the single source of truth for both the
scraper (``dorm_power.py``) and the Flask dashboard (``web.py``).  One
file, one schema, one migration policy.  All helpers are pure-Python so
they can be unit-tested with an in-memory ``:memory:`` connection (no
disk I/O during tests).

主要功能 / Key responsibilities:
  * Maintain the 6-table schema: ``records`` (R1, R49 has UNIQUE(ts)),
    ``daily_elec`` (R2), ``violations`` (R2), ``pay_history`` (R2),
    ``run_status`` (R2), ``meta`` (R2 + R34A expanded).
  * Provide CRUD helpers (``insert_record``, ``get_records``,
    ``insert_violation``, ``get_violations`` …) plus the typed
    ``_coerce_float`` / ``_coerce_str`` coercion utilities that the
    scraper relies on for portal JSON normalisation.
  * Manage ``meta`` key/value state used by lazy getters in
    ``config.py`` (eqprice, last_room_id, last_scrape_at, …).
  * Idempotent ``init()`` migrates the schema forward across rounds.

数据流 / Data flow:
  dorm_power (writes) + web / feishu_bot (reads) -> db helpers ->
  records.db (sqlite3 file)

依赖 / Dependencies:
  * stdlib: ``sqlite3``, ``datetime``, ``typing``
  * ``config`` (for ``DB_PATH``)

Round history:
  * R1   - original ``records`` schema
  * R2   - 5 additional tables (daily_elec / violations / pay_history /
            run_status / meta)
  * R34A - meta key constants + last-scrape tracking
  * R34D - DROP COLUMN raw_html migration + _coerce_* helpers
            promoted to PUBLIC
  * R36   - db.insert 3->2 参数修复 (sql guard already dropped)
  * R39   - backfill robustness helpers (upsert + partial retry)
  * R41   - monthly_projection cumulative-meter helpers
  * R42   - meta key added for last_eqprice_refresh
  * R49   - records.ts UNIQUE 约束 + db.insert 改 INSERT OR REPLACE,
            去重 migration (scripts/deploy/r49_dedupe_records.py)

The original per-schema docstring follows below for reference.

---

SQLite helpers shared by the scraper and the Flask dashboard.

The Round 1 schema was intentionally minimal: each scrape is one row.
Round 2 adds five more tables (and the helpers that go with them) for
the new portal endpoints:

* ``daily_elec``     — per-day usage totals from
  ``/campus/webchat/dormEmDayElectQuery/getEmDayElectQuery``
  (F2 in the spec).
* ``violations``     — power-violation records from
  ``/campus/webchat/dormEmWgQuery/selectWgElect`` (F3).
* ``pay_history``    — recharge / refund history from
  ``/campus/webchat/dormEmPayQuery/getEmPayQuery`` (F5).
* ``run_status``     — single-row-per-room live meter snapshot from
  ``/campus/webchat/dormEmRunStatus/getEmRunStatus`` (F4).
* ``meta``           — generic key/value store for ``last_violation_alert_at``,
  ``eqprice``, ``last_scrape_at``, ``backfill_done``, etc.

The original ``records`` table is untouched; ``init()`` adds the new
tables with ``CREATE TABLE IF NOT EXISTS`` so existing deployments
migrate forward without any manual step.

Round 34D — Public API surface:

* ``_coerce_float`` and ``_coerce_str`` (defined below) are PUBLIC
  helpers.  Other modules (``dorm_power.py``, ``feishu_bot.py``) may
  import them as ``from db import _coerce_float, _coerce_str``.  The
  leading underscore is for the original "module-private" convention
  but the functions are intentionally re-used across modules — do
  not duplicate the bodies elsewhere; import from here.

* ``init()`` is idempotent and performs the Round 34D ``DROP COLUMN
  raw_html`` migration on every call (safe no-op if already dropped).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

import config


# ---------------------------------------------------------------------------
# Round 34D — meta key constants (centralised so all writers agree on names)
# ---------------------------------------------------------------------------
# These constants are public — any module may ``from db import
# META_LAST_DAILY_REPORT`` etc. to avoid the typo-prone string-literal
# pattern that the pre-Round-34 codebase had.

# Round 34 — push cadence gates
META_LAST_DAILY_REPORT = "last_daily_report_date"
META_LAST_WEEKLY_REPORT = "last_weekly_report_iso"
META_LAST_MONTHLY_REPORT = "last_monthly_report_mo"

# Round 34 — push enable / time
META_PUSH_L1_ENABLE = "push_l1_enable"
META_PUSH_L2_ENABLE = "push_l2_enable"
META_PUSH_DAILY_ENABLE = "push_daily_enable"
META_PUSH_WEEKLY_ENABLE = "push_weekly_enable"
META_PUSH_MONTHLY_ENABLE = "push_monthly_enable"
META_PUSH_DAILY_TIME = "push_daily_time"
META_PUSH_WEEKLY_TIME = "push_weekly_time"
META_PUSH_MONTHLY_TIME = "push_monthly_time"
META_PUSH_L1_RECEIVERS = "push_receivers_l1"
META_PUSH_L2_RECEIVERS = "push_receivers_l2"
META_PUSH_REPORT_RECEIVERS = "push_receivers_report"
META_PUSH_ALERT_RECEIVERS = "push_receivers_alert"

# Round 34 — quiet hours
META_QUIET_HOURS_START = "quiet_hours_start"
META_QUIET_HOURS_END = "quiet_hours_end"

# Round 34 — scrape config (WebUI override)
META_DORM_BASE_URL = "dorm_base_url"
META_DORM_OPENID = "dorm_openid"
META_DORM_ROOM_ID = "dorm_room_id"
META_EQPRICE = "eqprice"
META_FEISHU_WEBHOOK_URL = "feishu_webhook_url"
META_API_INTERNAL_TOKEN = "api_internal_token"

# Round 34 — OOBE state
META_OOBE_STEP = "oobe_step"
META_OOBE_COMPLETED = "oobe_completed"
META_ADMIN_PASSWORD = "admin_password"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT    NOT NULL UNIQUE,
    read_time  TEXT,
    remain     REAL
);
CREATE INDEX IF NOT EXISTS idx_records_ts ON records(ts);

CREATE TABLE IF NOT EXISTS daily_elec (
    roomId   TEXT NOT NULL,
    dt       TEXT NOT NULL,
    total_eq REAL,
    esbm     REAL,
    eebm     REAL,
    zong_eq  REAL,
    PRIMARY KEY (roomId, dt)
);

CREATE TABLE IF NOT EXISTS violations (
    roomId    TEXT NOT NULL,
    dt        TEXT NOT NULL,
    wg_reason TEXT NOT NULL,
    wg_power  REAL,
    PRIMARY KEY (roomId, dt, wg_reason)
);

CREATE TABLE IF NOT EXISTS pay_history (
    roomId    TEXT NOT NULL,
    dt        TEXT NOT NULL,
    pay_type  TEXT NOT NULL,
    fee_type  TEXT NOT NULL,
    money     REAL,
    PRIMARY KEY (roomId, dt, pay_type, fee_type)
);

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

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_daily_elec_dt       ON daily_elec(dt);
CREATE INDEX IF NOT EXISTS idx_violations_dt       ON violations(dt);
CREATE INDEX IF NOT EXISTS idx_pay_history_dt      ON pay_history(dt);
"""


def get_conn() -> sqlite3.Connection:
    """Open a connection with row factory + WAL for safer concurrent reads."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the scraper write while Flask reads concurrently.
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init() -> None:
    """Idempotent schema bootstrap.  Adds new tables/columns without touching existing data."""
    with get_conn() as conn:
        conn.executescript(_SCHEMA)
        # Round 34D — drop unused raw_html column on existing DBs
        # (safe if column doesn't exist — try/except OperationalError)
        try:
            conn.execute("ALTER TABLE records DROP COLUMN raw_html")
        except sqlite3.OperationalError:
            pass  # already dropped or new install
        # Round 49 — rebuild ``records`` to add UNIQUE(ts) constraint
        # if the existing schema is missing it.  SQLite has no
        # ``ALTER TABLE ADD CONSTRAINT``, so the only safe path is
        # CREATE new -> copy data -> DROP + RENAME.  This migration is
        # idempotent: a no-op once the UNIQUE constraint exists.
        _migrate_records_unique(conn)
        # Round 35 — collapse legacy meta.dorm_room_id into meta.last_room_id.
        # Done inside the same ``with`` block so the migration is part of
        # the init transaction (no partial state if the process dies
        # mid-call).  Idempotent: a no-op if ``last_room_id`` is already
        # set, and clearing ``dorm_room_id`` is safe to repeat.
        _migrate_dorm_room_id(conn)


def _migrate_records_unique(conn: sqlite3.Connection) -> None:
    """Round 49 — rebuild ``records`` with UNIQUE(ts) if it lacks the constraint.

    Background: pre-R49 ``records`` schema was
    ``CREATE TABLE records (id, ts, read_time, remain)`` with only an
    ``INDEX(ts)``.  Cron overlap + an insert race produced 177 dup rows
    in production (863 total / 686 unique ts).  ``db.insert()`` used
    plain ``INSERT INTO`` so the dups stacked.

    Post-R49 the schema is
    ``CREATE TABLE records (id, ts UNIQUE, read_time, remain)`` and
    ``db.insert()`` uses ``INSERT OR REPLACE``.  New installs pick this
    up via the ``_SCHEMA`` string above; existing installs need this
    rebuild helper.

    Detection: query ``sqlite_master`` for any index whose SQL contains
    ``UNIQUE`` AND which sits on the ``records`` table.  The
    ``CREATE INDEX IF NOT EXISTS idx_records_ts`` line is NOT unique,
    so absence of a ``UNIQUE`` index marker means we're on the pre-R49
    schema.  When detected, rebuild the table inside a single
    transaction so a crash mid-call leaves the original intact.
    """
    # Check whether records already has a UNIQUE index on ts
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'records'"
    ).fetchall()
    has_unique = any(
        r[0] and "UNIQUE" in r[0].upper() for r in rows
    )
    if has_unique:
        return  # already migrated
    # Rebuild table — copy data, drop, rename.
    # NOTE: ``BEGIN TRANSACTION; ... COMMIT;`` in executescript keeps
    # the whole rebuild atomic.  If we crash mid-rebuild, the BEGIN
    # rolls back and the original ``records`` table is untouched.
    conn.executescript("""
        BEGIN TRANSACTION;

        CREATE TABLE records_new (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            ts         TEXT    NOT NULL UNIQUE,
            read_time  TEXT,
            remain     REAL
        );
        INSERT INTO records_new (id, ts, read_time, remain)
            SELECT id, ts, read_time, remain FROM records;
        DROP TABLE records;
        ALTER TABLE records_new RENAME TO records;
        CREATE INDEX idx_records_ts ON records(ts);

        COMMIT;
    """)


def _migrate_dorm_room_id(conn: sqlite3.Connection) -> None:
    """Round 35 — collapse ``meta.dorm_room_id`` into ``meta.last_room_id``.

    Background: admin UI Round 34B wrote ``meta.dorm_room_id``, but
    ``dorm_power.run_once`` only ever read ``config.DORM_ROOM_ID`` (env)
    and wrote ``meta.last_room_id``.  That left two parallel storage
    locations; admin could show a value the scraper ignored, and the
    scraper could write a value the admin form never surfaced.

    This migration: if ``meta.dorm_room_id`` is set AND
    ``meta.last_room_id`` is empty, copy the legacy value into the
    canonical key.  Then zero out ``dorm_room_id`` (not delete — easier
    to roll back if a future audit needs the historical value).
    """
    legacy = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (META_DORM_ROOM_ID,),
    ).fetchone()
    if not legacy or not legacy["value"]:
        return
    canonical = conn.execute(
        "SELECT value FROM meta WHERE key = ?", ("last_room_id",),
    ).fetchone()
    if not canonical or not canonical["value"]:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            ("last_room_id", legacy["value"]),
        )
    # Zero (don't delete) the legacy key so roll-back is possible
    # without a DB restore.  set_meta would open a second connection;
    # use the cursor we already have.
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (META_DORM_ROOM_ID, ""),
    )


# ---------------------------------------------------------------------------
# Legacy `records` table (untouched by Round 2)
# ---------------------------------------------------------------------------

def insert(
    remain: Optional[float],
    read_time: Optional[str],
) -> int:
    """Insert one scrape row and return the new id.

    Round 34D — the old ``raw_html`` parameter has been removed; the
    ``records.raw_html`` column no longer exists.  Callers should stop
    passing the third argument.

    Round 49 — switched from ``INSERT INTO`` to ``INSERT OR REPLACE``
    so a duplicate ``ts`` (same-second cron overlap, retry, or any
    other race) overwrites the previous row instead of stacking
    duplicates.  Pairs with the ``UNIQUE(ts)`` constraint on the
    ``records`` schema (see ``_SCHEMA`` / ``_migrate_records_unique``).
    The ``ts`` written below is second-precision
    (``strftime("%Y-%m-%d %H:%M:%S")``), so the probability of two
    honest scrapes sharing a ts is essentially zero — this path is a
    belt-and-suspenders fix for the cron-overlap dups seen in R48.
    """
    # Store `ts` in SQLite's native datetime format so it sorts and
    # compares cleanly with `datetime('now', '-N hours')` in query().
    # Round 47 — store NAIVE LOCAL (CST) time so the dashboard "采集
    # 时间" / "抄表时间" labels match the user's wall clock without any
    # browser-side re-parse.  All internal cadence / dedupe comparisons
    # were also migrated to naive local in the same commit so the math
    # is consistent end-to-end.
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT OR REPLACE INTO records (ts, read_time, remain) "
            "VALUES (?, ?, ?)",
            (ts, read_time, remain),
        )
        return int(cur.lastrowid)


def query(
    hours: int = 24,
    start_dt: Optional[str] = None,
    end_dt: Optional[str] = None,
) -> list[sqlite3.Row]:
    """Return scrape rows within the requested window.

    Three call shapes are supported (Round 33d extended this with
    explicit ``start_dt`` / ``end_dt`` for the dashboard's 采集记录
    panel — see the end of the docstring):

    1. ``query()`` or ``query(hours=N)`` — window of the last N hours
       from "now" (time-windowed, not row-bounded).  The time-windowed
       ``WHERE ts >= datetime('now', ?)`` is the correct shape for
       this query.  An earlier version of this project used ``LIMIT ?``
       with ``hours`` as the bound, which silently returned the wrong
       rows once the table held more than ``hours`` records.  Keep
       this filter on ``ts``; ``hours`` is always a window length, not
       a row count.

    2. ``query(start_dt=..., end_dt=...)`` — explicit absolute range
       (ISO-ish strings like ``"2026-09-08 00:00:00"``).  Either side
       may be ``None``; if both are ``None`` we fall back to (1).

    3. ``query(start_dt=..., hours=...)`` — when ``start_dt``/``end_dt``
       are provided they take precedence over ``hours``.  The hours
       arg is preserved only so existing callers (``/api/data?hours=N``)
       keep working without change.
    """
    # Round 33d — explicit range filter takes precedence over hours.
    if start_dt is not None or end_dt is not None:
        # Round 46 — defense-in-depth: normalize any 'T' separator to a
        # space.  records.ts is stored as "YYYY-MM-DD HH:MM:SS" and
        # SQLite string compare sees 'T'(84) > ' '(32), so a range like
        # "2026-09-15T00:00:00" silently returns zero rows even when the
        # table holds hundreds of matching records.  The frontend
        # already converts T→space before sending the request; this
        # branch lets any other caller (curl, ad-hoc scripts) pass
        # either form.
        if start_dt and "T" in start_dt:
            start_dt = start_dt.replace("T", " ")
        if end_dt and "T" in end_dt:
            end_dt = end_dt.replace("T", " ")
        with get_conn() as conn:
            clauses: list[str] = []
            params: list[Any] = []
            if start_dt:
                clauses.append("ts >= ?")
                params.append(start_dt)
            if end_dt:
                clauses.append("ts <= ?")
                params.append(end_dt)
            sql = (
                "SELECT id, ts, read_time, remain "
                "FROM records "
                f"WHERE {' AND '.join(clauses)} "
                "ORDER BY ts DESC LIMIT 1000"
            )
            return list(conn.execute(sql, params))
    if hours <= 0:
        hours = 24
    with get_conn() as conn:
        return list(
            conn.execute(
                "SELECT id, ts, read_time, remain "
                "FROM records "
                "WHERE ts >= datetime('now', ?) "
                "ORDER BY ts ASC",
                (f"-{int(hours)} hours",),
            )
        )


def latest() -> Optional[sqlite3.Row]:
    """Return the most recent row, or None if the table is empty."""
    with get_conn() as conn:
        return conn.execute(
            "SELECT id, ts, read_time, remain FROM records "
            "ORDER BY ts DESC LIMIT 1"
        ).fetchone()


# ---------------------------------------------------------------------------
# Round 2 helpers — daily_elec (F2)
# ---------------------------------------------------------------------------

def _coerce_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _coerce_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def record_daily_elec(room_id: str, rows: Iterable[dict]) -> int:
    """Upsert one or more rows into ``daily_elec`` idempotently.

    Each row may carry keys: ``dt``, ``total_eq`` (or ``totalEq``),
    ``esbm``, ``eebm``, ``zong_eq`` (or ``zongEq``), ``useEq``.
    Returns the number of rows written.

    Round 33c — semantic mapping fix:
      * F2 (``getEmDayElectQuery``) returns ``{esbm, eebm, useEq}``
        where ``eebm`` IS the cumulative end-of-day meter reading
        (in kW·h), and ``useEq`` is the per-day delta (which is
        ``eebm - esbm``).  The school's HTML 用量查询 page labels
        these as 截止表码 / 用电量.  F2 does NOT emit a ``zong_eq``
        field — if we leave the column NULL, the chart's
        ``used_today = zong - prev_zong`` runs against stale F1
        rows and produces wrong deltas (e.g. 6.70 instead of 30.83).
        **Always prefer ``eebm`` as the cumulative baseline.**
      * F1 (``selectRecord``) returns ``useEq`` = cumulative since
        meter activation (a different semantic from F2's same-named
        field).  Only used when ``eebm`` is missing.
      * ``zong_eq`` field (if present) is honored as a third-party
        override; this preserves backward-compat with any future
        endpoint that does emit it explicitly.
    """
    if not room_id:
        return 0
    n = 0
    with get_conn() as conn:
        for r in rows:
            dt = _coerce_str(r.get("dt"))
            if not dt:
                continue
            # Round 33c — populate zong_eq with the right cumulative
            # source.  Priority: eebm (F2 cumulative) > zong_eq/zongEq
            # (explicit) > useEq (F1 cumulative, only as last resort).
            zong_eq = _coerce_float(r.get("eebm"))
            if zong_eq is None:
                zong_eq = _coerce_float(r.get("zong_eq") or r.get("zongEq"))
            if zong_eq is None:
                zong_eq = _coerce_float(r.get("useEq"))
            conn.execute(
                "INSERT OR REPLACE INTO daily_elec "
                "(roomId, dt, total_eq, esbm, eebm, zong_eq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    room_id,
                    dt,
                    _coerce_float(r.get("total_eq") or r.get("totalEq")),
                    _coerce_float(r.get("esbm")),
                    _coerce_float(r.get("eebm")),
                    zong_eq,
                ),
            )
            n += 1
    return n


def recent_daily_elec(room_id: str, days: int = 30) -> list[dict]:
    """Return up to ``days`` of daily-usage rows for the dashboard chart."""
    if not room_id or days <= 0:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT dt, total_eq, esbm, eebm, zong_eq "
            "FROM daily_elec "
            "WHERE roomId = ? AND dt >= datetime('now', ?) "
            "ORDER BY dt ASC",
            (room_id, f"-{int(days)} days"),
        )]


# ---------------------------------------------------------------------------
# Round 2 helpers — violations (F3)
# ---------------------------------------------------------------------------

def record_violation(room_id: str, rows: Iterable[dict]) -> int:
    """Upsert violation rows.  Idempotent: PRIMARY KEY (roomId, dt, wg_reason)."""
    if not room_id:
        return 0
    n = 0
    with get_conn() as conn:
        for r in rows:
            dt = _coerce_str(r.get("dt"))
            reason = _coerce_str(r.get("wg_reason") or r.get("wgReasonLabel"))
            if not dt or not reason:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO violations "
                "(roomId, dt, wg_reason, wg_power) "
                "VALUES (?, ?, ?, ?)",
                (
                    room_id,
                    dt,
                    reason,
                    _coerce_float(r.get("wg_power") or r.get("wgPower")),
                ),
            )
            n += 1
    return n


def recent_violations(room_id: str, days: int = 30) -> list[dict]:
    """Return violations in the last ``days`` days, newest first."""
    if not room_id or days <= 0:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT dt, wg_reason, wg_power "
            "FROM violations "
            "WHERE roomId = ? AND dt >= datetime('now', ?) "
            "ORDER BY dt DESC",
            (room_id, f"-{int(days)} days"),
        )]


# ---------------------------------------------------------------------------
# Round 2 helpers — pay_history (F5)
# ---------------------------------------------------------------------------

def record_pay(room_id: str, rows: Iterable[dict]) -> int:
    """Upsert pay-history rows.  Idempotent on (roomId, dt, pay_type, fee_type)."""
    if not room_id:
        return 0
    n = 0
    with get_conn() as conn:
        for r in rows:
            dt = _coerce_str(r.get("dt"))
            pay_type = _coerce_str(r.get("pay_type") or r.get("payTypeLabel"))
            fee_type = _coerce_str(r.get("fee_type") or r.get("feeTypeLabel"))
            if not dt or not pay_type or not fee_type:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO pay_history "
                "(roomId, dt, pay_type, fee_type, money) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    room_id,
                    dt,
                    pay_type,
                    fee_type,
                    _coerce_float(r.get("money")),
                ),
            )
            n += 1
    return n


def recent_pay(room_id: str, days: int = 30) -> list[dict]:
    """Return payment rows in the last ``days`` days, newest first."""
    if not room_id or days <= 0:
        return []
    with get_conn() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT dt, pay_type, fee_type, money "
            "FROM pay_history "
            "WHERE roomId = ? AND dt >= datetime('now', ?) "
            "ORDER BY dt DESC",
            (room_id, f"-{int(days)} days"),
        )]


# ---------------------------------------------------------------------------
# Round 2 helpers — run_status (F4)
# ---------------------------------------------------------------------------

def upsert_run_status(room_id: str, data: dict) -> None:
    """Upsert the single-row-per-room live meter snapshot.

    ``data`` is the dict returned by
    ``/campus/webchat/dormEmRunStatus/getEmRunStatus``.  Unknown / empty
    fields are stored as NULL.  Only one row per roomId is ever kept.
    """
    if not room_id or not isinstance(data, dict):
        return
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO run_status "
            "(roomId, meter_no, dt, run_status, work_status, update_dt, "
            " vol, cur, yggl, stop_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                room_id,
                _coerce_str(data.get("meterNo")),
                _coerce_str(data.get("dt")),
                _coerce_str(data.get("runStatus")),
                _coerce_str(data.get("workStatus")),
                _coerce_str(data.get("updateDt")),
                _coerce_float(data.get("vol")),
                _coerce_float(data.get("cur")),
                _coerce_float(data.get("yggl")),
                _coerce_str(data.get("stopReason")),
            ),
        )


def get_run_status(room_id: str) -> Optional[dict]:
    """Return the live run_status row, or None if the meter was never probed."""
    if not room_id:
        return None
    with get_conn() as conn:
        row = conn.execute(
            "SELECT roomId, meter_no, dt, run_status, work_status, "
            "       update_dt, vol, cur, yggl, stop_reason "
            "FROM run_status WHERE roomId = ?",
            (room_id,),
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Round 2 helpers — meta (generic key/value)
# ---------------------------------------------------------------------------

def get_meta(key: str) -> Optional[str]:
    """Return the value for ``key`` or None if not set."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
    return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    """Insert or update a meta kv row.  Cheap; safe to call on every scrape."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, str(value)),
        )


def get_meta_float(key: str) -> Optional[float]:
    """Convenience: parse the meta value as float, or None on miss / bad value."""
    v = get_meta(key)
    if v is None:
        return None
    return _coerce_float(v)


def get_meta_int(key: str) -> Optional[int]:
    """Convenience: parse the meta value as int, or None on miss / bad value."""
    v = get_meta(key)
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Round 34D — cadence-gated fetch status helpers
# ---------------------------------------------------------------------------

def record_cadence_status(kind: str, status: str) -> None:
    """Record the result of the latest cadence-gated fetch.

    ``kind``   — one of ``"f2"``, ``"f3"``, ``"f5"`` (the F2/F3/F5
                 cadence-gated endpoints).
    ``status`` — ``"ok"`` or ``"failed"``.

    Round 34D — separate the "fetch attempt" timestamp from the
    "fetch success" timestamp.  When fetch fails, we DON'T stamp
    ``last_<kind>_at``, so the next cron cycle retries within minutes
    instead of waiting a full cadence window.
    """
    set_meta(f"last_{kind}_status", status)
    # Round 47 — naive local (CST) timestamp; see ``insert()`` for the
    # rationale.  ``last_<kind>_status_at`` is consumed by the same
    # naive-math cadence gate (``_parse_meta_ts`` → naive compare) so
    # tz tagging would cause the off-by-8h crash that R23 originally
    # fixed; we now skip the tz tag entirely.
    set_meta(f"last_{kind}_status_at", datetime.now().isoformat())


def set_scrape_status(status: str) -> None:
    """Set ``last_scrape_status`` meta.  Round 34D — make ``"failed"`` actually possible.

    Before Round 34D, the scraper raised on total primary-fetch failure,
    so ``last_scrape_status`` was effectively always ``"ok"`` in
    ``records.meta``.  This helper gives callers a place to stamp
    ``"failed"`` explicitly when the primary fetch throws, before the
    exception bubbles up.  Worker A is responsible for calling it from
    the primary-fetch path in ``dorm_power.py``.
    """
    set_meta("last_scrape_status", status)
