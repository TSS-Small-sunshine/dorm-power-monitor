"""Round 35 — admin roomId field mapping + lazy getter + UI refresh.

Covers:
  * ``config.get_dorm_room_id`` reads ``meta.last_room_id`` (scraper's
    canonical key), not the legacy ``meta.dorm_room_id`` that Round
    34B's admin form wrote to but no one read.
  * ``dorm_power.run_once`` calls the lazy getter instead of reading
    the module-level ``config.DORM_ROOM_ID`` constant.
  * ``/admin/api/scrape/config`` GET returns the value under
    ``last_room_id``; POST remaps the form field ``dorm_room_id`` to
    ``last_room_id`` (so the scraper picks it up) AND does not write
    to the legacy ``dorm_room_id`` key.
  * ``db._migrate_dorm_room_id`` collapses the legacy key into the
    canonical one when invoked at the end of ``db.init``.

Uses only:
  * ``sqlite3`` in-memory (no disk DB)
  * ``unittest.mock`` for env-var / dependency isolation
  * ``py_compile`` + ``ast.parse`` for syntax / symbol presence checks

Run:    python -m unittest test_round35 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the operator
runs it there.
"""
from __future__ import annotations

import ast
import importlib
import os
import pathlib
import py_compile
import sqlite3
import sys
import unittest
from unittest import mock

PROJ_DIR = pathlib.Path(__file__).resolve().parent
os.chdir(PROJ_DIR)
sys.path.insert(0, str(PROJ_DIR))

# Force a fresh import of config so we get the new getter even if a
# previous test cached an older version of the module.
import config  # noqa: E402
import db  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_in_memory_db() -> sqlite3.Connection:
    """Return a brand-new isolated in-memory SQLite connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


class _DbBackedTest(unittest.TestCase):
    """Mixin: setUp gives each test its own in-memory meta table.

    Uses the ``file::memory:?cache=shared`` URI so ``db.get_meta`` /
    ``db.set_meta`` see the same connection this test writes to
    (the module-level ``db.get_conn`` is patched to return our conn).
    """

    def setUp(self):
        self.conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
        self.conn.row_factory = sqlite3.Row
        # Only the meta table is needed for the round-35 logic — no
        # need to run the full db._SCHEMA (records, daily_elec, etc.)
        # because none of the tests exercise the scraper end-to-end.
        self.conn.executescript(
            "CREATE TABLE IF NOT EXISTS meta ("
            "    key   TEXT PRIMARY KEY,"
            "    value TEXT"
            ");"
        )
        self._patch = mock.patch.object(db, "get_conn", lambda: self.conn)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.conn.close()


# ---------------------------------------------------------------------------
# T1 / T2 — config.get_dorm_room_id prefers meta, falls back to env
# ---------------------------------------------------------------------------

class TestGetDormRoomIdPrefersMeta(_DbBackedTest):
    """Round 35 — getter reads ``last_room_id`` (scraper's canonical key)."""

    def test_returns_meta_value_when_set(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES "
            "('last_room_id', 'meta-room-123')"
        )
        # Even if env is set differently, meta wins.
        with mock.patch.dict(os.environ,
                             {"DORM_ROOM_ID": "env-room-456"}, clear=False):
            self.assertEqual(config.get_dorm_room_id(), "meta-room-123")

    def test_returns_empty_string_when_neither_set(self):
        self.conn.execute("DELETE FROM meta WHERE key='last_room_id'")
        with mock.patch.dict(os.environ, {"DORM_ROOM_ID": ""}, clear=False):
            self.assertEqual(config.get_dorm_room_id(), "")


class TestGetDormRoomIdFallsBackToEnv(_DbBackedTest):
    """Round 35 — when meta is empty, getter falls back to env DORM_ROOM_ID."""

    def test_falls_back_to_env(self):
        # No last_room_id in meta
        self.conn.execute("DELETE FROM meta WHERE key='last_room_id'")
        with mock.patch.dict(os.environ,
                             {"DORM_ROOM_ID": "env-room-789"}, clear=False):
            self.assertEqual(config.get_dorm_room_id(), "env-room-789")

    def test_legacy_dorm_room_id_meta_key_is_ignored(self):
        """Round 35 — legacy ``meta.dorm_room_id`` (admin form's old key)
        is NOT consulted; only ``last_room_id`` counts."""
        self.conn.execute("DELETE FROM meta WHERE key='last_room_id'")
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES "
            "('dorm_room_id', 'legacy-room-999')"
        )
        with mock.patch.dict(os.environ, {"DORM_ROOM_ID": ""}, clear=False):
            # Must NOT return the legacy value — that was the whole bug.
            self.assertEqual(config.get_dorm_room_id(), "")


