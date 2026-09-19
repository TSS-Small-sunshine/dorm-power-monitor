"""Round 49 — records 表 UNIQUE(ts) 约束 + db.insert 改 INSERT OR REPLACE.

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

and ``db.insert()`` used plain ``INSERT INTO``.  Production data
accumulated 177 dup rows over 863 total / 686 unique ts — same ``ts``
scraped twice (cron overlap / insert race) with different ``read_time``
and ``remain`` values stacked, producing a "锯齿" line chart.

R49 fix
=======
1.  ``records.ts`` gains a ``UNIQUE`` constraint so SQLite itself
    rejects duplicate inserts.
2.  ``db.insert()`` now uses ``INSERT OR REPLACE`` so any future
    same-ts write overwrites the previous row instead of erroring.
3.  ``db.init()`` adds ``_migrate_records_unique`` — idempotent
    rebuild of the table on existing installs that lack the UNIQUE
    constraint.  New installs pick it up from ``_SCHEMA``.
4.  ``scripts/deploy/r49_dedupe_records.py`` cleans up historical
    duplicates (deletes all but the lowest-remain row per ts) and
    rebuilds the table to enforce UNIQUE.

Coverage (5 static / dynamic testcases):

  T1  ``test_schema_has_unique_constraint``
        — Regex guard.  Verifies ``db.py``'s ``_SCHEMA`` string
        declares ``ts TEXT NOT NULL UNIQUE`` for the records table.
        Pre-R49 this was just ``ts TEXT NOT NULL``.
  T2  ``test_db_insert_uses_insert_or_replace``
        — Text guard.  Verifies ``db.insert()`` uses
        ``INSERT OR REPLACE INTO records`` and does NOT use plain
        ``INSERT INTO records``.  Pairs with T1 — the constraint AND
        the writer both have to change to actually prevent future dups.
  T3  ``test_insert_or_replace_overwrites_duplicate``
        — Runtime repro.  Monkey-patches ``db.DB_PATH`` (via the
        ``config`` module) to a tempfile, calls ``db.init()`` then
        ``db.insert()`` twice with the same ts, asserts exactly one row
        survives and its remain reflects the second call.  Pins the
        behavioural contract that T2 only asserts textually.
  T4  ``test_migration_script_rebuilds_table``
        — Static guard.  Walks ``r49_dedupe_records.py`` source to
        assert it contains the CREATE TABLE records_new / INSERT /
        DROP / RENAME sequence that SQLite needs to add a UNIQUE
        constraint to an existing table.  Pairs with the deployment
        story in ``r49_deploy.sh``.
  T5  ``test_all_target_files_py_compile``
        — ``py_compile`` the three files we touched.  Catches
        syntax / encoding issues without running the code (per the
        project rule "don't run pytest locally").
"""
from __future__ import annotations

import os
import re
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
DB_PY = PROJ_DIR / "db.py"
MIGRATION_PY = PROJ_DIR / "scripts" / "deploy" / "r49_dedupe_records.py"


def _read(path: Path) -> str:
    """Return file contents as text (utf-8).  Helper for the static guards."""
    return path.read_text(encoding="utf-8")


# =========================================================================
# T1 — _SCHEMA declares UNIQUE(ts)
# =========================================================================
class TestSchemaHasUniqueConstraint(unittest.TestCase):
    """db.py _SCHEMA records block must declare ts TEXT NOT NULL UNIQUE."""

    def test_schema_has_unique_constraint(self) -> None:
        src = _read(DB_PY)
        # Pull the CREATE TABLE IF NOT EXISTS records (...) block.
        pattern = (
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+records\s*\(([^)]+)\)"
        )
        m = re.search(pattern, src, re.IGNORECASE | re.DOTALL)
        self.assertIsNotNone(
            m,
            "CREATE TABLE IF NOT EXISTS records (...) block not found "
            "in db.py",
        )
        body = m.group(1)
        # Normalise whitespace so layout drift doesn't break the assertion.
        body_flat = re.sub(r"\s+", " ", body).strip()
        self.assertIn(
            "ts TEXT NOT NULL UNIQUE",
            body_flat,
            "records table must declare `ts TEXT NOT NULL UNIQUE`. "
            f"Got body: {body_flat!r}",
        )
        # Also assert the bare ``ts TEXT NOT NULL`` (without UNIQUE)
        # is NOT still there — defence against a partial edit.
        self.assertNotIn(
            "ts TEXT NOT NULL,",
            body_flat,
            "records.ts lost its UNIQUE tag — bare `ts TEXT NOT NULL,` "
            "should not survive in the schema.",
        )


# =========================================================================
# T2 — db.insert uses INSERT OR REPLACE
# =========================================================================
class TestDbInsertUsesInsertOrReplace(unittest.TestCase):
    """db.insert() must use INSERT OR REPLACE INTO records, not plain INSERT."""

    def test_db_insert_uses_insert_or_replace(self) -> None:
        src = _read(DB_PY)
        self.assertIn(
            "INSERT OR REPLACE INTO records",
            src,
            "db.insert() must use `INSERT OR REPLACE INTO records` "
            "so a duplicate ts silently overwrites the previous row.",
        )
        # Defence: no plain ``INSERT INTO records`` should remain in
        # db.insert().  We check the absence of the literal prefix.
        self.assertNotIn(
            '"INSERT INTO records',
            src,
            "db.insert() should not use plain `INSERT INTO records` — "
            "the pre-R49 code path is gone.",
        )


