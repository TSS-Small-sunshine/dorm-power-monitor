"""db package - Round 61 Pydantic refactor compat shim.

R61 split ``db.py`` into a regular Python package.  This ``__init__``
IS the canonical public surface for the persistence layer; every legacy
caller that used to do ``import db; db.insert(...)`` / ``db.get_meta(...)``
/ ``db.META_LAST_DAILY_REPORT`` keeps working without any code change
because the symbols are re-exported here verbatim.

The package layout is:

    db/
        __init__.py   - this file; re-exports everything
        _legacy.py    - verbatim copy of the pre-R61 db.py
        models.py     - Pydantic v2 entities (Record / DailyElec / ...)
        repo.py       - Repository wrappers (RecordRepo / MetaRepo / ...)

Public API surface (preserved from pre-R61 db.py):

    init, get_conn,
    insert, query, latest,
    record_daily_elec, recent_daily_elec,
    record_violation, recent_violations,
    record_pay, recent_pay,
    upsert_run_status, get_run_status,
    get_meta, set_meta, get_meta_float, get_meta_int,
    record_cadence_status, set_scrape_status,
    _coerce_float, _coerce_str,
    _migrate_records_unique, _migrate_dorm_room_id,
    _SCHEMA,
    META_LAST_DAILY_REPORT, META_LAST_WEEKLY_REPORT,
    META_LAST_MONTHLY_REPORT,
    META_PUSH_L1_ENABLE, META_PUSH_L2_ENABLE,
    META_PUSH_DAILY_ENABLE, META_PUSH_WEEKLY_ENABLE,
    META_PUSH_MONTHLY_ENABLE,
    META_PUSH_DAILY_TIME, META_PUSH_WEEKLY_TIME,
    META_PUSH_MONTHLY_TIME,
    META_PUSH_L1_RECEIVERS, META_PUSH_L2_RECEIVERS,
    META_PUSH_REPORT_RECEIVERS, META_PUSH_ALERT_RECEIVERS,
    META_QUIET_HOURS_START, META_QUIET_HOURS_END,
    META_DORM_BASE_URL, META_DORM_OPENID, META_DORM_ROOM_ID,
    META_EQPRICE, META_FEISHU_WEBHOOK_URL, META_API_INTERNAL_TOKEN,
    META_OOBE_STEP, META_OOBE_COMPLETED, META_ADMIN_PASSWORD,

R61 NEW surface:

    from db.models import Record, DailyElec, Violation, Pay, RunStatus,
                            MetaEntry, StatsCard
    from db.repo   import RecordRepo, DailyElecRepo, ViolationRepo,
                            PayRepo, RunStatusRepo, MetaRepo

R61 design decisions:

* ``_legacy`` is the only module that imports ``config`` and opens
  real SQLite connections.  ``db.models`` is pure Pydantic and has
  no DB dependency, so it is import-safe in any context (tests, CI,
  Jupyter).
* ``db.repo`` re-imports ``_legacy.get_conn`` lazily at call time,
  which lets tests monkey-patch the connection factory *after* the
  package import has completed (the pattern existing test files
  already follow).
* The package's ``__getattr__`` is intentionally NOT used; everything
  is explicitly listed in the ``__all__`` and the ``from-import``
  block below to keep static analysis tools happy (``pyflakes``,
  ``ruff F401``, IDE autocompletion).
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Layer 1 - re-export every legacy public symbol from db._legacy.
# ---------------------------------------------------------------------------
from db._legacy import (
    # connection / bootstrap
    init,
    get_conn,
    _SCHEMA,
    # internal migrations
    _migrate_records_unique,
    _migrate_dorm_room_id,
    # record-snapshot helpers
    insert,
    query,
    latest,
    # coercion helpers (PUBLIC per R34D)
    _coerce_float,
    _coerce_str,
    # daily_elec (F2)
    record_daily_elec,
    recent_daily_elec,
    # violations (F3)
    record_violation,
    recent_violations,
    # pay_history (F5)
    record_pay,
    recent_pay,
    # run_status (F4)
    upsert_run_status,
    get_run_status,
    # meta (R2 + R34A expanded)
    get_meta,
    set_meta,
    get_meta_float,
    get_meta_int,
    record_cadence_status,
    set_scrape_status,
    # meta key constants - R34 push cadence
    META_LAST_DAILY_REPORT,
    META_LAST_WEEKLY_REPORT,
    META_LAST_MONTHLY_REPORT,
    META_PUSH_L1_ENABLE,
    META_PUSH_L2_ENABLE,
    META_PUSH_DAILY_ENABLE,
    META_PUSH_WEEKLY_ENABLE,
    META_PUSH_MONTHLY_ENABLE,
    META_PUSH_DAILY_TIME,
    META_PUSH_WEEKLY_TIME,
    META_PUSH_MONTHLY_TIME,
    META_PUSH_L1_RECEIVERS,
    META_PUSH_L2_RECEIVERS,
    META_PUSH_REPORT_RECEIVERS,
    META_PUSH_ALERT_RECEIVERS,
    META_QUIET_HOURS_START,
    META_QUIET_HOURS_END,
    # meta key constants - R34 scrape config
    META_DORM_BASE_URL,
    META_DORM_OPENID,
    META_DORM_ROOM_ID,
    META_EQPRICE,
    META_FEISHU_WEBHOOK_URL,
    META_API_INTERNAL_TOKEN,
    # meta key constants - R34 OOBE
    META_OOBE_STEP,
    META_OOBE_COMPLETED,
    META_ADMIN_PASSWORD,
)

# ---------------------------------------------------------------------------
# Layer 2 - new Pydantic v2 domain entities (R61).
# ---------------------------------------------------------------------------
from db.models import (
    DailyElec,
    MetaEntry,
    Pay,
    Record,
    RunStatus,
    StatsCard,
    Violation,
)

# ---------------------------------------------------------------------------
# Layer 3 - new repository wrappers (R61).
# ---------------------------------------------------------------------------
from db.repo import (
    DailyElecRepo,
    MetaRepo,
    PayRepo,
    RecordRepo,
    RunStatusRepo,
    ViolationRepo,
)


__all__ = [
    # Legacy public surface (call signatures preserved bit-for-bit)
    "init",
    "get_conn",
    "_SCHEMA",
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
    # meta key constants
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
    # R61 Pydantic models
    "Record",
    "DailyElec",
    "Violation",
    "Pay",
    "RunStatus",
    "MetaEntry",
    "StatsCard",
    # R61 Repository pattern
    "RecordRepo",
    "DailyElecRepo",
    "ViolationRepo",
    "PayRepo",
    "RunStatusRepo",
    "MetaRepo",
]
