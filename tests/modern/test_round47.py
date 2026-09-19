"""Round 47 — migrate the entire timestamp stack from UTC to naive CST.

Background
==========
The dashboard's "采集时间" / "抄表时间" / "最后更新" labels were
displaying UTC strings in the user's browser.  A scrape written at
``datetime.now(UTC) = 2026-09-17 05:00:00`` was rendered to the user
(who lives in CST, UTC+8) as "5:00" — they actually wanted to see
"13:00".  The Round 23 tz fix made internal cadence math work, but it
didn't propagate to the user-facing render path.

Round 47 fix
============
Make the entire stack store NAIVE LOCAL (CST) timestamps end-to-end::

  * ``db.insert()``           — ``datetime.now()`` (was ``datetime.now(UTC)``)
  * ``db.record_cadence_status()`` — same
  * ``dorm_power._stamp_now()``    — same
  * ``dorm_power._date_range()``   — same
  * ``dorm_power._pay_date_range()``— same
  * ``dorm_power._is_top_of_hour()`` — naive local
  * ``dorm_power._now_beijing()``  — drops the +8h shift (no-op now)
  * ``dorm_power._parse_meta_ts()``— returns the naive datetime unchanged
  * ``dorm_power._check_stale_scrape()`` — naive compare
  * ``dorm_power._maybe_push_violation_card()`` — naive compare + no tz tag
  * ``web.py`` health check    — naive compare (drop UTC import)
  * ``web.py`` feishu capture log — naive local
  * ``web.py`` JS ``new Date(...)`` — drop the ``+ 'Z'`` suffix
  * ``feishu_bot.quiet_hours_now()`` — naive local
  * ``feishu_bot`` error timestamp — naive local
  * ``dorm_power._check_stale_scrape()`` card subtitle label — UTC → CST

Plus a one-shot ``scripts/deploy/r47_migrate_utc_to_cst.py`` that adds
+8h to every historical ``records.ts`` / ``meta.<ts-shaped-key>`` row
to keep the table internally consistent.

Coverage (5 runtime testcases + 2 static guards):

  T1  ``test_db_insert_writes_naive_local_ts``
        — ``db.insert()`` returns a ts that parses as a naive datetime
        matching ``datetime.now()`` ±60 s.  Pins the headline fix.
  T2  ``test_stamp_now_uses_naive_local``
        — ``dorm_power._stamp_now`` writes a meta value that parses
        as naive local matching ``datetime.now()`` ±60 s.
  T3  ``test_is_top_of_hour_uses_local_clock``
        — mock ``dorm_power.datetime`` to fixed hours and verify
        ``_is_top_of_hour()`` flips correctly on local minute==0.
  T4  ``test_frontend_drops_Z_suffix``
        — static guard: ``web.py`` no longer contains the literal
        ``new Date(... + 'Z')`` pattern (the round-47 frontend fix).
  T5  ``test_migration_script_shifts_utc_to_cst``
        — importlib-load the migration script, mock sqlite3, plant
        one record + one meta row, assert both UPDATEs use
        ``+8 h`` correctly.

Plus two static guards:

  S1  ``test_round47_syntax_compiles``
        — ``py_compile`` on db.py / dorm_power.py / web.py / feishu_bot.py /
        scripts/deploy/r47_migrate_utc_to_cst.py.
  S2  ``test_db_imports_drop_UTC``
        — ``db.py`` and ``dorm_power.py`` and ``web.py`` no longer
        import ``UTC`` from ``datetime``.

Run:    python -m unittest test_round47 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the
operator runs it there.
"""
from __future__ import annotations

import ast
import importlib.util
import os
import pathlib
import py_compile
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta
from unittest import mock

PROJ_DIR = pathlib.Path(__file__).resolve().parent
os.chdir(PROJ_DIR)
sys.path.insert(0, str(PROJ_DIR))

# Force fresh imports so we get the new code even if a previous test
# cached an older module version.
for mod in ("config", "db", "dorm_power", "web"):
    if mod in sys.modules:
        del sys.modules[mod]
import config  # noqa: E402
import db as _db  # noqa: E402


