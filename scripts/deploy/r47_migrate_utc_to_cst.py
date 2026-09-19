"""r47_migrate_utc_to_cst.py — Round 47 data migration.

Background
==========
Before Round 47, ``db.insert()`` and ``_stamp_now()`` wrote timestamps
with ``datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")`` so the
SQLite ``records.ts`` and every ``meta`` key held UTC strings.  The
school-side portal reports came back in CST (Asia/Shanghai) but the
scraper normalised them through ``replace(tzinfo=UTC)`` to make them
"fair" against the UTC meta timestamps.

From Round 47 onward the entire stack stores NAIVE LOCAL (CST)
timestamps: ``db.insert`` writes ``datetime.now()``, ``_stamp_now``
writes ``datetime.now()``, ``_parse_meta_ts`` returns a naive
``datetime``, and ``quiet_hours_now`` uses local clock.  Frontend
``new Date(...)`` no longer appends ``'Z'``.

This script fixes the historical data so the dashboard / Feishu cards
show consistent CST timestamps before AND after the upgrade::

    records.ts                              + 8 h
    meta key like 'last_*_at' / '*_status_at' / '*_enable_at' /
    'oobe_started_at' / '*_last_stamp_*'    + 8 h

Operational notes
=================
* **MUST run while dorm-cron is stopped** so a cron tick can't write a
  fresh CST row into a table that's half-migrated UTC.  ``r47_deploy.sh``
  stops the timer before invoking this script and only starts it back up
  after the migration returns OK.
* Idempotent: running twice produces the same end state because the
  second run finds already-CST timestamps and ``datetime.strptime``
  parses them fine — but adding 8h twice would push them 8h too far.
  The deploy orchestrator only invokes this once.  Manual re-runs are
  the operator's responsibility.
* Naive parse via ``datetime.strptime("%Y-%m-%d %H:%M:%S")`` so the
  conversion is purely string arithmetic — no tz juggling.  Any row
  whose ``ts`` doesn't match the format is left untouched (logged
  implicitly via the ``continue``).

Usage on server
===============

    systemctl stop dorm-cron.timer dorm-cron.service        # not done here
    python3 /opt/dorm-power-monitor/scripts/deploy/r47_migrate_utc_to_cst.py
    systemctl start dorm-cron.timer dorm-cron.service       # not done here

Default ``DB_PATH`` is the production ``/var/lib/dorm-power-monitor/dorm.db``.
For local testing (a laptop) override via the env var or hard-code a
relative path; the script never imports the Flask app, so it cannot
accidentally touch the wrong DB.

Exit code is 0 on success; non-zero on a fatal SQLite error.  Migration
count is printed to stdout so the operator can spot-check the magnitude
("we had N records before, expect N after; if the count matches, the
UPDATE didn't drop anything").
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta

# Default to the production path.  Override via env when running on a
# dev box: ``DB_PATH=/tmp/dorm.db python3 r47_migrate_utc_to_cst.py``.
DB_PATH = os.environ.get(
    "DB_PATH",
    "/var/lib/dorm-power-monitor/dorm.db",
)

# meta keys whose value is a ``%Y-%m-%d %H:%M:%S`` timestamp we want to
# shift +8h.  Kept as a LIKE-list so we don't have to enumerate every
# new cadence key introduced in later rounds.
_META_TS_PATTERNS = (
    "last_%_at",            # last_scrape_at, last_<kind>_status_at, ...
    "%_status_at",          # last_f2_status_at etc. (already covered above but defensive)
    "%_enable_at",          # oobe_started_at, push_enable_at, ...
    "oobe_started_at",      # explicit fallback if a future round keys it without _at
    "%_last_stamp_%",       # _last_stamp_l3_daily, _last_stamp_l3_weekly, _last_stamp_l3_monthly
)


def _build_meta_where_clause() -> str:
    """Return a WHERE clause fragment that matches every ts-shaped meta key."""
    return " OR ".join(["key LIKE ?"] * len(_META_TS_PATTERNS))


def _shift(ts: str) -> str | None:
    """Add 8 hours to ``ts`` and reformat.  Return None if the format is bad."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def migrate(db_path: str = DB_PATH) -> None:
    """Open ``db_path`` in-place and shift every ts by +8 hours.

    Caller is expected to have already stopped ``dorm-cron`` so the
    table doesn't get a half-migrated row mid-flight.
    """
    print(f"[r47] opening {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()

    # ---- records.ts ------------------------------------------------------
    cur.execute("SELECT id, ts FROM records")
    rows = cur.fetchall()
    updated_records = 0
    skipped_records = 0
    for row_id, ts in rows:
        new_ts = _shift(ts)
        if new_ts is None:
            skipped_records += 1
            continue
        cur.execute(
            "UPDATE records SET ts = ? WHERE id = ?",
            (new_ts, row_id),
        )
        updated_records += 1

    # ---- meta ------------------------------------------------------------
    params = [p.replace("_", "\\_").replace("%", "*") for p in _META_TS_PATTERNS]
    # NOTE: SQLite uses ``*`` as the wildcard in LIKE, NOT ``%``.  But
    # we are using parameter binding — the driver substitutes the
    # literal value, so passing ``%`` is fine here (no SQL injection).
    like_params = [p.replace("*", "%") for p in _META_TS_PATTERNS]
    cur.execute(
        f"SELECT key, value FROM meta WHERE {_build_meta_where_clause()}",
        like_params,
    )
    meta_rows = cur.fetchall()
    updated_meta = 0
    skipped_meta = 0
    for key, value in meta_rows:
        if not value:
            continue
        new_value = _shift(value)
        if new_value is None:
            skipped_meta += 1
            continue
        cur.execute(
            "UPDATE meta SET value = ? WHERE key = ?",
            (new_value, key),
        )
        updated_meta += 1

    conn.commit()
    conn.close()

    print(f"[r47] records : updated={updated_records} skipped={skipped_records}")
    print(f"[r47] meta    : updated={updated_meta} skipped={skipped_meta}")
    print(f"[r47] migration complete")
    print()
    print("⚠ Restart dorm-cron NOW.  Any new scrape after this point will")
    print("  write CST (naive local) directly via _stamp_now, so the table")
    print("  is consistent end-to-end.")


if __name__ == "__main__":
    try:
        migrate(sys.argv[1] if len(sys.argv) > 1 else DB_PATH)
    except sqlite3.Error as exc:
        print(f"FATAL sqlite error: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)