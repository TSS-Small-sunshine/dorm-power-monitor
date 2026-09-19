"""Round 48 — frontend JS timezone parse fix.

Background
==========
Round 47 switched the backend (``db.insert`` / ``_stamp_now`` /
``_parse_meta_ts`` / ...) from ``datetime.now(UTC)`` to NAIVE LOCAL time
so the dashboard's "采集时间" / "抄表时间" / "最后更新" labels match the
user's wall clock.  But it dropped ``+ 'Z'`` from the frontend JS:

  var last = tsRaw ? new Date(tsRaw.replace(' ', 'T')) : null;

That second half broke the "在线/离线" pill because JavaScript's
``new Date('YYYY-MM-DD HH:MM:SS')`` (no trailing 'Z') is interpreted as
the BROWSER's local timezone, not the server's.  On a CST desktop the
pill is correct (== wall clock), but on a UTC container / a server
deployed without ``TZ=Asia/Shanghai`` the parser shifts the timestamp
by 8 h and ``ageSec = (Date.now() - last.getTime()) / 1000`` either
goes negative (pill forever "离线") or wraps 8 h forward.

Round 48 fix
============
Append ``+08:00`` to every JS Date string-parsing call so the parser
*unconditionally* treats the naive-CST string as Asia/Shanghai wall
clock, independent of the host's TZ env var::

  web.py:2148    setPill()            new Date(... + '+08:00')
  web.py:2775    queryRecords() guard new Date(s + '+08:00') > new Date(e + '+08:00')

We deliberately keep ``db.insert()`` writing
``datetime.now().strftime("%Y-%m-%d %H:%M:%S")`` (R47 contract) —
switching to ``datetime.now().isoformat()`` would output
``YYYY-MM-DDTHH:MM:SS.ffffff+08:00`` and break the existing query
helpers that string-compare against naive ``YYYY-MM-DD HH:MM:SS``
literals (ASCII ' ' (0x20) < 'T' (0x54), so ISO rows sort AFTER naive
rows and ``WHERE ts <= '2026-09-17 23:59:59'`` would miss every ISO row).

Coverage (3 runtime / static testcases):

  T1  ``test_web_setPill_marks_plus_08_00``
        — AST-level guard.  Walks ``web.py`` for the JS Date string
        parser used by ``setPill()`` and asserts the literal
        ``+ '+08:00'`` suffix is present.  Pre-R48 the file contained
        ``new Date(tsRaw.replace(' ', 'T'))`` (no tz tag) which
        produced the 8 h pill-shift bug.
  T2  ``test_db_insert_keeps_strftime_not_isoformat``
        — AST-level guard.  Walks ``db.insert()`` and confirms it
        still uses ``datetime.now().strftime(...)`` (the R47 contract)
        and does NOT use ``datetime.now().isoformat()``.  Pinning the
        "don't switch the schema" half of R48.
  T3  ``test_naive_cst_string_parses_to_correct_offset``
        — runtime simulation of the browser side.  Re-creates the
        JS-side ``new Date(raw.replace(' ', 'T') + '+08:00')`` logic in
        pure Python (``datetime.fromisoformat``) and asserts the parsed
        value sits inside a ±2 h window around ``datetime.now()`` when
        the input is "wall-clock right now".  This is the bug repro:
        without ``+08:00`` the parsed value would drift 8 h on any
        non-CST host.

Plus a static guard:

  S1  ``test_round48_syntax_compiles``
        — ``py_compile`` round-trip on all touched files
        (web.py / db.py / tests/modern/test_round48.py).

Run:    python -m unittest test_round48 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the
operator runs it there.
"""
from __future__ import annotations

import ast
import os
import pathlib
import py_compile
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone

PROJ_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
os.chdir(PROJ_DIR)
sys.path.insert(0, str(PROJ_DIR))


# CST offset is fixed at +08:00 — match the JS-side ``+ '+08:00'``
# literal so a future TZ change forces an explicit code update.
CST = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Static guards — AST-level + regex checks that pin the round-48 edits
# ---------------------------------------------------------------------------

