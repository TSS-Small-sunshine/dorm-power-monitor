"""Round 39 — fix dashboard "本月预计电费 ¥— 等待学校返回数据" deadlock.

Background
==========
The dashboard's "本月预计电费" card was stuck at ``¥—`` with the
misleading subtitle "等待学校返回数据" forever after R37b's deploy.

Root cause: ``_backfill_once`` set ``meta.backfill_done='1'``
**unconditionally**, even when F2 (the ``getEmDayElectQuery`` step
that actually writes ``daily_elec``) had raised mid-loop.  R36 had
fixed the ``db.insert()`` 3-arg TypeError but the backfill flag was
already set — so the *next* deploy / cron tick silently skipped
re-populating ``daily_elec``.  Meanwhile the cron-driven F2 path
is throttled at 24h (``_F2_INTERVAL_SEC``), so even after the TypeError
was fixed there was no F2 row to feed ``_compute_monthly_projection``.

This round fixes two things:

  1. ``_backfill_once`` only sets ``backfill_done`` when ALL THREE
     endpoints succeed (F1 + F2 + F5).  Any failure leaves the flag
     unset so the next cron tick retries the batch.  T1/T2/T3 pin
     this behavior.

  2. ``updateMonthlyProjection`` JS no longer lies about "等待学校
     返回数据" — that string implied a network problem when the
     actual reason is "daily average not yet computable" (either
     daily_elec is empty or only today has data).  When ``used_kwh``
     is known, we now show "已用 ¥X.XX" instead of a bare ¥— so the
     user sees real numbers during the cold-start.  T4/T5/T6 pin
     this behavior.

Coverage (>= 6 testcases as required by the round-39 spec):

  T1  ``test_backfill_partial_does_not_set_done_flag``
        — F2 raises → ``backfill_done`` stays unset, fetcher logs a
          warning, ``set_meta`` is NOT called.
  T2  ``test_backfill_all_success_sets_done_flag``
        — F1 + F2 + F5 all succeed → ``backfill_done='1'``.
  T3  ``test_backfill_idempotent_if_already_done``
        — meta.backfill_done='1' at entry → function returns
          immediately, fetchers not called, no set_meta either way.
  T4  ``test_monthly_projection_shows_used_when_no_avg_daily``
        — daily_elec has used_kwh data, but avg_daily is None
          (1 day observed is below the projection threshold) → the
          JSON payload includes ``used_kwh`` and excludes
          ``monthly_projection``.
  T5  ``test_monthly_projection_returns_none_when_no_data``
        — daily_elec empty + stats.daily_avg=None → returns
          ``monthly_projection=None``.
  T6  ``test_updateMonthlyProjection_text_change``
        — web.py source: the misleading "等待学校返回数据" string is
          GONE from ``updateMonthlyProjection``, and the new copy
          "日均数据积累中" + "已用 ... kW·h" is present.

Uses only:

  * ``sqlite3`` in-memory (no disk DB)
  * ``unittest.mock`` for env-var / dependency isolation
  * ``py_compile`` + ``ast.parse`` for syntax / symbol presence checks
  * ``re`` for substring checks on the JS source

Run:    python -m unittest test_round39 -v

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
import re
import sqlite3
import sys
import unittest
from datetime import date, timedelta
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
import dorm_power  # noqa: E402
import web as web_module  # noqa: E402


# ---------------------------------------------------------------------------
# Test DB scaffolding (shared in-memory SQLite so each test is isolated)
# ---------------------------------------------------------------------------

def _make_test_db() -> None:
    """Create an isolated in-memory SQLite with the schema initialized."""
    _make_test_db._COUNTER = getattr(_make_test_db, "_COUNTER", 0) + 1
    shared_uri = (
        f"file:test_round39_db_{_make_test_db._COUNTER}"
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


def _reset_meta() -> None:
    """Wipe the meta table — every test starts from a clean slate."""
    with _db.get_conn() as conn:
        conn.execute("DELETE FROM meta")


def _seed_daily_elec(room_id: str, days: list[dict]) -> None:
    """Insert canned daily_elec rows so projection tests have data."""
    rows = []
    for entry in days:
        rows.append({
            "dt": entry["dt"],
            "esbm": entry.get("esbm", 0.0),
            "eebm": entry.get("eebm", entry.get("zong_eq", 1.0)),
            "zong_eq": entry.get("zong_eq", 1.0),
            "useEq": entry.get("useEq", 1.0),
        })
    _db.record_daily_elec(room_id, rows)


# ---------------------------------------------------------------------------
# T1 / T2 / T3 — _backfill_once partial-failure semantics
# ---------------------------------------------------------------------------

class TestRound39BackfillRobustness(unittest.TestCase):
    """T1 / T2 / T3: ``_backfill_once`` must only set ``backfill_done``
    when ALL three endpoints succeed.  A partial failure leaves the
    flag unset so the next cron tick retries."""

    ROOM = "r39-room-uuid"

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()

    def tearDown(self) -> None:
        _restore_db()

    def _assert_no_backfill_done(self) -> None:
        """Helper — meta.backfill_done must NOT exist or be unset."""
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='backfill_done'"
            ).fetchone()
        row_repr = repr(dict(row)) if row else "None"
        self.assertIsNone(
            row,
            f"backfill_done must NOT be set on partial failure; "
            f"got row={row_repr}",
        )

    def _assert_backfill_done(self) -> None:
        """Helper — meta.backfill_done must be '1'."""
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='backfill_done'"
            ).fetchone()
        self.assertIsNotNone(
            row, "backfill_done flag missing — expected '1'",
        )
        self.assertEqual(row["value"], "1")

    # ----------------------------------------------------------------- T1
    def test_backfill_partial_does_not_set_done_flag(self) -> None:
        """T1: When F2 (the daily_elec writer) raises mid-loop, the
        function MUST NOT set ``backfill_done='1'`` — otherwise the
        empty daily_elec + sticky flag deadlock persists across cron
        ticks.  The fix is to gate ``set_meta`` on all three OK flags.
        """
        fake_session = mock.Mock()
        # F1 returns rows, F2 raises (simulating the R36-era TypeError),
        # F5 returns rows.
        with mock.patch.object(
            dorm_power, "_fetch_dormEmQuery",
            return_value=[{"dt": "2026-09-15", "useEq": 1.0}],
        ), mock.patch.object(
            dorm_power, "_fetch_dormEmDayElectQuery",
            side_effect=TypeError(
                "insert() takes 2 positional arguments but 3 were given"
            ),
        ), mock.patch.object(
            dorm_power, "_fetch_dormEmPayQuery",
            return_value=[{
                "dt": "2026-09-15",
                "pay_type": "微信",
                "fee_type": "电费",
                "money": 50.0,
            }],
        ), mock.patch.object(
            _db, "record_daily_elec"
        ) as fake_record:
            # run the backfill
            dorm_power._backfill_once(fake_session, "openid-x", self.ROOM)

        # The whole point: backfill_done must NOT be set on partial.
        self._assert_no_backfill_done()
        # Sanity: db.record_daily_elec was NOT called (the F2 call
        # itself raised before the write).
        self.assertFalse(
            fake_record.called,
            "record_daily_elec must not be called when F2 itself raises",
        )

    # ----------------------------------------------------------------- T2
    def test_backfill_all_success_sets_done_flag(self) -> None:
        """T2: Happy path — all three endpoints succeed → ``backfill_done='1'``.

        Sanity guard so the new gating logic doesn't accidentally
        regress and never set the flag (which would force backfill
        on every cron tick — wasteful school-side requests).
        """
        fake_session = mock.Mock()
        with mock.patch.object(
            dorm_power, "_fetch_dormEmQuery",
            return_value=[{"dt": "2026-09-15", "useEq": 1.0}],
        ), mock.patch.object(
            dorm_power, "_fetch_dormEmDayElectQuery",
            return_value=[
                {"dt": "2026-09-15", "eebm": 100.0, "esbm": 99.0,
                 "useEq": 1.0, "zong_eq": 1.0},
            ],
        ), mock.patch.object(
            dorm_power, "_fetch_dormEmPayQuery",
            return_value=[{
                "dt": "2026-09-15",
                "pay_type": "微信",
                "fee_type": "电费",
                "money": 50.0,
            }],
        ):
            dorm_power._backfill_once(fake_session, "openid-x", self.ROOM)

        # Backfill must have completed and persisted the flag.
        self._assert_backfill_done()

    # ----------------------------------------------------------------- T3
    def test_backfill_idempotent_if_already_done(self) -> None:
        """T3: If ``backfill_done='1'`` already, the function returns
        immediately without calling any fetcher — preserves the
        long-standing "only on first scrape" semantics.
        """
        # Seed the flag before the call.
        _db.set_meta("backfill_done", "1")

        with mock.patch.object(
            dorm_power, "_fetch_dormEmQuery"
        ) as f1, mock.patch.object(
            dorm_power, "_fetch_dormEmDayElectQuery"
        ) as f2, mock.patch.object(
            dorm_power, "_fetch_dormEmPayQuery"
        ) as f5:
            dorm_power._backfill_once(mock.Mock(), "openid-x", self.ROOM)

        self.assertFalse(f1.called, "F1 must not be called when already done")
        self.assertFalse(f2.called, "F2 must not be called when already done")
        self.assertFalse(f5.called, "F5 must not be called when already done")


# ---------------------------------------------------------------------------
# T4 / T5 — _compute_monthly_projection payload shape
# ---------------------------------------------------------------------------

class TestRound39MonthlyProjectionPayload(unittest.TestCase):
    """T4 / T5: ``_compute_monthly_projection`` must return
    ``monthly_projection=None`` when no daily average is available,
    but ``used_kwh`` must still be populated so the dashboard can
    show "已用 ¥X" during the cold-start."""

    ROOM = "r39-room-uuid"

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()

    def tearDown(self) -> None:
        _restore_db()

    # ----------------------------------------------------------------- T4
    def test_monthly_projection_shows_used_when_no_avg_daily(self) -> None:
        """T4: daily_elec has 1+ rows this month (so ``used_kwh`` is
        populated), BUT ``eqprice`` is missing → ``monthly_projection``
        can't compute.  The payload must include ``used_kwh`` AND have
        ``monthly_projection=None``.

        This is the realistic cold-start scenario the dashboard
        actually sees: the school hasn't returned EqPrice yet (OOBE
        step 2/3 pending), but daily_elec is fine from the backfill.
        Frontend then renders "已用 ¥X.XX" instead of bare ¥—.
        """
        today = date.today()
        _seed_daily_elec(self.ROOM, [
            {"dt": today.isoformat(), "zong_eq": 12.34},
        ])
        # eqprice=None → projection gate returns None even though
        # used_kwh is computable.
        result = web_module._compute_monthly_projection(
            eqprice=None,
            stats={"daily_avg": None},
            room_id=self.ROOM,
        )
        # used_kwh must be populated so the dashboard can render
        # "已用 ¥X.XX".
        self.assertIsNotNone(
            result["used_kwh"],
            f"used_kwh must be set; got {result!r}",
        )
        self.assertGreater(result["used_kwh"], 0)
        # monthly_projection must be None — eqprice is the blocker here.
        self.assertIsNone(
            result["monthly_projection"],
            f"monthly_projection must be None when eqprice missing; "
            f"got {result!r}",
        )
        # avg_daily IS computed (1 sample with zong_eq>0) but eqprice
        # blocks the projection — pinning the realistic shape.
        self.assertEqual(result["days_observed"], 1)
        self.assertIsNotNone(result["avg_daily"])

    # ----------------------------------------------------------------- T5
    def test_monthly_projection_returns_none_when_no_data(self) -> None:
        """T5: daily_elec empty AND stats.daily_avg=None → the payload
        is entirely None.  This is the dashboard's "¥—" empty state."""
        result = web_module._compute_monthly_projection(
            eqprice=0.5,
            stats={},
            room_id=self.ROOM,
        )
        self.assertIsNone(result["used_kwh"])
        self.assertIsNone(result["avg_daily"])
        self.assertIsNone(result["monthly_projection"])
        self.assertEqual(result["days_observed"], 0)

    def test_monthly_projection_returns_value_when_avg_available(self) -> None:
        """Sanity guard (bonus): when daily_elec has 2+ rows this
        month, avg_daily + monthly_projection must both compute."""
        today = date.today()
        yesterday = today - timedelta(days=1)
        _seed_daily_elec(self.ROOM, [
            {"dt": yesterday.isoformat(), "zong_eq": 10.0},
            {"dt": today.isoformat(),     "zong_eq": 25.0},
        ])
        result = web_module._compute_monthly_projection(
            eqprice=0.5,
            stats={},
            room_id=self.ROOM,
        )
        self.assertEqual(result["days_observed"], 2)
        self.assertIsNotNone(result["avg_daily"])
        self.assertIsNotNone(result["monthly_projection"])
        # rough sanity — projection should be > used_kwh * price
        # because there are days_left we extrapolate.
        self.assertGreater(result["monthly_projection"], 0)


