"""Round 46 — fix dashboard historical-trend datetime-local T→space bug.

Background
==========
The dashboard's historical-trend panel (采集记录) renders a date-range
picker built on ``<input type="datetime-local">``.  When the user
clicks 查询, the frontend assembles an ``/api/data?start=...&end=...``
URL by padding the raw input with seconds::

    // datetime-local value is "YYYY-MM-DDTHH:MM".
    var startStr = s.length === 16 ? s + ':00' : s;  // → "2026-09-16T00:00:00"
    var endStr   = e.length === 16 ? e + ':59' : e;  // → "2026-09-16T23:59:00"

These strings reach ``db.query(start_dt=..., end_dt=...)`` which
builds::

    WHERE ts >= ? AND ts <= ?

Records are written by ``db.insert()`` with ``ts =
datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")`` — note the
**space** between date and time, no ``T``.

SQLite string compare is byte-by-byte:

  - ``'2026-09-16 10:48:55'``  (records.ts, space at position 10)
  - ``'2026-09-16T00:00:00'``  (start_str,    ``T``    at position 10)

The 11th character decides the comparison: ``' '`` (0x20) vs
``'T'`` (0x54) — ``T`` is bigger, so ``records.ts < start_str`` for
**every row on or after 2026-09-16**, and ``WHERE ts >=
'2026-09-16T00:00:00'`` returns zero rows even though the table holds
**764** historical records.  The dashboard showed "暂无采集记录" and
the operator thought the scraper was down.

Round 46 fix
============
Two layers, defence in depth:

  1. **Frontend** — ``web.py`` lines 2769-2770 now do
     ``.replace('T', ' ')`` after padding seconds.  Browser users
     get the fix the moment the new bundle is served.

  2. **Backend** — ``db.query()`` now normalises any ``T`` separator
     on ``start_dt`` / ``end_dt`` to a space before building SQL.
     Any non-browser caller (curl, ad-hoc scripts, future SDK) can
     pass either form.

Both changes are **purely additive** — callers passing the
already-correct space-separated form get identical behaviour to
before.  ``db.insert()`` is untouched: records keep the
``YYYY-MM-DD HH:MM:SS`` format that downstream code already expects.

Coverage (5 testcases):

  T1  ``test_query_with_T_format_returns_rows``
        — pass ``start_dt='2026-09-15T00:00:00'`` (with literal T).
        Backend must normalise and return matching records.  Pins
        the defence-in-depth branch.

  T2  ``test_query_with_space_format_returns_rows``
        — pass ``start_dt='2026-09-15 00:00:00'`` (space form).  Must
        still return matching records.  Pins that the space path is
        not broken by the new normalize step.

  T3  ``test_query_with_both_T_normalizes``
        — pass **both** sides with T separators.  Both bounds must
        normalise, the AND clause must hold, and the row count must
        equal the seed.

  T4  ``test_query_no_start_no_end_uses_hours``
        — pass neither side; ``db.query(hours=1)`` must fall through
        to the ``datetime('now', '-N hours')`` branch.  Pins that the
        new T-normalize block does not swallow the hours-only path.

  T5  ``test_query_with_date_only``
        — pass ``start_dt='2026-09-15'`` (no time at all).  String
        compare is still valid because ``'2026-09-15' < '2026-09-15
        00:00:00'`` and every record on that day is ``>= '2026-09-15
        00:00:00'`` only on the **next** day.  Document the limit by
        asserting zero matches (and that the empty-end bound is
        satisfied).

Plus two static guards (cheap, no DB):

  S1  ``test_round46_syntax_compiles``
        — ``py_compile`` round-trip on web.py and db.py.

  S2  ``test_frontend_replaces_T_with_space``
        — AST-level proof the ``web.py`` JS contains
        ``.replace('T', ' ')`` on the bound strings.

  S3  ``test_db_query_normalizes_T_separator``
        — AST-level proof the ``db.query`` source contains the
        ``start_dt.replace("T", " ")`` branch.

Uses only:

  * Shared in-memory SQLite (mirrors test_round41 / test_round42).
  * ``py_compile`` + ``ast.parse`` for syntax / wiring sanity checks.

Run:    python -m unittest test_round46 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the
operator runs it there.
"""
from __future__ import annotations

import ast
import os
import pathlib
import py_compile
import sqlite3
import sys
import unittest

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
        f"file:test_round46_db_{_make_test_db._COUNTER}"
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


