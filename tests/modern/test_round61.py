"""Round 61 - data layer Pydantic-ification (pure internal refactor).

Background
==========
Pre-R61 the entire persistence layer lived in ``db.py``: a single file
holding SQLite helpers (``insert``, ``query``, ``latest``,
``record_daily_elec``, ``record_violation``, ``record_pay``,
``upsert_run_status``, ``get_run_status``, ``get_meta``, ``set_meta``
...) plus the meta-key constants and the schema.  Callers (``dorm_power``,
``web``, ``feishu_bot``) used ``import db`` and reached the symbols via
attribute access.

R61 refactor splits the file into a package::

    db/
        __init__.py   - re-exports every legacy symbol + new models/repos
        _legacy.py    - verbatim copy of the pre-R61 db.py (frozen)
        models.py     - Pydantic v2 entities (Record / DailyElec / ... /
                        StatsCard)
        repo.py       - Repository wrappers (RecordRepo / ... / MetaRepo)
                        with typed SQL accessors

The compat contract - **the entire reason this round is "purely
internal"** - is:

* Every legacy public symbol stays reachable as ``db.INSERT(...)`` /
  ``db.META_FOO``.
* No public-function signature changes.
* No schema / migration changes.
* No actual ``pip install`` performed; ``pydantic>=2.0,<3.0`` is added
  to ``requirements.txt`` but not installed here.

This test file exercises that contract statically (no runtime import
of pydantic, per the project rule "don't run pytest locally"):

T1  ``test_package_init_re_exports_legacy_symbols``
    - Walks ``db/__init__.py`` AST and asserts every legacy public
      function and every ``META_*`` constant is bound in the
      package namespace.  This is the core compat guarantee.
T2  ``test_models_module_has_seven_pydantic_classes``
    - Walks ``db/models.py`` AST and asserts the 7 Pydantic models
      (Record, DailyElec, Violation, Pay, RunStatus, MetaEntry,
      StatsCard) are defined as ``BaseModel`` subclasses.
T3  ``test_repo_module_has_six_repositories``
    - Walks ``db/repo.py`` AST and asserts every repository class is
      present plus the ``get_conn`` re-export.
T4  ``test_legacy_module_keeps_original_signatures``
    - Walks ``db/_legacy.py`` AST and confirms every legacy public
      function exists with the expected signature shape.
T5  ``test_legacy_module_has_uniqueness_migration``
    - Walks ``db/_legacy.py`` AST and confirms the R49
      ``_migrate_records_unique`` builder + R34D
      ``_migrate_dorm_room_id`` migrations are still inside
      ``init()``.
T6  ``test_requirements_txt_pins_pydantic_v2``
    - Greps ``requirements.txt`` for the v2 pin.  Defends against a
      drift back to ``pydantic>=1.0`` (which would silently
      re-introduce the v1 API surface).
T7  ``test_db_py_is_docstring_only_shim``
    - Confirms ``db.py`` is now a docstring-only sentinel kept at the
      project root so pre-R61 test files that ``py_compile`` /
      ``ast.parse`` / read ``db.py`` directly still find a valid
      Python file at that path.  The shim must contain no executable
      statements so it cannot shadow the canonical ``db/`` package
      on Python installs where the file form wins over the package
      form.
T8  ``test_py_compile_all_target_files``
    - ``py_compile`` every file the round touched.  Catches
      syntax / encoding issues without invoking pydantic at runtime.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
DB_INIT_PY = PROJ_DIR / "db" / "__init__.py"
DB_LEGACY_PY = PROJ_DIR / "db" / "_legacy.py"
DB_MODELS_PY = PROJ_DIR / "db" / "models.py"
DB_REPO_PY = PROJ_DIR / "db" / "repo.py"
DB_PY_ROOT = PROJ_DIR / "db.py"
REQUIREMENTS_TXT = PROJ_DIR / "requirements.txt"


def _read(path: Path) -> str:
    """Return file contents as text (utf-8)."""
    return path.read_text(encoding="utf-8")


def _ast(path: Path) -> ast.Module:
    """Parse a Python file into an AST without executing it."""
    return ast.parse(_read(path), filename=str(path))


def _names_from_importfrom(path: Path) -> list[str]:
    """Return every imported name from every ``from X import Y`` statement."""
    tree = _ast(path)
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out.append(alias.name)
    return out


def _classdefnames(path: Path) -> list[str]:
    """Return every class name declared anywhere in ``path``."""
    tree = _ast(path)
    return sorted({n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)})


def _funcdefnames(path: Path) -> list[str]:
    """Return every top-level function name declared in ``path``."""
    tree = _ast(path)
    return sorted(
        {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    )


# =========================================================================
# T1 - db/__init__.py re-exports every required legacy symbol
# =========================================================================
class TestPackageInitReExportsLegacySymbols(unittest.TestCase):
    """Every legacy public symbol must be bound by ``db.__init__``."""

    # The full set of public symbols the brief + the pre-R61 db.py
    # require.  Keeping this list explicit (rather than e.g.
    # ``__all__``-based) makes drift visible at test time.
    REQUIRED_LEGACY = [
        # connection / bootstrap
        "init",
        "get_conn",
        "_SCHEMA",
        # migrations (R49, R35)
        "_migrate_records_unique",
        "_migrate_dorm_room_id",
        # record-snapshot helpers
        "insert",
        "query",
        "latest",
        # coercion helpers (R34D PUBLIC)
        "_coerce_float",
        "_coerce_str",
        # daily_elec (F2)
        "record_daily_elec",
        "recent_daily_elec",
        # violations (F3)
        "record_violation",
        "recent_violations",
        # pay_history (F5)
        "record_pay",
        "recent_pay",
        # run_status (F4)
        "upsert_run_status",
        "get_run_status",
        # meta (R2 + R34A)
        "get_meta",
        "set_meta",
        "get_meta_float",
        "get_meta_int",
        "record_cadence_status",
        "set_scrape_status",
        # meta key constants - R34 push cadence
        "META_LAST_DAILY_REPORT",
        "META_LAST_WEEKLY_REPORT",
        "META_LAST_MONTHLY_REPORT",
        "META_PUSH_L1_ENABLE",
        "META_PUSH_L2_ENABLE",
        "META_PUSH_DAILY_ENABLE",
        "META_PUSH_WEEKLY_ENABLE",
        "META_PUSH_MONTHLY_ENABLE",
        "META_PUSH_DAILY_TIME",
        "META_PUSH_WEEKLY_TIME",
        "META_PUSH_MONTHLY_TIME",
        "META_PUSH_L1_RECEIVERS",
        "META_PUSH_L2_RECEIVERS",
        "META_PUSH_REPORT_RECEIVERS",
        "META_PUSH_ALERT_RECEIVERS",
        "META_QUIET_HOURS_START",
        "META_QUIET_HOURS_END",
        # meta key constants - R34 scrape config
        "META_DORM_BASE_URL",
        "META_DORM_OPENID",
        "META_DORM_ROOM_ID",
        "META_EQPRICE",
        "META_FEISHU_WEBHOOK_URL",
        "META_API_INTERNAL_TOKEN",
        # meta key constants - R34 OOBE
        "META_OOBE_STEP",
        "META_OOBE_COMPLETED",
        "META_ADMIN_PASSWORD",
    ]

    REQUIRED_R61_MODELS = [
        "Record",
        "DailyElec",
        "Violation",
        "Pay",
        "RunStatus",
        "MetaEntry",
        "StatsCard",
    ]

    REQUIRED_R61_REPOS = [
        "RecordRepo",
        "DailyElecRepo",
        "ViolationRepo",
        "PayRepo",
        "RunStatusRepo",
        "MetaRepo",
    ]

    def setUp(self) -> None:
        self.imported = set(_names_from_importfrom(DB_INIT_PY))

    def test_legacy_functions_re_exported(self) -> None:
        # ``__init__`` imports each legacy symbol with ``from db._legacy
        # import X``.  We assert via the parsed AST; we deliberately do
        # NOT do ``import db`` so the test stays pydantic-free.
        for sym in self.REQUIRED_LEGACY:
            self.assertIn(
                sym,
                self.imported,
                f"db.__init__ did not re-export legacy symbol {sym!r}; "
                f"this would break callers like `import db; db.{sym}(...)`",
            )

    def test_r61_models_re_exported(self) -> None:
        for sym in self.REQUIRED_R61_MODELS:
            self.assertIn(
                sym,
                self.imported,
                f"db.__init__ did not re-export new Pydantic model "
                f"{sym!r} from db.models",
            )

    def test_r61_repos_re_exported(self) -> None:
        for sym in self.REQUIRED_R61_REPOS:
            self.assertIn(
                sym,
                self.imported,
                f"db.__init__ did not re-export new repository "
                f"{sym!r} from db.repo",
            )

    def test_init_lists_all_required_symbols_in_dunder_all(self) -> None:
        # Defence against partial edits: ``__all__`` must mirror the
        # import block so ``from db import *`` callers also see every
        # symbol.
        src = _read(DB_INIT_PY)
        m = re.search(r"^__all__\s*=\s*\[(.*?)\]", src, re.DOTALL | re.MULTILINE)
        self.assertIsNotNone(
            m,
            "db/__init__.py must define __all__ explicitly so static "
            "analysis tools (ruff F401, pyflakes) do not flag the "
            "re-exports as unused.",
        )
        block = m.group(1)
        listed = re.findall(r'"([^"]+)"', block)
        # The ``from __future__ import annotations`` line is a
        # no-op-style import we deliberately do NOT put in __all__.
        # Filter it before comparing so the equality check is
        # meaningful (otherwise "annotations" would always be in
        # ``imported`` but never in ``__all__``).
        imported_sans_future = {
            s for s in self.imported if s != "annotations"
        }
        self.assertEqual(
            set(listed),
            imported_sans_future,
            "__all__ should list exactly the same names that are "
            "imported by the package init (excluding the no-op "
            "`from __future__ import annotations` line); if these "
            "drift the project static analysis rules will start "
            "flagging re-exports.",
        )


# =========================================================================
# T2 - db/models.py declares 7 Pydantic classes
# =========================================================================
class TestModelsModuleHasSevenPydanticClasses(unittest.TestCase):
    """The seven R61 Pydantic models must all be defined.

    We do NOT instantiate them here (pydantic isn't installed in the
    test environment by the project's "don't pip install" rule);
    instead we walk the AST and verify the class definitions exist,
    inherit from ``BaseModel``, and have the expected field structure.
    """

    EXPECTED = {
        "Record",
        "DailyElec",
        "Violation",
        "Pay",
        "RunStatus",
        "MetaEntry",
        "StatsCard",
    }

    def test_seven_classes_defined(self) -> None:
        names = set(_classdefnames(DB_MODELS_PY))
        self.assertTrue(
            self.EXPECTED.issubset(names),
            "db/models.py missing one or more Pydantic models. "
            f"Expected {sorted(self.EXPECTED)}, got {sorted(names)}",
        )

    def test_every_class_inherits_from_basemodel(self) -> None:
        tree = _ast(DB_MODELS_PY)
        basemodel_aliases = {"BaseModel"}
        # Find the imported pydantic.BaseModel name (could be aliased).
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "pydantic":
                for alias in node.names:
                    if alias.name == "BaseModel":
                        basemodel_aliases.add(alias.asname or alias.name)
        self.assertTrue(
            basemodel_aliases,
            "Could not find `from pydantic import BaseModel` in db/models.py",
        )

        class_models = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef)
        }
        missing = []
        for cls_name in self.EXPECTED:
            cls = class_models.get(cls_name)
            if not cls:
                missing.append(cls_name)
                continue
            bases = {b.id for b in cls.bases if isinstance(b, ast.Name)}
            if not (bases & basemodel_aliases):
                missing.append(cls_name)
        self.assertEqual(
            missing,
            [],
            "The following classes do not inherit from pydantic "
            f"BaseModel: {missing!r}. Each db.models class must be "
            "a Pydantic v2 BaseModel subclass to satisfy the R61 "
            "typed contract.",
        )

    def test_record_has_ts_validator(self) -> None:
        # R61 - Record.ts must have a field_validator that enforces
        # the "YYYY-MM-DD HH:MM:SS" format.  Find the Record class
        # and walk its body for a field_validator decorator.
        tree = _ast(DB_MODELS_PY)
        record_cls = next(
            (n for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef) and n.name == "Record"),
            None,
        )
        self.assertIsNotNone(record_cls, "Record class missing in db/models.py")
        found = False
        for node in ast.walk(record_cls):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Name)
                    and dec.func.id == "field_validator"
                ):
                    args = [a.value for a in dec.args if isinstance(a, ast.Constant)]
                    if "ts" in args:
                        found = True
                        break
        self.assertTrue(
            found,
            "db.models.Record must define a @field_validator('ts') to "
            "reject malformed scrape timestamps at the boundary.",
        )


# =========================================================================
# T3 - db/repo.py declares 6 typed repositories + get_conn
# =========================================================================
class TestRepoModuleHasSixRepositories(unittest.TestCase):
    """Every R61 repository wrapper plus the ``get_conn`` re-export."""

    EXPECTED = {
        "RecordRepo",
        "DailyElecRepo",
        "ViolationRepo",
        "PayRepo",
        "RunStatusRepo",
        "MetaRepo",
    }

    def test_six_repos_defined(self) -> None:
        names = set(_classdefnames(DB_REPO_PY))
        self.assertTrue(
            self.EXPECTED.issubset(names),
            "db/repo.py missing one or more repositories. "
            f"Expected {sorted(self.EXPECTED)}, got {sorted(names)}",
        )

    def test_get_conn_is_top_level_function(self) -> None:
        funcs = set(_funcdefnames(DB_REPO_PY))
        self.assertIn(
            "get_conn",
            funcs,
            "db/repo.py must re-export get_conn() at the top level so "
            "`from db.repo import get_conn` works for new callers.",
        )

    def test_each_repo_has_crud_methods(self) -> None:
        # Spot-check that each repo has the surface area the brief
        # described.  Allows different SQL choices (e.g. upsert vs
        # upsert_batch) but enforces at least the core CRUD verbs.
        tree = _ast(DB_REPO_PY)
        repo_classes = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.ClassDef) and n.name in self.EXPECTED
        }
        expectations = {
            "RecordRepo": {"insert", "latest", "query"},
            "DailyElecRepo": {"upsert_batch", "recent"},
            "ViolationRepo": {"upsert_batch", "recent"},
            "PayRepo": {"upsert_batch", "recent"},
            "RunStatusRepo": {"upsert", "get"},
            "MetaRepo": {"get", "set", "get_float", "get_int"},
        }
        for cls_name, expected_methods in expectations.items():
            cls = repo_classes.get(cls_name)
            self.assertIsNotNone(cls, f"{cls_name} missing from db/repo.py")
            method_names = {
                n.name
                for n in ast.walk(cls)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            missing = expected_methods - method_names
            self.assertEqual(
                missing,
                set(),
                f"{cls_name} is missing expected methods: {sorted(missing)}",
            )


# =========================================================================
# T4 - db/_legacy.py keeps original signatures (verbatim re-export)
# =========================================================================
class TestLegacyModuleKeepsOriginalSignatures(unittest.TestCase):
    """Every original public function must still exist in _legacy.py.

    Pinning the *function name* set is sufficient for the compat
    contract - we don't introspect parameter lists because the
    function bodies must remain byte-identical to the pre-R61
    ``db.py`` to honour the "frozen reference" promise that the
    ``db/_legacy.py`` docstring makes.
    """

    REQUIRED_FUNCS = {
        "init",
        "get_conn",
        "_migrate_records_unique",
        "_migrate_dorm_room_id",
        "insert",
        "query",
        "latest",
        "_coerce_float",
        "_coerce_str",
        "record_daily_elec",
        "recent_daily_elec",
        "record_violation",
        "recent_violations",
        "record_pay",
        "recent_pay",
        "upsert_run_status",
        "get_run_status",
        "get_meta",
        "set_meta",
        "get_meta_float",
        "get_meta_int",
        "record_cadence_status",
        "set_scrape_status",
    }

    def test_legacy_functions_present(self) -> None:
        # Walk every function (top-level + nested) to be tolerant of
        # helper functions defined inside larger helpers.
        tree = _ast(DB_LEGACY_PY)
        names = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
        }
        missing = self.REQUIRED_FUNCS - names
        self.assertEqual(
            missing,
            set(),
            "db/_legacy.py is missing original public functions: "
            f"{sorted(missing)}; this would break the no-API-drift "
            "guarantee for callers that still use the legacy "
            "function-style signatures.",
        )

    def test_init_calls_both_migrations(self) -> None:
        # Round 49 + Round 35 migrations must still be called from
        # inside init(); a partial copy could silently drop one of
        # them and corrupt production DBs.
        tree = _ast(DB_LEGACY_PY)
        init_fn = next(
            (n for n in tree.body
             if isinstance(n, ast.FunctionDef) and n.name == "init"),
            None,
        )
        self.assertIsNotNone(init_fn, "init() missing in db/_legacy.py")

        called_names = set()
        for node in ast.walk(init_fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
            ):
                called_names.add(node.func.id)
        self.assertIn(
            "_migrate_records_unique",
            called_names,
            "init() must still call _migrate_records_unique (R49) so "
            "legacy installs without UNIQUE(ts) get rebuilt.",
        )
        self.assertIn(
            "_migrate_dorm_room_id",
            called_names,
            "init() must still call _migrate_dorm_room_id (R35) so "
            "the legacy meta key is collapsed into last_room_id.",
        )

    def test_records_schema_has_unique_ts(self) -> None:
        # Round 49 - the records table schema must declare
        # ``ts TEXT NOT NULL UNIQUE``; the migration only triggers
        # when this marker is absent.
        src = _read(DB_LEGACY_PY)
        m = re.search(
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+records\s*\(([^)]+)\)",
            src,
            re.IGNORECASE | re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "CREATE TABLE IF NOT EXISTS records (...) block missing in "
            "db/_legacy.py",
        )
        body = re.sub(r"\s+", " ", m.group(1)).strip()
        self.assertIn(
            "ts TEXT NOT NULL UNIQUE",
            body,
            "records schema must retain `ts TEXT NOT NULL UNIQUE` "
            f"from R49; got body: {body!r}",
        )

    def test_meta_constants_present(self) -> None:
        # Snapshot the public META_* set so a future rename is caught
        # here rather than at the first caller that breaks.
        src = _read(DB_LEGACY_PY)
        meta_lines = re.findall(r"^(META_[A-Z0-9_]+)\s*=\s*", src, re.MULTILINE)
        expected_consts = [
            "META_LAST_DAILY_REPORT",
            "META_LAST_WEEKLY_REPORT",
            "META_LAST_MONTHLY_REPORT",
            "META_PUSH_L1_ENABLE",
            "META_PUSH_L2_ENABLE",
            "META_PUSH_DAILY_ENABLE",
            "META_PUSH_WEEKLY_ENABLE",
            "META_PUSH_MONTHLY_ENABLE",
            "META_PUSH_DAILY_TIME",
            "META_PUSH_WEEKLY_TIME",
            "META_PUSH_MONTHLY_TIME",
            "META_PUSH_L1_RECEIVERS",
            "META_PUSH_L2_RECEIVERS",
            "META_PUSH_REPORT_RECEIVERS",
            "META_PUSH_ALERT_RECEIVERS",
            "META_QUIET_HOURS_START",
            "META_QUIET_HOURS_END",
            "META_DORM_BASE_URL",
            "META_DORM_OPENID",
            "META_DORM_ROOM_ID",
            "META_EQPRICE",
            "META_FEISHU_WEBHOOK_URL",
            "META_API_INTERNAL_TOKEN",
            "META_OOBE_STEP",
            "META_OOBE_COMPLETED",
            "META_ADMIN_PASSWORD",
        ]
        missing = [c for c in expected_consts if c not in meta_lines]
        self.assertEqual(
            missing,
            [],
            "db/_legacy.py is missing one or more META_* public "
            f"constants: {missing!r}; callers expecting these would "
            "AttributeError.",
        )


# =========================================================================
# T5 - requirements.txt pins pydantic v2
# =========================================================================
class TestRequirementsTxtPinsPydanticV2(unittest.TestCase):
    """``requirements.txt`` must add ``pydantic>=2.0,<3.0`` exactly."""

    def test_pydantic_v2_pin(self) -> None:
        src = _read(REQUIREMENTS_TXT)
        self.assertIn(
            "pydantic>=2.0,<3.0",
            src,
            "requirements.txt must pin pydantic to the v2 major "
            "(`pydantic>=2.0,<3.0`); the v1 API surface is "
            "intentionally NOT supported by db.models.",
        )

    def test_v1_pin_rejected(self) -> None:
        # Defence - make sure no v1 leak accidentally landed.
        src = _read(REQUIREMENTS_TXT)
        for bad in ("pydantic>=1.0", "pydantic<2.0", "pydantic>=1.0,<2.0"):
            self.assertNotIn(
                bad,
                src,
                f"requirements.txt must not pin {bad!r}; pydantic v1 "
                "does not support the field_validator API db.models "
                "relies on.",
            )


# =========================================================================
# T6 - db.py is now a docstring-only deprecated shim (NOT canonical)
# =========================================================================
class TestDbPyIsDeadCompatShim(unittest.TestCase):
    """Confirms ``db.py`` is now a fallback shim, not the canonical body.

    Pre-R61, ``db.py`` held the entire persistence layer.  After R61
    the canonical body lives in ``db/__init__.py`` (re-export shim) +
    ``db/_legacy.py`` (frozen implementation) + ``db/models.py``
    (Pydantic v2 entities) + ``db/repo.py`` (typed repositories).
    Python's import system prefers the package form whenever
    ``db/__init__.py`` exists, so the package is canonical at runtime.

    The old ``db.py`` is KEPT at the project root ONLY as a sentinel
    for the many pre-R61 test files that ``py_compile`` /
    ``ast.parse`` / read it as a file.  This shim:

    * Has only a module docstring (zero executable statements) - so
      even if some pathological code path loads it via the legacy
      file-name route, no stale schema or signature can leak out.
    * Still parses cleanly (the R35 / R36 / R46 / R47 / R48 / R49 /
      R50 ``py_compile`` / AST guards continue to pass).
    * Explicitly directs new readers to the ``db/`` package.
    """

    def test_db_py_exists_as_shim(self) -> None:
        # Kept as a docstring-only sentinel.
        self.assertTrue(
            DB_PY_ROOT.exists(),
            f"{DB_PY_ROOT} must still exist at the project root as a "
            "compat shim.  Pre-R61 test files that do "
            "`py_compile.compile(str(PROJ_DIR / 'db.py'))` rely on a "
            "valid file at this path; removing it would force a wide "
            "edit across the existing test suite.",
        )

    def test_db_py_is_docstring_only(self) -> None:
        # The shim must contain ONLY a module docstring - any executable
        # code risks shadowing the package form on a Python install
        # where the file form wins over the package form.
        src = _read(DB_PY_ROOT)
        tree = ast.parse(src, filename=str(DB_PY_ROOT))
        # A docstring-only module has exactly one top-level node:
        # an ``Expr`` whose value is a ``Constant`` string.
        significant = [
            n for n in tree.body
            if not (
                isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant)
                and isinstance(n.value.value, str)
            )
        ]
        self.assertEqual(
            significant,
            [],
            "db.py must be a docstring-only shim - any executable code "
            "risks shadowing the db/ package on a Python install "
            "where the file form wins over the package form. "
            f"Significant nodes found: "
            f"{[type(n).__name__ for n in significant]!r}",
        )

    def test_db_py_shim_docstring_mentions_r61_deprecation(self) -> None:
        # Defence against drift: the shim must keep telling future
        # readers that the body moved to the db/ package.  Without
        # this marker a new contributor might "fix" the empty file
        # by re-adding the implementation directly.
        src = _read(DB_PY_ROOT)
        self.assertIn(
            "Round 61",
            src,
            "db.py shim docstring must mention R61 so future readers "
            "know it is the deprecated form.",
        )
        self.assertIn(
            "db/",
            src,
            "db.py shim docstring must point readers to the db/ "
            "package where the canonical implementation now lives.",
        )

    def test_db_package_still_canonical(self) -> None:
        # Sanity: the package form still wins (the shim is dead code
        # at import time) and every package module exists.
        self.assertTrue(
            (PROJ_DIR / "db").is_dir(),
            "db/ package directory must exist at the project root.",
        )
        for required in ("__init__.py", "_legacy.py", "models.py", "repo.py"):
            self.assertTrue(
                (PROJ_DIR / "db" / required).exists(),
                f"db/{required} missing - the package is incomplete.",
            )


# =========================================================================
# T8 - py_compile every file the round touched
# =========================================================================
class TestPyCompileAllTargetFiles(unittest.TestCase):
    """All five touched files must parse without syntax errors.

    This catches encoding / mismatched-bracket / Chinese-punctuation
    regressions without invoking pydantic at runtime (per the project
    rule "don't run pytest locally").
    """

    TARGETS = [
        DB_PY_ROOT,           # the R61 docstring-only compat shim
        DB_INIT_PY,
        DB_LEGACY_PY,
        DB_MODELS_PY,
        DB_REPO_PY,
        PROJ_DIR / "tests" / "modern" / "test_round61.py",
    ]

    def test_all_files_py_compile(self) -> None:
        import py_compile

        for path in self.TARGETS:
            with self.subTest(path=str(path)):
                py_compile.compile(str(path), doraise=True)
                print(f"  [py_compile OK] {path.name}")


# =========================================================================
# Run with ``python -m unittest tests.modern.test_round61`` (or pytest).
# Static-only; does NOT invoke pydantic at runtime.
# =========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