# =========================================================================
# T3 — runtime: INSERT OR REPLACE actually overwrites
# =========================================================================
class TestInsertOrReplaceOverwritesDuplicate(unittest.TestCase):
    """End-to-end runtime repro: two inserts with the same ts collapse to one row."""

    def test_insert_or_replace_overwrites_duplicate(self) -> None:
        # We monkey-patch config.DB_PATH BEFORE importing db so the
        # module-level DB_PATH (or the get_conn call inside db) picks
        # up the tempfile.  db.py reads ``config.DB_PATH`` lazily inside
        # ``get_conn()`` so changing config is enough.
        import config  # noqa: WPS433 — intentional import for monkey-patch

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        tmp_path = tmp.name
        try:
            # Stash and swap.
            original_path = config.DB_PATH
            config.DB_PATH = tmp_path

            # Import db after the swap so any module-level capture
            # picks up the new path.  In this codebase db.get_conn()
            # reads config.DB_PATH at call time so even a pre-import
            # swap would work; we still re-import defensively.
            if "db" in sys.modules:
                del sys.modules["db"]
            import db  # noqa: WPS433

            db.init()

            # Two inserts at the same second — even if datetime.now()
            # would tick between calls, our ts is set internally via
            # strftime("%Y-%m-%d %H:%M:%S") so we monkey-patch it to a
            # fixed string so the test is deterministic.
            fixed_ts = "2026-09-17 10:00:00"

            def _fake_strftime(_fmt: str) -> str:
                return fixed_ts

            # Patch the ``datetime`` reference inside the db module
            # so the second call returns the same ts as the first.
            import datetime as _dt_mod

            class _FakeDatetime:
                @staticmethod
                def now() -> _FakeDatetime:
                    return _FakeDatetime()

                def strftime(self, _fmt: str) -> str:
                    return fixed_ts

            original_datetime = db.datetime
            db.datetime = _FakeDatetime  # type: ignore[assignment]

            try:
                db.insert(100.0, "2026-09-17 10:00:00")
                db.insert(110.0, "2026-09-17 10:00:05")
            finally:
                db.datetime = original_datetime  # type: ignore[assignment]

            # Read back — must be exactly one row with remain=110.
            conn = sqlite3.connect(tmp_path)
            try:
                rows = conn.execute(
                    "SELECT ts, remain, read_time FROM records"
                ).fetchall()
                self.assertEqual(
                    len(rows),
                    1,
                    f"Expected 1 row after INSERT OR REPLACE, got "
                    f"{len(rows)}: {rows!r}",
                )
                ts, remain, read_time = rows[0]
                self.assertEqual(ts, fixed_ts)
                self.assertEqual(
                    remain,
                    110.0,
                    "Second insert() should have overwritten the first — "
                    f"got remain={remain}",
                )
                self.assertEqual(
                    read_time,
                    "2026-09-17 10:00:05",
                    "read_time should reflect the second call's value",
                )
            finally:
                conn.close()
        finally:
            os.unlink(tmp_path)


# =========================================================================
# T4 — migration script rebuilds the table
# =========================================================================
class TestMigrationScriptRebuildsTable(unittest.TestCase):
    """r49_dedupe_records.py must contain the SQLite rebuild dance."""

    def test_migration_script_rebuilds_table(self) -> None:
        src = _read(MIGRATION_PY)
        # All four DDL statements the rebuild needs.
        required = [
            "CREATE TABLE records_new",
            "INSERT INTO records_new",
            "DROP TABLE records",
            "ALTER TABLE records_new RENAME TO records",
        ]
        for needle in required:
            self.assertIn(
                needle,
                src,
                f"r49_dedupe_records.py missing required SQL fragment: "
                f"{needle!r}",
            )
        # Defence — the dedupe DELETE must exist (we're not just
        # rebuilding the schema, we're cleaning history too).
        self.assertIn(
            "DELETE FROM records",
            src,
            "r49_dedupe_records.py must DELETE historical duplicate rows, "
            "not just rebuild the schema.",
        )
        # Defence — the new table must include UNIQUE.
        # Find the CREATE TABLE records_new block specifically.
        m = re.search(
            r"CREATE\s+TABLE\s+records_new\s*\(([^)]+)\)",
            src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "CREATE TABLE records_new block not found in migration script",
        )
        body_flat = re.sub(r"\s+", " ", m.group(1)).strip()
        self.assertIn(
            "UNIQUE",
            body_flat.upper(),
            "records_new schema must declare a UNIQUE constraint; "
            f"got body: {body_flat!r}",
        )


# =========================================================================
# T5 — py_compile every file we changed
# =========================================================================
class TestAllTargetFilesPyCompile(unittest.TestCase):
    """All three touched files must parse without syntax errors."""

    def test_all_target_files_py_compile(self) -> None:
        import py_compile

        targets = [
            DB_PY,
            PROJ_DIR / "tests" / "modern" / "test_round49.py",
            MIGRATION_PY,
        ]
        for path in targets:
            with self.subTest(path=str(path)):
                py_compile.compile(str(path), doraise=True)
                print(f"  [py_compile OK] {path.name}")


# =========================================================================
# Run with ``python -m unittest tests.modern.test_round49`` (or pytest).
# =========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)