# ---------------------------------------------------------------------------
# Test DB scaffolding — shared in-memory SQLite so we can plant records
# at deterministic timestamps.
# ---------------------------------------------------------------------------

def _make_test_db() -> None:
    """Create an isolated in-memory SQLite with the schema initialised."""
    _make_test_db._COUNTER = getattr(_make_test_db, "_COUNTER", 0) + 1
    shared_uri = (
        f"file:test_round47_db_{_make_test_db._COUNTER}"
        f"?mode=memory&cache=shared"
    )

    pin = sqlite3.connect(shared_uri, uri=True, timeout=30,
                          isolation_level=None)
    pin.row_factory = sqlite3.Row
    pin.execute("PRAGMA foreign_keys=ON;")
    _db._TEST_CONN = pin
    _db._TEST_CONN_URI = shared_uri

    def _shared_conn():
        c = sqlite3.connect(shared_uri, uri=True, timeout=30,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON;")
        return c

    _db._ORIGINAL_GET_CONN = getattr(_db, "get_conn", None)
    _db.get_conn = _shared_conn
    pin.executescript(_db._SCHEMA)


def _restore_db() -> None:
    pin = getattr(_db, "_TEST_CONN", None)
    if pin is not None:
        try:
            pin.close()
        except sqlite3.Error:
            pass
        _db._TEST_CONN = None
        _db._TEST_CONN_URI = None
    if getattr(_db, "_ORIGINAL_GET_CONN", None):
        _db.get_conn = _db._ORIGINAL_GET_CONN
        delattr(_db, "_ORIGINAL_GET_CONN")


# ---------------------------------------------------------------------------
# Runtime tests — pin the naive-CST contract end-to-end
# ---------------------------------------------------------------------------

class TestRound47NaiveCST(unittest.TestCase):
    """T1..T3 + T5 — runtime coverage of the round-47 contract."""

    def setUp(self) -> None:
        _make_test_db()

    def tearDown(self) -> None:
        _restore_db()

    # ----------------------------------------------------------------- T1
    def test_db_insert_writes_naive_local_ts(self) -> None:
        """T1: ``db.insert()`` writes a ``records.ts`` that parses as
        a NAIVE datetime matching ``datetime.now()`` ±60 s.  Pre-R47
        this assertion would have failed because ``insert()`` wrote
        ``datetime.now(UTC)`` (8 h off from the user's wall clock).
        """
        before = datetime.now()
        new_id = _db.insert(remain=12.34, read_time="2026-09-17 13:00:00")
        after = datetime.now()

        rows = _db.query(start_dt="2000-01-01", end_dt="2099-12-31")
        self.assertEqual(
            len(rows), 1,
            "Expected exactly 1 row after insert; got "
            f"{len(rows)} — db.insert is broken.",
        )
        ts_str = rows[0]["ts"]
        # Parses as naive datetime — assert no tzinfo attached.
        ts_obj = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        self.assertIsNone(
            getattr(ts_obj, "tzinfo", None),
            "Round 47 contract: records.ts must be NAIVE local (no "
            f"tzinfo).  Got ts={ts_str!r}, tzinfo={ts_obj.tzinfo!r}.",
        )
        # And it matches the local wall clock within 60s.
        self.assertGreaterEqual(ts_obj, before - timedelta(seconds=60))
        self.assertLessEqual(ts_obj, after + timedelta(seconds=60))
        # Sanity — the row id matches what insert returned.
        self.assertEqual(rows[0]["id"], new_id)

    # ----------------------------------------------------------------- T2
    def test_stamp_now_uses_naive_local(self) -> None:
        """T2: ``dorm_power._stamp_now()`` writes a meta value that
        parses as naive local matching ``datetime.now()`` ±60 s.
        Pre-R47 this wrote ``datetime.now(UTC)``.
        """
        import dorm_power
        # Use a key that does not collide with any production meta key.
        META_KEY = "r47_test_stamp_key"
        # Clean any pre-existing value from earlier test runs.
        _db.set_meta(META_KEY, "")

        before = datetime.now()
        dorm_power._stamp_now(META_KEY)
        after = datetime.now()

        v = _db.get_meta(META_KEY)
        self.assertIsNotNone(v, "_stamp_now must write a value")
        v_obj = datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        self.assertIsNone(
            getattr(v_obj, "tzinfo", None),
            f"Round 47 contract: _stamp_now writes naive local; "
            f"got tzinfo={v_obj.tzinfo!r}.",
        )
        self.assertGreaterEqual(v_obj, before - timedelta(seconds=60))
        self.assertLessEqual(v_obj, after + timedelta(seconds=60))

    # ----------------------------------------------------------------- T3
    def test_is_top_of_hour_uses_local_clock(self) -> None:
        """T3: ``dorm_power._is_top_of_hour()`` returns True when
        ``datetime.now().minute == 0``.  Pre-R47 it returned True when
        ``datetime.now(UTC).minute == 0`` (off by 8 h from user clock).
        """
        import dorm_power
        # Mock dorm_power.datetime to return a fixed local time.
        with mock.patch.object(dorm_power, "datetime", wraps=datetime) as mock_dt:
            # Case A — local 13:00 → True
            mock_dt.now.return_value = datetime(2026, 9, 17, 13, 0, 0)
            self.assertTrue(
                dorm_power._is_top_of_hour(),
                "_is_top_of_hour() must return True when local "
                "minute == 0; failed for 13:00 mock.",
            )
            # Case B — local 13:30 → False
            mock_dt.now.return_value = datetime(2026, 9, 17, 13, 30, 0)
            self.assertFalse(
                dorm_power._is_top_of_hour(),
                "_is_top_of_hour() must return False when local "
                "minute != 0; failed for 13:30 mock.",
            )
            # Case C — local 13:59:59 → still False
            mock_dt.now.return_value = datetime(2026, 9, 17, 13, 59, 59)
            self.assertFalse(
                dorm_power._is_top_of_hour(),
                "_is_top_of_hour() must return False for minute=59.",
            )

    # ----------------------------------------------------------------- T5
    def test_migration_script_shifts_utc_to_cst(self) -> None:
        """T5: load ``r47_migrate_utc_to_cst.py`` via importlib and
        exercise ``migrate()`` against a mocked sqlite3 connection.
        Verify both the records.ts UPDATE and the meta UPDATE use the
        shifted (+8 h) value.
        """
        migration_path = (
            PROJ_DIR / "scripts" / "deploy" / "r47_migrate_utc_to_cst.py"
        )
        spec = importlib.util.spec_from_file_location(
            "r47_migrate", migration_path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        fake_conn = mock.MagicMock()
        fake_cur = mock.MagicMock()
        fake_conn.cursor.return_value = fake_cur
        # Plant one records row + one meta row to migrate.
        fake_cur.fetchall.side_effect = [
            [(1, "2026-09-17 05:00:00")],
            [("last_scrape_at", "2026-09-17 05:00:00")],
        ]

        with mock.patch.object(mod.sqlite3, "connect", return_value=fake_conn):
            mod.migrate(db_path=":memory:")

        # Verify both UPDATE calls used the +8h value (13:00:00, not 05:00:00).
        all_calls = [
            str(call) for call in fake_cur.execute.call_args_list
        ]
        joined = "\n".join(all_calls)

        # Records UPDATE: id=1, ts shifted to 13:00:00.
        records_ok = any(
            "UPDATE records" in c
            and "2026-09-17 13:00:00" in c
            and "WHERE id = 1" in c
            for c in all_calls
        )
        self.assertTrue(
            records_ok,
            "Migration must UPDATE records SET ts='2026-09-17 13:00:00' "
            "WHERE id=1; calls seen:\n" + joined,
        )

        # Meta UPDATE: key=last_scrape_at, value shifted to 13:00:00.
        meta_ok = any(
            "UPDATE meta" in c
            and "last_scrape_at" in c
            and "2026-09-17 13:00:00" in c
            for c in all_calls
        )
        self.assertTrue(
            meta_ok,
            "Migration must UPDATE meta SET value='2026-09-17 13:00:00' "
            "WHERE key='last_scrape_at'; calls seen:\n" + joined,
        )

        # And the script must commit the transaction.
        self.assertTrue(
            fake_conn.commit.called,
            "Migration must call conn.commit() before returning.",
        )


# ---------------------------------------------------------------------------
# Static guards — catch the wiring even if a runtime test is skipped
# ---------------------------------------------------------------------------

class TestRound47StaticGuards(unittest.TestCase):
    """AST-level + regex guards that pin the round-47 edits."""

    DB_PY = PROJ_DIR / "db.py"
    DORM_POWER_PY = PROJ_DIR / "dorm_power.py"
    WEB_PY = PROJ_DIR / "web.py"
    FEISHU_BOT_PY = PROJ_DIR / "feishu_bot.py"
    MIGRATION_PY = (
        PROJ_DIR / "scripts" / "deploy" / "r47_migrate_utc_to_cst.py"
    )

    # ----------------------------------------------------------------- S1
    def test_round47_syntax_compiles(self) -> None:
        """S1: ``py_compile`` round-trip on all edited files plus the
        new migration script.  Catches typos at the bytecode level.
        """
        py_compile.compile(str(self.DB_PY), doraise=True)
        py_compile.compile(str(self.DORM_POWER_PY), doraise=True)
        py_compile.compile(str(self.WEB_PY), doraise=True)
        py_compile.compile(str(self.FEISHU_BOT_PY), doraise=True)
        py_compile.compile(str(self.MIGRATION_PY), doraise=True)

    # ----------------------------------------------------------------- S2
    def test_db_imports_drop_UTC(self) -> None:
        """S2: ``db.py``, ``dorm_power.py``, ``web.py`` MUST NOT import
        ``UTC`` from ``datetime``.  Round 47 removed it because the
        whole stack now uses naive local time; leaving the import
        would mean a future bug could silently re-introduce tz-aware
        math.
        """
        for path, label in [
            (self.DB_PY, "db.py"),
            (self.DORM_POWER_PY, "dorm_power.py"),
            (self.WEB_PY, "web.py"),
        ]:
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.module != "datetime":
                    continue
                names = [n.name for n in node.names]
                self.assertNotIn(
                    "UTC", names,
                    f"{label} still imports UTC from datetime — "
                    "round 47 must drop it.",
                )

    # ----------------------------------------------------------------- T4
    def test_frontend_drops_Z_suffix(self) -> None:
        """T4: the ``web.py`` JavaScript that parses ``data-ts`` MUST
        NOT append ``'Z'`` anymore.  Pre-R47 the JS did
        ``new Date(tsRaw.replace(' ', 'T') + 'Z')`` which forced the
        browser to interpret the (now-CST) string as UTC, shifting the
        "在线/离线" pill by 8 h.
        """
        src = self.WEB_PY.read_text(encoding="utf-8")
        # Search the legacy pattern.  We look for the substring
        # ``"T') + 'Z'"`` — that's the exact suffix the round-47 fix
        # removed.  A single occurrence anywhere is enough to fail.
        self.assertNotIn(
            "T') + 'Z'",
            src,
            "Round 47 frontend fix missing — web.py JS still does "
            "new Date(... + 'Z') which re-shifts the CST string by "
            "8 h back to UTC.  Drop the 'Z' suffix.",
        )

    # -----------------------------------------------------------------
    def test_db_insert_uses_naive_now(self) -> None:
        """Bonus static guard: ``db.insert()`` MUST call
        ``datetime.now()`` (no ``UTC`` arg).  Pins the headline fix at
        the AST level so a future regression that re-introduces
        ``datetime.now(UTC)`` is caught even if the runtime test is
        skipped due to environment quirks.
        """
        tree = ast.parse(self.DB_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "insert"):
                continue
            src = ast.unparse(node)
            # The naive form is present.
            self.assertIn(
                "datetime.now()",
                src,
                "db.insert must call datetime.now() (round 47).",
            )
            # The tz-aware form is gone.
            self.assertNotIn(
                "datetime.now(UTC)",
                src,
                "db.insert still calls datetime.now(UTC); round 47 "
                "must drop the tz arg.",
            )
            return
        self.fail("db.insert() function definition missing")


if __name__ == "__main__":
    unittest.main(verbosity=2)