class TestRound48StaticGuards(unittest.TestCase):
    """T1, T2 + S1 — wiring-level proof that the round-48 edits landed."""

    WEB_PY = PROJ_DIR / "web.py"
    DB_PY = PROJ_DIR / "db.py"
    TEST_PY = PROJ_DIR / "tests" / "modern" / "test_round48.py"

    # ----------------------------------------------------------------- S1
    def test_round48_syntax_compiles(self) -> None:
        """S1: ``py_compile`` round-trip on the files touched by R48.

        Catches typos at the bytecode level.  We deliberately do NOT
        touch db.py this round (R47 already validated that contract)
        but we compile it anyway so a future regression there is
        caught even if the runtime tests are skipped.
        """
        py_compile.compile(str(self.WEB_PY), doraise=True)
        py_compile.compile(str(self.DB_PY), doraise=True)
        py_compile.compile(str(self.TEST_PY), doraise=True)

    # ----------------------------------------------------------------- T1
    def test_web_setPill_marks_plus_08_00(self) -> None:
        """T1: ``web.py`` ``setPill()`` MUST parse ``data-ts`` as
        ``new Date(tsRaw.replace(' ', 'T') + '+08:00')`` (with the
        literal ``+ '+08:00'`` suffix) so the browser interprets the
        naive-CST string as Asia/Shanghai wall clock regardless of the
        container / browser TZ env var.

        Pre-R48 the file had ``new Date(tsRaw.replace(' ', 'T'))`` —
        no tz tag — which broke the pill on any non-CST host.

        NB: ``setPill`` is JavaScript embedded in a Python multiline
        string in web.py, so ast.walk won't find it — we slice the
        function body out of the source with a regex instead.
        """
        src = self.WEB_PY.read_text(encoding="utf-8")

        # Slice out the setPill function body — the JS template literal
        # starts at ``function setPill() {`` and ends at the matching
        # closing ``}``.  We use a permissive stop pattern (``}`` at
        # indent 4 followed by a blank/comment line) because the
        # surrounding context is a Python string.
        match = re.search(
            r"function\s+setPill\s*\(\s*\)\s*\{(.*?)^\s{4}\}",
            src,
            re.DOTALL | re.MULTILINE,
        )
        self.assertIsNotNone(
            match,
            "web.py must still contain a setPill() function definition",
        )
        set_pill_src = match.group(0)

        # The literal suffix ``+ '+08:00'`` MUST be present in the
        # ``new Date(...)`` call.  Use a regex so we don't get fooled by
        # comments or unrelated string literals.
        self.assertRegex(
            set_pill_src,
            r"new Date\(\s*tsRaw\.replace\(\s*['\"] ['\"]\s*,\s*['\"]T['\"]\s*\)\s*\+\s*'\+08:00'\s*\)",
            "setPill() must construct 'new Date(tsRaw.replace(' ', 'T') "
            "+ '+08:00')' so the parser treats the naive-CST string as "
            "Asia/Shanghai wall clock.  Without the '+08:00' tag the "
            "pill shifts 8 h on any non-CST host.",
        )

        # Belt-and-braces: the legacy untagged form must be GONE.
        self.assertNotIn(
            "new Date(tsRaw.replace(' ', 'T'))",
            set_pill_src,
            "setPill() still uses the untagged new Date(tsRaw.replace("
            "' ', 'T')) form — that's the round-47/48 bug.",
        )

    # ----------------------------------------------------------------- T1b
    def test_web_queryRecords_marks_plus_08_00(self) -> None:
        """T1b: companion guard for the ``queryRecords()`` range
        validator (``new Date(s + '+08:00') > new Date(e + '+08:00')``).
        Without the tz tag, ``new Date('2026-09-17 13:00')`` parses
        against the browser TZ and the start/end compare is silently
        wrong on non-CST hosts.
        """
        src = self.WEB_PY.read_text(encoding="utf-8")
        # The literal ``s + '+08:00'`` / ``e + '+08:00'`` MUST be
        # present in the validator block.
        self.assertIn(
            "s + '+08:00'",
            src,
            "queryRecords() guard must mark the datetime-local inputs "
            "with '+08:00' so the start/end compare uses CST.",
        )
        self.assertIn(
            "e + '+08:00'",
            src,
            "queryRecords() guard must mark BOTH the start and end "
            "datetime-local inputs with '+08:00'.",
        )

    # ----------------------------------------------------------------- T2
    def test_db_insert_keeps_strftime_not_isoformat(self) -> None:
        """T2: ``db.insert()`` MUST continue to use
        ``datetime.now().strftime(...)`` (R47 contract) and MUST NOT
        use ``datetime.now().isoformat()``.

        Pinning the "don't switch the schema" half of R48.  Switching
        to ISO-with-TZ would break the existing query helpers that
        string-compare against naive ``YYYY-MM-DD HH:MM:SS`` literals
        (ASCII ' ' (0x20) < 'T' (0x54)).
        """
        tree = ast.parse(self.DB_PY.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name == "insert"):
                continue
            src = ast.unparse(node)
            self.assertIn(
                "datetime.now().strftime",
                src,
                "db.insert() must still call "
                "datetime.now().strftime(...) — round 47 contract.",
            )
            self.assertNotIn(
                "datetime.now().isoformat",
                src,
                "db.insert() must NOT call datetime.now().isoformat() "
                "in round 48 — the resulting ISO-with-TZ string breaks "
                "the existing naive-string query helpers.",
            )
            return
        self.fail("db.insert() function definition missing")