def _seed_records(rows: list[tuple[str, str, float]]) -> None:
    """Insert canned records rows with explicit ``ts`` values.

    Each entry: ``(ts, read_time, remain)``.  ``ts`` MUST be in
    ``YYYY-MM-DD HH:MM:SS`` form (space-separated) — that is exactly
    what ``db.insert()`` writes in production and is the contract
    this whole round is built around.
    """
    with _db.get_conn() as conn:
        conn.executemany(
            "INSERT INTO records (ts, read_time, remain) "
            "VALUES (?, ?, ?)",
            rows,
        )


# A canonical 4-row seed covering the 2026-09-15 / 09-16 boundary so we
# can write tight lower/upper bound tests without flakiness.  All rows
# follow the production format ("YYYY-MM-DD HH:MM:SS").
SEED_RECORDS: list[tuple[str, str, float]] = [
    ("2026-09-15 06:00:00", "2026-09-15 06:00:00", 12.5),
    ("2026-09-15 18:30:00", "2026-09-15 18:30:00", 11.8),
    ("2026-09-16 00:00:00", "2026-09-16 00:00:00", 11.2),
    ("2026-09-16 10:48:55", "2026-09-16 10:48:55", 10.7),
]


# ---------------------------------------------------------------------------
# Runtime tests — pin the T→space normalize contract end-to-end
# ---------------------------------------------------------------------------

class TestRound46DatetimeNormalize(unittest.TestCase):
    """T1..T5 — round-trip ``db.query()`` against a hand-seeded
    ``records`` table.  Each test plants the same 4 rows so the
    expected row counts are deterministic.
    """

    def setUp(self) -> None:
        _make_test_db()
        _seed_records(SEED_RECORDS)

    def tearDown(self) -> None:
        _restore_db()

    # ----------------------------------------------------------------- T1
    def test_query_with_T_format_returns_rows(self) -> None:
        """T1: ``start_dt`` with a literal ``T`` separator MUST
        normalise and return the matching records.  Pre-Round-46
        this returned ``[]`` because ``'T' > ' '`` at byte index 10.
        """
        rows = _db.query(
            start_dt="2026-09-15T00:00:00",
            end_dt="2026-09-16T23:59:59",
        )
        self.assertEqual(
            len(rows), len(SEED_RECORDS),
            f"db.query() must normalise T→space; expected "
            f"{len(SEED_RECORDS)} rows in the seeded range, got "
            f"{len(rows)} — the 'T > space' regression is back.",
        )
        # And the rows must actually be the ones we planted (no
        # false-positive from a mis-formed range).
        timestamps = sorted(r["ts"] for r in rows)
        self.assertEqual(
            timestamps,
            sorted(ts for ts, _rt, _rem in SEED_RECORDS),
            "Returned rows must match the seeded timestamps exactly.",
        )

    # ----------------------------------------------------------------- T2
    def test_query_with_space_format_returns_rows(self) -> None:
        """T2: the canonical space-separated form MUST still work —
        no regression on the path the scraper already takes.
        """
        rows = _db.query(
            start_dt="2026-09-15 00:00:00",
            end_dt="2026-09-16 23:59:59",
        )
        self.assertEqual(
            len(rows), len(SEED_RECORDS),
            f"Space-separated range MUST keep working; expected "
            f"{len(SEED_RECORDS)} rows, got {len(rows)}.",
        )

    # ----------------------------------------------------------------- T3
    def test_query_with_both_T_normalizes(self) -> None:
        """T3: pass **both** bounds with ``T`` separators and a
        narrower window.  Confirms the AND clause survives when both
        sides were normalised independently.
        """
        rows = _db.query(
            start_dt="2026-09-16T00:00:00",
            end_dt="2026-09-16T23:59:59",
        )
        self.assertEqual(
            len(rows), 2,
            f"Narrowed range on 2026-09-16 must return exactly the "
            f"2 rows planted for that day; got {len(rows)}.",
        )
        # Sanity: the two returned rows must be the 2026-09-16 ones
        # (not a 2026-09-15 leak from the lower bound).
        for row in rows:
            self.assertTrue(
                row["ts"].startswith("2026-09-16"),
                f"Lower-bound normalise must exclude 2026-09-15 "
                f"rows; leaked ts={row['ts']!r}.",
            )

    # ----------------------------------------------------------------- T4
    def test_query_no_start_no_end_uses_hours(self) -> None:
        """T4: when neither side is supplied, ``db.query()`` MUST
        fall through to the ``hours`` branch (``datetime('now',
        '-N hours')``) and not call the new T-normalize block.  The
        normalize branch only runs when ``start_dt is not None or
        end_dt is not None``.

        We can't easily test the *exact* SQL bound, but we CAN test
        the contract: with no bounds and ``hours=1`` the seeded rows
        (which are days old) MUST NOT be returned — that proves we
        didn't accidentally widen the time-windowed path.
        """
        rows = _db.query(hours=1)
        self.assertEqual(
            rows, [],
            "hours=1 must return only rows from the last 1h; the "
            "seeded records are days old so the windowed path must "
            f"exclude them.  Got {len(rows)} rows — the new "
            "normalize branch must not have leaked into this path.",
        )

    # ----------------------------------------------------------------- T5
    def test_query_with_date_only(self) -> None:
        """T5: a bare ``'YYYY-MM-DD'`` (no time at all) is still
        a valid SQLite string bound.  Because every record on
        2026-09-15 starts with ``'2026-09-15 '`` (a space after
        the date, ASCII 0x20) and ``' ' > ''`` lexicographically,
        the lower bound ``'2026-09-15'`` is **strictly less** than
        any 2026-09-15 record — so the day IS included.  This pins
        that we don't accidentally require a T *and* a time, and
        also documents the bare-date form for future callers.
        """
        rows = _db.query(
            start_dt="2026-09-15",
            end_dt="2026-09-15",
        )
        self.assertEqual(
            len(rows), 2,
            f"Bare-date range on 2026-09-15 must include both "
            f"records planted that day; got {len(rows)}.",
        )
        for row in rows:
            self.assertTrue(
                row["ts"].startswith("2026-09-15"),
                f"Bare-date lower bound must NOT leak rows from "
                f"other days; leaked ts={row['ts']!r}.",
            )


