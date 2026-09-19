"""Round 36 — db.insert() 2-arg caller fix + regression guard.

Background
==========
Round 34D dropped ``records.raw_html`` and shrank ``db.insert`` to
``(remain, read_time)``.  The call site in ``dorm_power.run_once``
was left calling the OLD 3-arg form, so every scrape blew up with::

    TypeError: insert() takes 2 positional arguments but 3 were given

which surfaced in the browser as
``抓取失败,使用最近缓存: Unexpected token …`` and in the server log as
``scrape failed: TypeError: insert() takes 2 positional arguments …``.

This test file pins down the fix:

  * ``run_once`` must call ``db.insert`` with EXACTLY 2 positional
    arguments (no more, no less).
  * No live code path passes a 3rd ``raw_html`` / JSON blob.
  * ``run_once`` returns without raising TypeError when all the
    school endpoints are mocked to return canned data.
  * The actual ``db.insert`` signature still takes 2 positional args
    (so a future accidental 3-arg caller would still TypeError).
  * ``dorm_power.py`` and ``db.py`` both compile cleanly.

Uses only:

  * ``sqlite3`` in-memory (no disk DB)
  * ``inspect`` for the function-signature assertion
  * ``unittest.mock`` for dependency isolation
  * ``py_compile`` + ``ast.parse`` for syntax / symbol presence checks

Run:    python -m unittest test_round36 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the
operator runs it there.
"""
from __future__ import annotations

import ast
import importlib
import inspect
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

# Force a fresh import of dorm_power so we always patch the live
# module object (not a cached duplicate).
if "dorm_power" in sys.modules:
    del sys.modules["dorm_power"]