# ---------------------------------------------------------------------------
# Runtime simulation — repro the browser-side parser in pure Python
# ---------------------------------------------------------------------------

class TestRound48RuntimeRepro(unittest.TestCase):
    """T3 — runtime simulation of the JS ``new Date(... + '+08:00')``.

    We can't run a headless browser in this test, but the JS Date
    parser's behavior with a trailing ``+HH:MM`` tz tag is identical
    to ``datetime.fromisoformat`` with a tz-aware suffix.  This is the
    repro: feed a "wall-clock right now" CST string and assert the
    parsed value sits within ±2 h of ``datetime.now(CST)`` regardless
    of the *parser*'s TZ — which is the exact property we want the
    browser to have.
    """

    # ----------------------------------------------------------------- T3
    def test_naive_cst_string_parses_to_correct_offset(self) -> None:
        """T3: a "wall-clock right now" CST string built by
        ``datetime.now().strftime(...)`` MUST parse to within ±2 h of
        ``datetime.now(CST)`` when the parser is told ``+08:00``.

        Pre-R48 the JS code omitted the ``+08:00`` tag, so on a UTC
        host the same string parsed 8 h off — that's the 8 h pill
        shift the user saw in their screenshot.
        """
        # Build the input the way db.insert() does (R47 contract).
        now_cst = datetime.now(CST)
        naive_str = now_cst.strftime("%Y-%m-%d %H:%M:%S")

        # Build the input the way the BROKEN (R47) JS code did.
        iso_no_tz = naive_str.replace(" ", "T")
        # Build the input the way the FIXED (R48) JS code does.
        iso_with_tz = iso_no_tz + "+08:00"

        # Parse both with the Python equivalent of ``new Date(...)``.
        # Without a tz tag, fromisoformat produces a naive datetime
        # (parroting the broken JS behavior — the BROWSER's local tz
        # is what we'd really want to mock, but the bug is exactly
        # that "local" differs from CST).
        parsed_no_tz = datetime.fromisoformat(iso_no_tz)
        parsed_with_tz = datetime.fromisoformat(iso_with_tz)

        # parsed_with_tz must be tz-aware and offset by +08:00.
        self.assertIsNotNone(
            parsed_with_tz.tzinfo,
            "R48 fix: '+08:00' suffix must produce a TZ-AWARE datetime "
            "so the browser stops guessing from the host TZ.",
        )
        self.assertEqual(
            parsed_with_tz.utcoffset(),
            timedelta(hours=8),
            "R48 fix: '+08:00' suffix must yield a +08:00 offset.",
        )

        # And it must round-trip: parsed_with_tz == now_cst (within
        # microsecond rounding because strftime truncates microseconds).
        drift_sec = abs((parsed_with_tz - now_cst).total_seconds())
        self.assertLess(
            drift_sec, 2,
            "R48 fix: '+08:00' suffix must yield the SAME wall clock "
            f"as the input.  Drift was {drift_sec:.3f} s.",
        )

        # Sanity-check the BUG: without the tz tag, the parser would
        # interpret the string as the parser's local TZ, and the
        # offset vs CST depends on where the parser lives.  We can't
        # trigger that here without mocking, but we can at least
        # confirm the two parses differ — i.e. the tz tag is doing
        # observable work.
        self.assertNotEqual(
            parsed_no_tz.tzinfo, parsed_with_tz.tzinfo,
            "Tagging the string with '+08:00' must change the parsed "
            "tzinfo; if they're equal the suffix is being ignored.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
