"""db.py - Round 61 deprecated compat shim.

This file was retained at the project root ONLY so existing test
files that ``py_compile`` / ``ast.parse`` / read ``db.py`` directly
continue to find a valid Python file at this path (the R35, R36,
R46, R47, R48, R49, R50 guards each ``py_compile.compile(str(PROJ_DIR
/ "db.py"), doraise=True)`` as a syntax sentinel).

The actual implementation lives in the ``db/`` package::

    db/
        __init__.py   - the canonical public surface; re-exports
                        every legacy symbol + the R61 Pydantic
                        models / repository wrappers.
        _legacy.py    - verbatim copy of the pre-R61 db.py
                        (frozen reference for the snapshot tests).
        models.py     - Pydantic v2 entities (Record / DailyElec /
                        Violation / Pay / RunStatus / MetaEntry /
                        StatsCard).
        repo.py       - Typed SQL wrappers (RecordRepo /
                        DailyElecRepo / ... / MetaRepo).

When ``db/__init__.py`` exists (the normal case), Python's import
system prefers the package form, so ``import db`` always resolves
to ``db/__init__.py`` and this file is never loaded.  The
``db._legacy`` submodule is the single source of truth for the
SQL helpers and ``META_*`` constants; the package init re-exports
every public name so legacy callers (``import db; db.insert(...)``,
``db.META_LAST_DAILY_REPORT``, ``db._coerce_float``, ``db._SCHEMA``
...) keep working unchanged.

NEVER EDIT THIS FILE - any new code goes in db/, db/models.py, or
db/repo.py.  This shim exists purely as a fallback for tooling
that reads ``db.py`` from the filesystem by path.
"""