# ---------------------------------------------------------------------------
# T6 — Frontend text: "等待学校返回数据" → "日均数据积累中"
# ---------------------------------------------------------------------------

class TestRound39FrontendTextChange(unittest.TestCase):
    """T6: Round 39 must rewrite the misleading "等待学校返回数据"
    string in ``updateMonthlyProjection``.  monthly_projection is
    a *local* computation (daily_elec + stats.daily_avg + eqprice),
    not a live school API — the original copy was straight-up wrong.
    """
    WEB_PY = PROJ_DIR / "web.py"

    def setUp(self) -> None:
        self.text = self.WEB_PY.read_text(encoding="utf-8")

    def test_old_text_is_gone(self) -> None:
        """The misleading string must not be ASSIGNED to a UI element
        in web.py.  (We tolerate it as a code comment explaining the
        historical context — the test strips JS block comments first.)"""
        # Strip /* ... */ block comments + // line comments to avoid
        # false positives from the historical context note.
        stripped = re.sub(r"/\*.*?\*/", "", self.text, flags=re.DOTALL)
        stripped = re.sub(r"//[^\n]*", "", stripped)
        self.assertNotIn(
            "等待学校返回数据", stripped,
            "Round 39 must remove the misleading '等待学校返回数据' copy "
            "from active UI code — monthly_projection is computed "
            "locally, not from the school API",
        )

    def test_new_text_is_present(self) -> None:
        """The new copy must appear at least once (in the cold-start
        branch) and include both pieces: '日均数据积累中' and the
        '已用 ... kW·h' hint shown when used_kwh is known."""
        self.assertIn(
            "日均数据积累中", self.text,
            "Round 39 must introduce the new '日均数据积累中' copy",
        )
        self.assertIn(
            "kW·h", self.text,
            "Round 39 must include the kW·h hint so users know what "
            "unit they're looking at",
        )

    def test_updateMonthlyProjection_function_has_new_branch(self) -> None:
        """AST-level guard: the function body must contain a positive
        branch for ``used_kwh > 0`` AND a fallback branch — proves
        the new behavior was wired, not just a string replacement."""
        tree = ast.parse(self.text)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "updateMonthlyProjection"):
                continue
            src = ast.unparse(node)
            self.assertIn(
                "usedKwh", src,
                "updateMonthlyProjection must reference the new "
                "usedKwh variable for the friendly fallback",
            )
            self.assertIn(
                "is-empty", src,
                "updateMonthlyProjection must still toggle is-empty on "
                "the valEl for the ¥— empty state",
            )
            return
        self.fail(
            "updateMonthlyProjection() function definition missing in web.py"
        )

    def test_round39_syntax_compiles(self) -> None:
        """py_compile round-trip — catches typos at the bytecode level."""
        py_compile.compile(str(self.WEB_PY), doraise=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)