# ---------------------------------------------------------------------------
# Syntax / wiring sanity (cheap static-only checks)
# ---------------------------------------------------------------------------

class TestRound46StaticGuards(unittest.TestCase):
    """AST-level guards — prove the round-46 wiring is in place even if
    a runtime test gets accidentally skipped due to environment quirks.
    No mock injection, no DB — just parse + read.
    """

    WEB_PY = PROJ_DIR / "web.py"
    DB_PY = PROJ_DIR / "db.py"

    def test_round46_syntax_compiles(self) -> None:
        """``py_compile`` round-trip on both edited modules — catches
        typos at the bytecode level."""
        py_compile.compile(str(self.WEB_PY), doraise=True)
        py_compile.compile(str(self.DB_PY), doraise=True)

    def test_frontend_replaces_T_with_space(self) -> None:
        """The ``web.py`` JavaScript that builds the ``/api/data``
        URL must contain ``.replace('T', ' ')`` on both bound
        strings.  Without this token the dashboard still sends a
        T-separated range and the round-46 frontend fix is dead
        code.
        """
        src = self.WEB_PY.read_text(encoding="utf-8")
        # The frontend is plain JavaScript inside a triple-quoted
        # Python string — we look for the literal token rather than
        # walking the AST.  The pattern is unique enough that a
        # single match is enough to pin the change.
        self.assertIn(
            ".replace('T', ' ')",
            src,
            "Round 46 frontend fix missing — web.py must call "
            ".replace('T', ' ') on start_str / end_str so "
            "datetime-local values align with records.ts format.",
        )
        # And it has to be applied to BOTH bounds — one side alone
        # is still a partial fix.
        occurrences = src.count(".replace('T', ' ')")
        self.assertGreaterEqual(
            occurrences, 2,
            f"Round 46 frontend fix must replace T on BOTH bounds; "
            f"expected >= 2 occurrences, found {occurrences}.",
        )

    def test_db_query_normalizes_T_separator(self) -> None:
        """``db.query()`` MUST contain a ``start_dt.replace("T", " ")``
        branch (and the matching ``end_dt`` one) so any non-browser
        caller still works.  This pins the defence-in-depth layer.
        """
        tree = ast.parse(self.DB_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "query"):
                continue
            src = ast.unparse(node)
            self.assertIn(
                'start_dt.replace("T", " ")',
                src,
                "Round 46 backend defense-in-depth missing — "
                "db.query() must normalize 'T'→' ' on start_dt.",
            )
            self.assertIn(
                'end_dt.replace("T", " ")',
                src,
                "Round 46 backend defense-in-depth missing — "
                "db.query() must normalize 'T'→' ' on end_dt.",
            )
            return
        self.fail("query function definition missing in db.py")


if __name__ == "__main__":
    unittest.main(verbosity=2)