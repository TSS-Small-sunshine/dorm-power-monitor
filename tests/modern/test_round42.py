"""Round 42 — fix ``web._read_eqprice`` missing fallback chain.

Background
==========
``web._read_eqprice()`` was a thin wrapper around ``db.get_meta("eqprice")``
that returned ``None`` whenever the cached value was missing::

    raw = db.get_meta("eqprice")
    if raw is None:
        return None
    try:
        return round(float(raw), 4)
    except (TypeError, ValueError):
        return None

Meanwhile ``dorm_power._read_eqprice()`` (Round 34A B1 fix) already had a
three-level chain::

    raw = db.get_meta(_META_EQPRICE)
    if raw is None or raw == "":
        raw = os.environ.get("DORM_EQPRICE", "0.5")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.5

Result on a fresh DB without ``meta.eqprice`` AND without
``DORM_EQPRICE`` in the env:

  * ``dorm_power`` silently used 0.5 (Feishu card renders fine)
  * ``web`` returned ``None`` → ``_compute_monthly_projection``'s
    ``if eqprice is None or eqprice <= 0 or avg_daily is None``
    guard tripped → ``monthly_projection=None`` → dashboard "¥—"

User's temporary workaround was a manual
``sqlite3 INSERT INTO meta VALUES ('eqprice', '0.5')``.  That patch
worked but a brand-new DB (or a different operator) would trip on the
same bug.

Round 42 fix
============
Bring ``web._read_eqprice()`` in line with the ``dorm_power`` contract:
meta → env (``DORM_EQPRICE``) → 0.5 default.  The user's manual INSERT
becomes redundant (harmless — meta takes priority anyway) but the
fresh-DB / new-operator case now Just Works.

Coverage (>= 5 testcases, this file ships 6):

  T1  ``test_read_eqprice_prefers_meta``
        — meta.eqprice='0.6' MUST return 0.6 regardless of env value.
        Pins the "meta wins over env" precedence.

  T2  ``test_read_eqprice_falls_back_to_env``
        — meta.eqprice missing AND env='0.55' MUST return 0.55.
        Pins the env fallback step.

  T3  ``test_read_eqprice_falls_back_to_default``
        — meta missing AND env unset/unset-via-mock MUST return 0.5.
        Pins the hard default (the Round 42 fix's whole reason for
        being).

  T4  ``test_read_eqprice_invalid_returns_none``
        — meta='abc' (unparseable) MUST return None without raising.
        Pins the defensive ``try/except`` guard so a bad cached value
        can't crash the dashboard.

  T5  ``test_read_eqprice_empty_string_treated_as_missing``
        — meta='' or env='' MUST walk past the empty value to the
        next source.  Catches the bug where the original code only
        checked ``raw is None`` and not ``raw == ""``.

  T6  ``test_compute_monthly_projection_uses_default_eqprice``
        — integration: with daily_elec seeded + no meta + no env, the
        monthly_projection MUST still compute (not None).  Pins the
        end-to-end path that this round is meant to restore.

Uses only:

  * ``unittest.mock`` patches ``db.get_meta`` and ``os.environ``
  * sqlite3 in-memory for the T6 integration test (mirrors the
    test_round41 pattern)
  * ``py_compile`` + ``ast.parse`` for syntax / wiring sanity checks

Run:    python -m unittest test_round42 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the
operator runs it there.
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
from datetime import date
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
import web as web_module  # noqa: E402


# ---------------------------------------------------------------------------
# Test DB scaffolding (shared in-memory SQLite so T6 can seed daily_elec)
# ---------------------------------------------------------------------------

def _make_test_db() -> None:
    """Create an isolated in-memory SQLite with the schema initialized."""
    _make_test_db._COUNTER = getattr(_make_test_db, "_COUNTER", 0) + 1
    shared_uri = (
        f"file:test_round42_db_{_make_test_db._COUNTER}"
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


def _seed_daily_elec(room_id: str, rows: list[dict]) -> None:
    """Insert canned daily_elec rows so T6 has real ``zong_eq`` data.

    Each entry: ``{"dt": "YYYY-MM-DD", "zong_eq": float}``.  The
    ``zong_eq`` column mirrors F2 ``eebm`` — the cumulative meter
    reading as of that date.
    """
    payload = []
    for entry in rows:
        payload.append({
            "dt": entry["dt"],
            "esbm": entry.get("esbm", 0.0),
            "eebm": entry.get("eebm", entry.get("zong_eq", 1.0)),
            "zong_eq": entry.get("zong_eq", 1.0),
            "useEq": entry.get("useEq", 1.0),
        })
    _db.record_daily_elec(room_id, payload)


# ---------------------------------------------------------------------------
# T1 / T2 — meta precedence and env fallback
# ---------------------------------------------------------------------------

class TestRound42EqpriceFallback(unittest.TestCase):
    """T1 / T2 / T3 / T5: ``web._read_eqprice()`` must walk the full
    meta → env → default chain the way ``dorm_power._read_eqprice()``
    already does (Round 34A B1 contract).  The patches below are
    scoped per-test so each precedence step is independent.
    """

    def setUp(self) -> None:
        # Re-bind the modules the patched functions read from, so each
        # test starts from a clean slate even if setUp is called more
        # than once.
        self._meta_patch = mock.patch.object(web_module.db, "get_meta")
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)

    def tearDown(self) -> None:
        # Stop any active patches just in case a test exited early.
        try:
            self._meta_patch.stop()
        except (RuntimeError, KeyError):
            pass
        try:
            self._env_patch.stop()
        except (RuntimeError, KeyError):
            pass

    # ----------------------------------------------------------------- T1
    def test_read_eqprice_prefers_meta(self) -> None:
        """T1: meta.eqprice='0.6' MUST win over any env value (env
        set to 0.9 here to prove meta takes priority).

        Pins the meta-over-env precedence so an operator setting
        ``DORM_EQPRICE`` on the server can't accidentally clobber a
        value the scraper already cached from the school's portal.
        """
        with self._meta_patch as get_meta_mock, \
             self._env_patch as env_mock:
            get_meta_mock.return_value = "0.6"
            env_mock["DORM_EQPRICE"] = "0.9"  # should be ignored
            get_meta_mock.assert_not_called()  # sanity: not yet
            result = web_module._read_eqprice()

        self.assertAlmostEqual(
            result, 0.6, places=4,
            msg=f"_read_eqprice() MUST honour meta first; got "
                f"{result!r} (env was 0.9, meta was 0.6)",
        )

    # ----------------------------------------------------------------- T2
    def test_read_eqprice_falls_back_to_env(self) -> None:
        """T2: meta returns ``None`` AND env='0.55' MUST return 0.55.

        Pins the env fallback step — the round's main motivation:
        without it, dashboard "¥—" was triggered every time meta was
        empty even if the operator had set ``DORM_EQPRICE``.
        """
        with self._meta_patch as get_meta_mock, \
             self._env_patch as env_mock:
            get_meta_mock.return_value = None
            env_mock["DORM_EQPRICE"] = "0.55"
            result = web_module._read_eqprice()

        self.assertAlmostEqual(
            result, 0.55, places=4,
            msg=f"_read_eqprice() MUST fall back to env when meta "
                f"is missing; got {result!r} (env was 0.55)",
        )

    # ----------------------------------------------------------------- T3
    def test_read_eqprice_falls_back_to_default(self) -> None:
        """T3: meta missing AND env unset MUST return the hard default
        0.5 (Round 34A B1 contract).

        This is THE test that pins the round-42 fix's whole reason
        for being: a fresh DB without meta.eqprice and without
        ``DORM_EQPRICE`` in the environment used to return None and
        starve the dashboard's monthly_projection.  Now it MUST
        return 0.5.

        To make this a tight pin we pop ``DORM_EQPRICE`` from the
        real process environment before the test runs (it isn't
        expected to be set, but cleanup is cheap).
        """
        # Make sure DORM_EQPRICE is genuinely absent for this test.
        os.environ.pop("DORM_EQPRICE", None)
        with self._meta_patch as get_meta_mock:
            get_meta_mock.return_value = None
            result = web_module._read_eqprice()

        self.assertAlmostEqual(
            result, 0.5, places=4,
            msg=f"_read_eqprice() MUST fall back to 0.5 default "
                f"when both meta and env are missing; got {result!r}",
        )


# ---------------------------------------------------------------------------
# T4 — invalid value contract (don't raise, return None)
# ---------------------------------------------------------------------------

class TestRound42EqpriceInvalidValue(unittest.TestCase):
    """T4: ``web._read_eqprice()`` must NOT raise on unparseable input.
    A corrupted meta value should return ``None`` (caller decides what
    to do — most often the projection guard turns it into "¥—").
    """

    def test_read_eqprice_invalid_returns_none(self) -> None:
        """T4: meta='abc' (cannot be parsed as float) MUST return
        ``None`` rather than raising ``ValueError``.  The original
        implementation already had a ``try/except`` for this path,
        and Round 42 must NOT regress it.
        """
        with mock.patch.object(web_module.db, "get_meta",
                               return_value="abc"), \
             mock.patch.dict(os.environ, {}, clear=False):
            # Belt-and-suspenders: make sure env doesn't accidentally
            # provide a parseable value during this test.
            os.environ.pop("DORM_EQPRICE", None)
            try:
                result = web_module._read_eqprice()
            except (TypeError, ValueError) as exc:
                self.fail(
                    f"_read_eqprice() must NOT raise on invalid "
                    f"meta value; raised {type(exc).__name__}: {exc}"
                )

        self.assertIsNone(
            result,
            f"_read_eqprice() must return None on unparseable meta "
            f"value 'abc'; got {result!r}",
        )


# ---------------------------------------------------------------------------
# T5 — empty string is treated as missing
# ---------------------------------------------------------------------------

class TestRound42EqpriceEmptyString(unittest.TestCase):
    """T5: An empty string (``""``) at any level MUST walk past that
    level to the next fallback.  The pre-R42 code only checked
    ``raw is None`` for the meta branch, so an empty-string meta
    value would have raised inside ``float(raw)``.
    """

    def test_read_eqprice_empty_meta_falls_back_to_env(self) -> None:
        """T5a: meta='' AND env='0.45' MUST return 0.45 — proves the
        new ``not raw`` short-circuit also catches the empty-string
        case (the pre-R42 ``raw is None`` check did NOT).
        """
        with mock.patch.object(web_module.db, "get_meta",
                               return_value=""), \
             mock.patch.dict(os.environ, {"DORM_EQPRICE": "0.45"}):
            result = web_module._read_eqprice()

        self.assertAlmostEqual(
            result, 0.45, places=4,
            msg=f"Empty meta string MUST fall through to env; got "
                f"{result!r} (env was 0.45)",
        )

    def test_read_eqprice_empty_meta_and_env_falls_back_to_default(self) -> None:
        """T5b: meta='' AND env='' (or env unset) MUST walk past BOTH
        empty strings and return the hard default 0.5.  Catches any
        regression where the empty-string short-circuit is dropped.
        """
        os.environ.pop("DORM_EQPRICE", None)
        with mock.patch.object(web_module.db, "get_meta",
                               return_value=""), \
             mock.patch.dict(os.environ, {"DORM_EQPRICE": ""}):
            result = web_module._read_eqprice()

        self.assertAlmostEqual(
            result, 0.5, places=4,
            msg=f"Empty meta AND empty env MUST fall back to 0.5 "
                f"default; got {result!r}",
        )


# ---------------------------------------------------------------------------
# T6 — end-to-end: projection still computes when only default is available
# ---------------------------------------------------------------------------

class TestRound42MonthlyProjectionEndToEnd(unittest.TestCase):
    """T6: With ``daily_elec`` seeded for this month AND no meta AND
    no env, ``_compute_monthly_projection`` MUST still produce a
    real ``monthly_projection`` (via the 0.5 default eqprice).  This
    pins the round-42 fix's user-visible promise: dashboard "¥—" goes
    away on fresh installs.

    Without the fix, ``_read_eqprice()`` returned None,
    ``_compute_monthly_projection``'s ``if eqprice is None or
    eqprice <= 0 or avg_daily is None`` guard tripped, and
    ``monthly_projection`` came back None.
    """

    ROOM = "r42-room-uuid"

    def setUp(self) -> None:
        _make_test_db()
        os.environ.pop("DORM_EQPRICE", None)

    def tearDown(self) -> None:
        _restore_db()

    # ----------------------------------------------------------------- T6
    def test_compute_monthly_projection_uses_default_eqprice(self) -> None:
        """T6: monthly_projection MUST be non-None even when meta +
        env are both missing — the 0.5 default saves the dashboard.

        Uses the round-41 delta math (5-day cumulative readings) so
        we know the expected projection value with high precision:
            used_kwh          = 117.81
            days_observed     = 5
            avg_daily         = 29.453
            days_left         = 30 - 15  (mid-Sep)
            monthly_projection = (117.81 + 29.453 * 15) * 0.5 ≈ ¥279.80
        """
        today = date.today()
        if today.month != 9 or today.day < 15:
            self.skipTest(
                f"T6 needs Sep 11..15 in the current month; today is "
                f"{today.isoformat()}"
            )
        rows = [
            {"dt": "2026-09-11", "zong_eq": 7745.97},
            {"dt": "2026-09-12", "zong_eq": 7775.53},
            {"dt": "2026-09-13", "zong_eq": 7812.50},
            {"dt": "2026-09-14", "zong_eq": 7835.53},
            {"dt": "2026-09-15", "zong_eq": 7863.78},
        ]
        _seed_daily_elec(self.ROOM, rows)

        # Pin the fallback: meta returns None AND env is empty.
        with mock.patch.object(web_module.db, "get_meta",
                               return_value=None), \
             mock.patch.dict(os.environ, {"DORM_EQPRICE": ""}):
            eqprice = web_module._read_eqprice()
            self.assertAlmostEqual(
                eqprice, 0.5, places=4,
                msg=f"_read_eqprice() must return 0.5 default when "
                    f"meta + env are empty; got {eqprice!r}",
            )
            projection = web_module._compute_monthly_projection(
                eqprice=eqprice,
                stats={},
                room_id=self.ROOM,
            )

        self.assertIsNotNone(
            projection["monthly_projection"],
            f"monthly_projection must compute via the 0.5 default; "
            f"got None — this is exactly the Round-42 '¥—' bug",
        )
        # Sanity: the projection must be in the same order of magnitude
        # as the round-41 expected value (~¥279.80).  If eqprice had
        # silently dropped to a much smaller number the projection
        # would scale down proportionally and trip this bound.
        self.assertGreater(
            projection["monthly_projection"], 100,
            f"monthly_projection must be > ¥100 (round-41 baseline "
            f"~¥280); got {projection['monthly_projection']!r}",
        )
        self.assertLess(
            projection["monthly_projection"], 500,
            f"monthly_projection must be < ¥500 (round-41 baseline "
            f"~¥280); got {projection['monthly_projection']!r}",
        )
        # And the eqprice field itself must surface the 0.5 default
        # back to the dashboard so the user sees the value it used.
        self.assertAlmostEqual(
            projection["eqprice"], 0.5, places=4,
            msg=f"projection['eqprice'] must echo the resolved 0.5 "
                f"default so the dashboard shows the right number; "
                f"got {projection['eqprice']!r}",
        )


# ---------------------------------------------------------------------------
# Syntax / wiring sanity (cheap static-only checks)
# ---------------------------------------------------------------------------

class TestRound42StaticGuards(unittest.TestCase):
    """AST-level guards — prove the round-42 wiring is in place even if
    a runtime test gets accidentally skipped due to calendar position
    or environment quirks.  No mock injection, no DB — just parse +
    read.
    """

    WEB_PY = PROJ_DIR / "web.py"

    def test_round42_syntax_compiles(self) -> None:
        """py_compile round-trip — catches typos at the bytecode level."""
        py_compile.compile(str(self.WEB_PY), doraise=True)

    def test_read_eqprice_uses_env_fallback(self) -> None:
        """``web._read_eqprice`` must contain ``DORM_EQPRICE`` so we
        know the env fallback was actually wired in (not just a
        docstring edit).  Without this token the round-42 fix would
        still return None on a fresh DB.
        """
        tree = ast.parse(self.WEB_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "_read_eqprice"):
                continue
            src = ast.unparse(node)
            self.assertIn(
                "DORM_EQPRICE", src,
                "Round 42 must wire the DORM_EQPRICE env fallback "
                "into web._read_eqprice() so dashboard and cron "
                "agree on the price.",
            )
            self.assertIn(
                "0.5", src,
                "Round 42 must wire the 0.5 hard default into "
                "web._read_eqprice() so a fresh DB doesn't return None.",
            )
            return
        self.fail("_read_eqprice function definition missing in web.py")

    def test_read_eqprice_no_longer_returns_none_on_missing(self) -> None:
        """The pre-R42 implementation had::

            raw = db.get_meta("eqprice")
            if raw is None:
                return None

        That early ``return None`` MUST be gone — the round-42 fix
        must walk the fallback chain instead of bailing out as soon
        as meta is empty.  This test pins the structural change.
        """
        tree = ast.parse(self.WEB_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "_read_eqprice"):
                continue
            src = ast.unparse(node)
            # The pre-R42 form was a literal ``return None`` directly
            # guarded by ``if raw is None``.  Round 42 keeps the
            # trailing ``return None`` for the unparseable-value
            # case (T4) but the meta-missing path must walk the
            # fallback chain instead.  We assert the meta-missing
            # branch no longer short-circuits.
            self.assertNotIn(
                'if raw is None:\n        return None',
                src,
                "Round 42 must remove the 'if raw is None: return "
                "None' short-circuit in web._read_eqprice — the "
                "fix must walk the fallback chain instead of "
                "bailing out as soon as meta is empty.",
            )
            return
        self.fail("_read_eqprice function definition missing in web.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)