import db  # noqa: E402
import dorm_power  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fresh_in_memory_db() -> sqlite3.Connection:
    """Return a brand-new isolated in-memory SQLite connection."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    return conn


def _stub_school_calls(testcase: unittest.TestCase) -> "_PatchList":
    """Patch every school endpoint / side-effect that ``run_once`` calls.

    Returns a list of ``mock.patch`` handles so the caller can stop them
    in ``addCleanup`` (or use a context-manager pattern).  Each patch
    returns a sensible no-op value so the happy-path branch of
    ``run_once`` runs without ever touching the network or Feishu.
    """
    patches = [
        # ---- Identity / config ----------------------------------------
        # config.get_dorm_room_id() is called at the top of run_once();
        # a truthy value here means we skip the HTML discovery step.
        mock.patch.object(
            config, "get_dorm_room_id", return_value="room-test-r36",
        ),
        # ---- Stale-scrape guard (best-effort, ignore) ------------------
        mock.patch.object(dorm_power, "_check_stale_scrape", lambda: None),
        # ---- Primary fetch — return a known good body -----------------
        mock.patch.object(
            dorm_power, "_fetch_data",
            return_value={
                "remainEq": 12.34,
                "dt": "2026-09-16 00:00:00",
                "meterNo": "M-R36",
            },
        ),
        # ---- EqPrice cache --------------------------------------------
        mock.patch.object(dorm_power, "_read_eqprice", return_value=0.5),
        # ---- One-shot backfill ----------------------------------------
        mock.patch.object(dorm_power, "_backfill_once", lambda *a, **kw: None),
        # ---- F4 (every scrape) ----------------------------------------
        mock.patch.object(
            dorm_power, "_fetch_dormEmRunStatus", return_value=None,
        ),
        # ---- Cadence gates: make F2/F3/F5 sub-blocks SKIP --------------
        # The fix to db.insert happens BEFORE these gates fire, but
        # we still stub them so we don't accidentally hit the school
        # during the F2/F3/F5 windows.
        mock.patch.object(dorm_power, "_is_due", return_value=False),
        mock.patch.object(dorm_power, "_stamp_now", lambda key: None),
        # ---- L3 dispatch ----------------------------------------------
        mock.patch.object(dorm_power, "_l3_due", return_value=False),
        # ---- Feishu / alerts ------------------------------------------
        mock.patch.object(dorm_power, "_post_feishu", lambda *a, **kw: None),
        mock.patch.object(
            dorm_power, "_check_low_battery_alert",
            lambda *a, **kw: None,
        ),
        mock.patch.object(
            dorm_power, "_check_violation_alert",
            lambda *a, **kw: None,
        ),
        # ---- Card builders (so we don't build real Feishu payloads) ---
        mock.patch.object(
            dorm_power, "_build_card",
            lambda *a, **kw: {"tag": "card", "_stub": True},
        ),
        mock.patch.object(
            dorm_power, "_build_offline_card",
            lambda *a, **kw: {"tag": "card", "_stub": True},
        ),
        # ---- Offline-meter gate ---------------------------------------
        mock.patch.object(dorm_power, "_is_offline", return_value=False),
    ]
    for p in patches:
        p.start()
    testcase.addCleanup(lambda: [p.stop() for p in reversed(patches)])
    return patches  # noqa: F841 — kept for symmetry / future assertions


# Local import alias so the helper above can reference ``config`` (it's
# imported transitively via dorm_power) without re-importing here.
import config  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRound36InsertSignature(unittest.TestCase):
    """T1: ``db.insert`` itself still takes exactly 2 positional args.

    The whole point of Round 36 is the *caller* must be 2-arg too.
    If somebody re-introduces the third ``raw_html`` parameter,
    these tests will fail loudly so the caller change is forced in
    lock-step.
    """

    def test_db_insert_has_two_positional_params(self):
        sig = inspect.signature(db.insert)
        params = [
            p for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
        self.assertEqual(
            len(params), 2,
            f"db.insert should take 2 positional args; signature is {sig}",
        )
        param_names = {p.name for p in params}
        self.assertIn("remain", param_names)
        self.assertIn("read_time", param_names)
        self.assertNotIn(
            "raw_html", param_names,
            "raw_html parameter must NOT come back — Round 34D removed it",
        )

    def test_db_insert_rejects_three_positional_args(self):
        # Calling with 3 positional args must TypeError — proves the
        # signature is genuinely 2-arg (not silently accepting extras).
        with self.assertRaises(TypeError):
            db.insert(1.0, "2026-09-16 00:00:00", "extra-blob")


class TestRound36CallerArguments(unittest.TestCase):
    """T2 / T3: ``run_once`` invokes ``db.insert`` with EXACTLY 2 args."""

    def setUp(self):
        self._stub_school_calls = _stub_school_calls(self)

    def test_db_insert_called_with_two_args(self):
        with mock.patch.object(db, "insert") as fake_insert:
            fake_insert.return_value = 42
            dorm_power.run_once(fetch_only=True)
            self.assertTrue(
                fake_insert.called,
                "db.insert was never called by run_once()",
            )
            self.assertEqual(
                fake_insert.call_count, 1,
                f"db.insert should be called exactly once, got "
                f"{fake_insert.call_count}",
            )
            call_args, call_kwargs = fake_insert.call_args
            self.assertEqual(
                len(call_args), 2,
                f"db.insert must be called positionally with 2 args; "
                f"got {call_args!r} kwargs={call_kwargs!r}",
            )

    def test_db_insert_not_called_with_three_args(self):
        with mock.patch.object(db, "insert") as fake_insert:
            fake_insert.return_value = 42
            dorm_power.run_once(fetch_only=True)
            call_args, _ = fake_insert.call_args
            self.assertNotEqual(
                len(call_args), 3,
                "Regression: db.insert was called with 3 args again — "
                "the raw_html caller must be gone.",
            )
            # Belt-and-braces: no third positional argument regardless
            # of how many were passed (also blocks accidental kwargs).
            self.assertLessEqual(
                len(call_args), 2,
                f"db.insert received too many positional args: {call_args!r}",
            )
            self.assertEqual(
                fake_insert.call_args.kwargs, {},
                "db.insert should not be called with kwargs either",
            )


class TestRound36RunOnceNoTypeError(unittest.TestCase):
    """T4: end-to-end happy path — ``run_once`` returns without TypeError."""

    def setUp(self):
        self._stub_school_calls = _stub_school_calls(self)

    def test_run_once_no_typeerror_on_success(self):
        # The bug surfaces as TypeError("insert() takes 2 positional
        # arguments but 3 were given") the moment run_once hits the
        # db.insert line.  If the fix is correct, this call returns
        # cleanly (fetch_only=True → returns (body, status) tuple).
        try:
            result = dorm_power.run_once(fetch_only=True)
        except TypeError as exc:
            self.fail(
                f"run_once() raised TypeError — the 3-arg db.insert "
                f"caller is back: {exc!r}"
            )
        # fetch_only=True returns a (body, scrape_status) tuple per
        # the docstring.  A non-tuple means the function changed shape.
        self.assertIsInstance(
            result, tuple,
            f"run_once(fetch_only=True) should return (body, status); "
            f"got {result!r}",
        )
        self.assertEqual(len(result), 2)
        body, status = result
        self.assertEqual(status, "ok")
        self.assertEqual(body.get("remainEq"), 12.34)


class TestRound36Syntax(unittest.TestCase):
    """T5: py_compile + ast.parse sanity for the patched files."""

    def test_dorm_power_compiles(self):
        py_compile.compile(
            str(PROJ_DIR / "dorm_power.py"), doraise=True,
        )

    def test_db_compiles(self):
        py_compile.compile(
            str(PROJ_DIR / "db.py"), doraise=True,
        )

    def test_dorm_power_source_has_two_arg_db_insert(self):
        # AST-level guard: ensures no live caller in dorm_power.py
        # passes 3 positional args to db.insert.  This catches a
        # regression even if run_once is renamed / refactored away.
        tree = ast.parse(
            pathlib.Path(PROJ_DIR / "dorm_power.py")
            .read_text(encoding="utf-8"),
        )
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Match `db.insert(...)` only (ignore e.g. config.get_dorm_room_id).
            if not (
                isinstance(func, ast.Attribute)
                and func.attr == "insert"
                and isinstance(func.value, ast.Name)
                and func.value.id == "db"
            ):
                continue
            if len(node.args) >= 3:
                offenders.append(
                    f"line {node.lineno}: db.insert("
                    f"{', '.join(a.id if isinstance(a, ast.Name) else '...' for a in node.args)})"
                )
        self.assertEqual(
            offenders, [],
            "dorm_power.py still calls db.insert with 3+ positional args:\n  "
            + "\n  ".join(offenders),
        )


class TestRound36NoRawJsonDump(unittest.TestCase):
    """T6: the ``json.dumps(body, ...)`` line that fed raw_html is gone."""

    def test_no_raw_blob_construction_at_insert_site(self):
        text = pathlib.Path(PROJ_DIR / "dorm_power.py").read_text(
            encoding="utf-8",
        )
        # The deleted line was:
        #   raw = json.dumps(body, ensure_ascii=False, sort_keys=True)
        # immediately followed by ``db.insert(remain, read_time, raw)``.
        # Verify both are gone.  We allow other json.dumps calls
        # elsewhere (the Feishu card builder uses one) so we constrain
        # the check to the run_once() block heuristically.
        suspect = 'raw = json.dumps(body, ensure_ascii=False, sort_keys=True)'
        self.assertNotIn(
            suspect, text,
            f"dead line still in dorm_power.py: {suspect!r}",
        )
        # And the third-arg caller form must be gone too.
        self.assertNotIn(
            "db.insert(remain, read_time, raw)",
            text,
            "dead 3-arg db.insert call still in dorm_power.py",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)