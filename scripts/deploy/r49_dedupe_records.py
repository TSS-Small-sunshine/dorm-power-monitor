"""r49_dedupe_records.py — Round 49 data migration.

Background
==========
Pre-R49 the ``records`` table schema was::

    CREATE TABLE records (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ts         TEXT    NOT NULL,
        read_time  TEXT,
        remain     REAL
    );
    CREATE INDEX idx_records_ts ON records(ts);

with no ``UNIQUE(ts)`` constraint, and ``db.insert()`` used plain
``INSERT INTO``.  Production data accumulated 177 dup rows over the
863 total / 686 unique ts (e.g. same ts scraped at 12:00 vs 20:00 with
different ``read_time`` / ``remain``).  The dashboard line chart drew a
"锯齿" — alternating 180/171 jumps between duped points.

R49 fix is two halves:

1.  Schema + writer — ``db.py`` adds ``UNIQUE(ts)`` to ``records``
    (fresh installs via ``_SCHEMA``; existing installs via the
    idempotent ``_migrate_records_unique`` helper called from
    ``db.init()``).  ``db.insert()`` now uses ``INSERT OR REPLACE``
    so any future duplicate ts is silently overwritten with the freshest
    read instead of stacked.

2.  Cleanup of the historical dup rows that pre-date the constraint.
    That's what THIS script does:

    a.  For each duplicated ``ts``, keep ONE row — the one with the
        smallest ``remain`` (电表 remain 是单调递减: 读数越小 = 越新).
        All other rows for that ``ts`` are deleted.

    b.  After dedupe, the production table already matches the new
        UNIQUE constraint, so ``db.init()``'s
        ``_migrate_records_unique`` is a no-op on the rebuilt table.
        We still log whether the constraint is present for visibility.

Operational notes
=================
*  MUST run while ``dorm-web`` is stopped so a concurrent writer
   (the cron, the dashboard's ``/api/admin`` endpoint, etc.) doesn't
   insert into a half-migrated table.  ``r49_deploy.sh`` stops the
   service before invoking this script and starts it back up only on
   success.
*  Idempotent: running twice produces the same end state.  The dedupe
   DELETE only fires on rows that are still duplicates of the surviving
   one, and the second run sees zero dups so the WHERE matches zero
   rows.
*  Default ``DB_PATH`` is the production
   ``/var/lib/dorm-power-monitor/dorm.db``.  Override via ``argv[1]``
   for dev work.  No Flask / config import — this script must NOT
   accidentally touch the wrong DB.

Usage on server
===============

    systemctl stop dorm-web                       # done by r49_deploy.sh
    python3 /opt/dorm-power-monitor/scripts/deploy/r49_dedupe_records.py
    systemctl start dorm-web                      # done by r49_deploy.sh

Exit code is 0 on success; non-zero on a fatal SQLite error.  Migration
counts are printed to stdout so the operator can spot-check the
magnitude ("we had N rows before, expect unique-ts after; if unique ==
total, the dedupe plus the rebuild succeeded").
"""
from __future__ import annotations

import os
import sqlite3
import sys

# Default to the production path.  Override via env or argv[1] when
# running on a dev box: ``DB_PATH=/tmp/dorm.db python3
# r49_dedupe_records.py /tmp/dorm.db``.  The script never imports the
# Flask app, so it cannot accidentally touch the wrong DB.
DEFAULT_DB_PATH = os.environ.get(
    "DB_PATH",
    "/var/lib/dorm-power-monitor/dorm.db",
)


def _has_unique_records(conn: sqlite3.Connection) -> bool:
    """Return True if ``records`` already has a UNIQUE index on ``ts``."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type = 'index' AND tbl_name = 'records'"
    ).fetchall()
    return any(r[0] and "UNIQUE" in r[0].upper() for r in rows)


def _rebuild_with_unique(conn: sqlite3.Connection) -> None:
    """Rebuild ``records`` with ``UNIQUE(ts)``.

    SQLite does not support ``ALTER TABLE ADD CONSTRAINT``, so the
    only safe path is CREATE new -> copy -> DROP + RENAME.  The
    rebuild runs inside a single transaction so a crash mid-rebuild
    leaves the original intact.
    """
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


def dedupe(db_path: str = DEFAULT_DB_PATH) -> None:
    """Open ``db_path`` in-place, dedupe, and rebuild with UNIQUE.

    Caller is expected to have already stopped ``dorm-web`` so the
    table doesn't get a concurrent insert mid-flight.
    """
    print(f"[r49] opening {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    cur = conn.cursor()

    # ---- Step 1: report starting state ----------------------------------
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT ts) FROM records;")
    total, unique_ts = cur.fetchone()
    dup_count = total - unique_ts
    print(
        f"[r49] before: total={total} unique_ts={unique_ts} "
        f"duplicates={dup_count}"
    )

    # ---- Step 2: dedupe --------------------------------------------------
    # For each duplicated ts, keep the row with the SMALLEST remain
    # (电表 remain 是单调递减 — 读数越小 = 越新).  Tiebreak by largest
    # id (the most recently inserted row wins when remain is equal).
    deleted = 0
    if dup_count > 0:
        cur.execute(
            """
            DELETE FROM records
            WHERE id NOT IN (
                SELECT id FROM records r1
                WHERE r1.id = (
                    SELECT id FROM records r2
                    WHERE r2.ts = r1.ts
                    ORDER BY r2.remain ASC, r2.id DESC
                    LIMIT 1
                )
            );
            """
        )
        deleted = cur.rowcount
        print(f"[r49] dedupe: deleted {deleted} duplicate rows")
    else:
        print("[r49] dedupe: no duplicates to clean")

    # ---- Step 3: rebuild if missing UNIQUE ------------------------------
    if not _has_unique_records(conn):
        print("[r49] rebuilding records table to add UNIQUE(ts)...")
        _rebuild_with_unique(conn)
        print("[r49] records table rebuilt with UNIQUE(ts)")
    else:
        print("[r49] records table already has UNIQUE constraint")

    # ---- Step 4: verify --------------------------------------------------
    cur.execute("SELECT COUNT(*), COUNT(DISTINCT ts) FROM records;")
    after_total, after_unique = cur.fetchone()
    print(
        f"[r49] after: total={after_total} unique_ts={after_unique} "
        f"dup_remaining={after_total - after_unique}"
    )

    conn.commit()
    conn.close()

    if after_total != after_unique:
        print(
            f"FATAL post-dedupe still has "
            f"{after_total - after_unique} duplicate rows — manual review",
            file=sys.stderr,
        )
        sys.exit(2)
    print()
    print("⚠ Restart dorm-web NOW.")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB_PATH
    try:
        dedupe(target)
    except sqlite3.Error as exc:
        print(
            f"FATAL sqlite error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)