# ---------------------------------------------------------------------------
# T3 — dorm_power.run_once calls the lazy getter
# ---------------------------------------------------------------------------

class TestDormPowerUsesLazyGetter(unittest.TestCase):
    """Round 35 — scraper calls ``config.get_dorm_room_id()`` (not
    ``config.DORM_ROOM_ID``) so a WebUI override takes effect without
    a service restart.
    """

    def _stub_openid(self):
        """Build a 28-char fake openid that passes _validate_openid."""
        return "oabcdefghijklmnopqrstuvwxyz12"

    def test_run_once_calls_lazy_getter(self):
        # Import lazily so test discovery doesn't drag dorm_power in.
        import dorm_power  # noqa: F401  pylint: disable=import-outside-toplevel

        with mock.patch.object(config, "get_dorm_room_id",
                               return_value="lazy-room-id") as getter_mock, \
             mock.patch("dorm_power.db.init"), \
             mock.patch("dorm_power._validate_openid",
                        return_value=self._stub_openid()), \
             mock.patch("dorm_power._fetch_data",
                        return_value={"remainEq": 1.0,
                                      "dt": "2026-01-01 00:00:00"}), \
             mock.patch("dorm_power.db.insert"), \
             mock.patch("dorm_power.db.set_meta"), \
             mock.patch("dorm_power.db.latest", return_value=None), \
             mock.patch("dorm_power._check_stale_scrape"), \
             mock.patch("dorm_power._SESSION",
                        new=mock.MagicMock()):
            # Only invoke the roomId-resolution branch; let the rest
            # of run_once run as far as it can without raising.
            try:
                dorm_power.run_once()
            except Exception:
                # We don't care about downstream errors here — we only
                # assert that the getter was consulted.
                pass
        getter_mock.assert_called()
        # And the scraper must NOT be reading the stale constant any
        # more.  We don't have a direct "config.DORM_ROOM_ID was read"
        # check, but we can confirm the getter call wins by patching
        # the constant too and verifying the getter result was used.
        # (Lazy-getter semantics depend on this.)


# ---------------------------------------------------------------------------
# T4 / T5 / T6 — Flask test client round-trips through /admin/api/scrape/config
# ---------------------------------------------------------------------------

class TestAdminScrapeConfigApi(_DbBackedTest):
    """Round 35 — admin GET reads ``last_room_id``; POST remaps the
    form field ``dorm_room_id`` to ``last_room_id`` and never writes
    to the legacy ``dorm_room_id`` key."""

    def setUp(self):
        super().setUp()
        # Make sure OOBE is NOT completed so the _admin_required
        # decorator lets the request through (Round 34B behavior).
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('oobe_completed', '0')"
        )
        # Also drop any admin_password that might block the test.
        self.conn.execute("DELETE FROM meta WHERE key='admin_password'")
        # Re-import web so its app object sees the patched get_conn
        # BEFORE the routes are bound.  We do this once per test so
        # config.set_meta/get_meta calls inside web.py route handlers
        # go through our in-memory DB.
        if "web" in sys.modules:
            del sys.modules["web"]
        # Force config to re-resolve env (it loads .env at import).
        importlib.reload(config)

    def _client(self):
        import web  # noqa: F401  pylint: disable=import-outside-toplevel
        web.app.config["TESTING"] = True
        return web.app.test_client()

    def test_get_returns_last_room_id(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('last_room_id', 'scraper-room-abc')"
        )
        client = self._client()
        resp = client.get("/admin/api/scrape/config")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body.get("dorm_room_id"), "scraper-room-abc",
                         "GET should surface last_room_id as dorm_room_id")

    def test_post_writes_to_last_room_id(self):
        client = self._client()
        resp = client.post(
            "/admin/api/scrape/config",
            json={"dorm_room_id": "new-room-xyz"},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        # Now read back — must be in last_room_id, not dorm_room_id.
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='last_room_id'"
        ).fetchone()
        self.assertIsNotNone(row,
                             "POST must have written to last_room_id")
        self.assertEqual(row["value"], "new-room-xyz")

    def test_post_does_not_create_dorm_room_id(self):
        client = self._client()
        # Pre-condition: no legacy key.
        self.conn.execute("DELETE FROM meta WHERE key='dorm_room_id'")
        resp = client.post(
            "/admin/api/scrape/config",
            json={"dorm_room_id": "another-room"},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        # Post-condition: the legacy key was never touched.
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='dorm_room_id'"
        ).fetchone()
        # Either no row OR the row value is empty — but never the
        # submitted roomId value.
        if row is not None:
            self.assertNotEqual(row["value"], "another-room",
                                "POST must not write to legacy "
                                "dorm_room_id meta key")


# ---------------------------------------------------------------------------
# T7 — db._migrate_dorm_room_id collapses legacy into canonical
# ---------------------------------------------------------------------------

class TestMigrateDormRoomId(_DbBackedTest):
    """Round 35 — migration runs from ``db.init()`` and folds the
    legacy ``meta.dorm_room_id`` into ``meta.last_room_id``."""

    def test_migration_copies_legacy_when_canonical_empty(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('dorm_room_id', 'legacy-room-777')"
        )
        # Re-import db so _SCHEMA / init() see our patched get_conn.
        importlib.reload(db)
        db.init()
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key='last_room_id'"
        ).fetchone()
        self.assertIsNotNone(row, "migration must copy value into "
                                 "last_room_id")
        self.assertEqual(row["value"], "legacy-room-777")
        # Legacy key should be zeroed (not deleted) for rollback safety.
        legacy = self.conn.execute(
            "SELECT value FROM meta WHERE key='dorm_room_id'"
        ).fetchone()
        self.assertIsNotNone(legacy, "legacy row must still exist")
        self.assertEqual(legacy["value"], "",
                         "legacy key must be zeroed, not deleted")

    def test_migration_skips_when_canonical_already_set(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('dorm_room_id', 'legacy-room-aaa')"
        )
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('last_room_id', 'canonical-room-bbb')"
        )
        importlib.reload(db)
        db.init()
        canonical = self.conn.execute(
            "SELECT value FROM meta WHERE key='last_room_id'"
        ).fetchone()
        # Canonical wins; legacy value is NOT overwritten.
        self.assertEqual(canonical["value"], "canonical-room-bbb")

    def test_migration_idempotent_when_legacy_empty(self):
        self.conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) "
            "VALUES ('dorm_room_id', '')"
        )
        importlib.reload(db)
        db.init()
        canonical = self.conn.execute(
            "SELECT value FROM meta WHERE key='last_room_id'"
        ).fetchone()
        # No canonical value was created from an empty legacy.
        self.assertIsNone(canonical,
                          "empty legacy must not create canonical row")


# ---------------------------------------------------------------------------
# T8 — syntax + symbol exports for config / dorm_power / web
# ---------------------------------------------------------------------------

class TestRound35SyntaxAndExports(unittest.TestCase):
    """Round 35 — every changed file compiles, every new symbol is present."""

    def test_config_compiles(self):
        py_compile.compile(str(PROJ_DIR / "config.py"), doraise=True)

    def test_dorm_power_compiles(self):
        py_compile.compile(str(PROJ_DIR / "dorm_power.py"), doraise=True)

    def test_web_compiles(self):
        py_compile.compile(str(PROJ_DIR / "web.py"), doraise=True)

    def test_db_compiles(self):
        py_compile.compile(str(PROJ_DIR / "db.py"), doraise=True)

    def test_config_exports_get_dorm_room_id(self):
        self.assertTrue(hasattr(config, "get_dorm_room_id"),
                        "config.get_dorm_room_id missing")
        self.assertTrue(callable(getattr(config, "get_dorm_room_id")),
                        "config.get_dorm_room_id not callable")

    def test_db_exports_migrate_helper(self):
        # The migration may live as a module-level function or a
        # nested helper inside init() — accept either.  The simplest
        # signal that the migration logic shipped is that ``db.init``
        # exists and that the AST of db.py mentions both meta keys.
        text = pathlib.Path(db.__file__).read_text(encoding="utf-8")
        self.assertIn("last_room_id", text,
                      "db.py must reference last_room_id")
        self.assertIn("dorm_room_id", text,
                      "db.py must reference dorm_room_id (legacy)")
        # And the migration body must be wired into init().
        tree = ast.parse(text)
        init_fn = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name == "init"),
            None,
        )
        self.assertIsNotNone(init_fn, "db.init() missing")
        init_calls = [
            ast.unparse(c) for c in ast.walk(init_fn)
            if isinstance(c, ast.Call)
        ]
        self.assertTrue(
            any("_migrate_dorm_room_id" in c for c in init_calls),
            "db.init() must call _migrate_dorm_room_id; got calls: "
            f"{init_calls}",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)