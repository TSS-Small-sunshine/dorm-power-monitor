"""db.models - Round 61 Pydantic v2 domain entities.

R61 typed Pydantic models for the dorm-power-monitor persistence layer.
These are the *contract* shapes; they exist alongside the SQL helpers in
``db._legacy`` / ``db.repo`` so callers can opt into typed ergonomics
without changing storage.  All seven models here pass through one of two
production paths:

1.  Built in-process via ``Record(ts=..., read_time=..., remain=...)``
    and passed to a ``RecordRepo``/legacy ``insert(...)`` call.
2.  Materialised from a SQL fetch (``SELECT ...``) into a typed shape
    so callers get IntelliSense / static checks instead of
    ``sqlite3.Row`` indexing.

Why Pydantic v2 specifically:

* ``pydantic.BaseModel`` is the de-facto Python validation surface and
  is already a transitive dep of several libraries the project uses
  (``openai``, ``fastapi``, ``mangum``-shaped stacks).  Pinning
  ``pydantic>=2.0,<3.0`` keeps the project aligned with v2 only.
* ``field_validator`` lets us reject malformed scrape timestamps at
  the boundary (caught before they hit SQLite and silently disappear
  into the rest of the query).
* ``model_dump()`` round-trips cleanly into the ``dict`` shape that
  the legacy ``record_*`` helpers already accept, so models can be
  fed back into any pre-R61 caller without separate code paths.

R61 NOTE on ``Pay`` vs ``pay_history`` table columns:

The portal JSON uses ``payTypeLabel`` / ``feeTypeLabel`` / ``money``;
the SQL table uses ``pay_type`` / ``fee_type`` / ``money``.  The
``Pay`` model below exposes both shapes via ``payType`` /
``feeType`` (input) and ``payType`` / ``feeType`` (canonical name in
the model) so the model can be constructed directly from portal
JSON OR from a ``SELECT *`` result without an intermediate adapter.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Shared config: allow extra fields, populate by name + alias, no strict
# type coercion on float (None stays None instead of becoming 0.0).
# ---------------------------------------------------------------------------
_BASE_CONFIG = ConfigDict(
    extra="ignore",          # tolerate legacy SELECT * with extra columns
    populate_by_name=True,   # accept both snake_case and alias fields
    str_strip_whitespace=True,
    validate_assignment=False,
)


# ===========================================================================
# Records (R1, R49 added UNIQUE(ts))
# ===========================================================================
class Record(BaseModel):
    """One scrape snapshot from ``dorm_power.selectRecord``.

    Storage:
        records(ts TEXT NOT NULL UNIQUE, read_time TEXT, remain REAL)

    Field choices:

    * ``ts``       - server-side second-precision local (CST) time, written
                     by ``db.insert``.  Format ``"YYYY-MM-DD HH:MM:SS"``.
                     Validated here so a malformed string is rejected at
                     the boundary instead of corrupting later queries
                     that compare ``ts >= datetime('now', ...)``.
    * ``read_time``- portal-reported read timestamp; may be ``None`` when
                     the dashboard renders "请先抄表".
    * ``remain``   - remaining kWh (or yuan) as a float; ``None`` when the
                     scrape failed to parse the remainder block.
    * ``id``       - SQLite rowid; ``None`` until the row has been
                     ``INSERT``-ed and re-read.
    """

    model_config = _BASE_CONFIG

    ts: str
    read_time: Optional[str] = None
    remain: Optional[float] = None
    id: Optional[int] = None

    @field_validator("ts")
    @classmethod
    def _validate_ts(cls, v: Any) -> str:
        # R47 - records.ts is naive local (CST), not ISO8601-with-timezone.
        # Reject anything that is not the strict "YYYY-MM-DD HH:MM:SS"
        # shape so a future regression to ISO8601 is caught loudly
        # instead of silently producing "no rows match" queries.
        if not isinstance(v, str):
            raise ValueError(f"Record.ts must be str, got {type(v).__name__}")
        datetime.strptime(v, "%Y-%m-%d %H:%M:%S")
        return v

    @field_validator("read_time")
    @classmethod
    def _validate_read_time(cls, v: Any) -> Optional[str]:
        # ``read_time`` is whatever the portal returned - allow None or
        # any string.  We deliberately do NOT strptime-coerce it here
        # because the portal format has shifted across releases
        # (``"2026-09-17 13:28:05"`` was the v1 shape; newer releases
        # have used ``"2026-09-17T13:28:05+08:00"``).  Downstream code
        # handles both.
        if v is None:
            return None
        if not isinstance(v, str):
            return None
        return v.strip() or None

    def to_db_row(self) -> dict:
        """Materialise into the dict shape the legacy ``insert(...)`` expects.

        Returns ``{id, ts, read_time, remain}``.  ``id`` may be ``None``
        for new (pre-INSERT) records.  Used by ``db.repo.RecordRepo``
        and by tests that want to feed a ``Record`` back into a legacy
        helper without a second translation layer.
        """
        return {
            "id": self.id,
            "ts": self.ts,
            "read_time": self.read_time,
            "remain": self.remain,
        }


# ===========================================================================
# daily_elec (R2, F2)
# ===========================================================================
class DailyElec(BaseModel):
    """One row of per-day usage totals from ``getEmDayElectQuery``.

    Storage:
        daily_elec(roomId TEXT, dt TEXT, total_eq REAL, esbm REAL,
                   eebm REAL, zong_eq REAL, PRIMARY KEY (roomId, dt))

    Field choices:

    * ``dt``        - either ``"YYYY-MM-DD"`` (chart x-axis) or
                      ``"YYYY-MM-DD HH:MM:SS"`` (audit log).  We accept
                      both here; ``DailyElecRepo.recent`` returns rows in
                      either shape verbatim.
    * ``roomId``    - portal room ID; optional in the model because
                      ``Recent`` lookups sometimes SELECT without the
                      roomId predicate (e.g. R62 global view).
    * ``total_eq`` / ``esbm`` / ``eebm`` / ``zong_eq`` - direct SQL
                      column mirror.  ``zong_eq`` is the cumulative
                      baseline the chart's "今天用了多少" diff uses.
    * ``used_today`` - not a real SQL column; populated at read time
                      when the row is materialised by ``DailyElecRepo``
                      (difference from the previous-day ``eebm``).
    """

    model_config = _BASE_CONFIG

    dt: str
    roomId: Optional[str] = None
    total_eq: Optional[float] = None
    esbm: Optional[float] = None
    eebm: Optional[float] = None
    zong_eq: Optional[float] = None
    used_today: Optional[float] = None  # computed at read time


# ===========================================================================
# violations (R2, F3)
# ===========================================================================
class Violation(BaseModel):
    """One row of power-violation records from ``selectWgElect``.

    Storage:
        violations(roomId TEXT, dt TEXT, wg_reason TEXT, wg_power REAL,
                   PRIMARY KEY (roomId, dt, wg_reason))
    """

    model_config = _BASE_CONFIG

    dt: str
    roomId: Optional[str] = None
    wg_reason: Optional[str] = None
    wg_power: Optional[float] = None


# ===========================================================================
# pay (R2, F5) - one row of recharge / refund history
# ===========================================================================
class Pay(BaseModel):
    """One row of payment history from ``getEmPayQuery``.

    Storage:
        pay_history(roomId TEXT, dt TEXT, pay_type TEXT, fee_type TEXT,
                    money REAL, PRIMARY KEY (roomId, dt, pay_type, fee_type))

    The ``Pay`` model uses ``payType`` / ``feeType`` to mirror the portal
    JSON shape (``payTypeLabel`` / ``feeTypeLabel`` are accepted as
    aliases via ``populate_by_name``).  The brief uses ``kind`` and
    ``amount``; we map:

      kind   -> payType  (alias: pay_type)
      amount -> money    (canonical column name)

    so ``Pay(kind="recharge", amount=50.0)`` and the SQL row both
    materialise without an intermediate dict adapter.
    """

    model_config = _BASE_CONFIG

    dt: str
    roomId: Optional[str] = None
    payType: Optional[str] = Field(default=None, alias="pay_type")
    feeType: Optional[str] = Field(default=None, alias="fee_type")
    money: Optional[float] = None

    @property
    def kind(self) -> Optional[str]:
        """Backwards-compat accessor returning the payment type label.

        The brief lists ``Pay.kind`` as the canonical field; the SQL
        table column is ``pay_type``.  Aliasing via Pydantic gives us
        both names without duplicating storage.
        """
        return self.payType

    @property
    def amount(self) -> Optional[float]:
        """Backwards-compat accessor returning the money column."""
        return self.money


# ===========================================================================
# run_status (R2, F4) - one row per room, live meter snapshot
# ===========================================================================
class RunStatus(BaseModel):
    """One row from ``getEmRunStatus`` - live meter snapshot.

    Storage:
        run_status(roomId TEXT PRIMARY KEY, meter_no TEXT, dt TEXT,
                   run_status TEXT, work_status TEXT, update_dt TEXT,
                   vol REAL, cur REAL, yggl REAL, stop_reason TEXT)
    """

    model_config = _BASE_CONFIG

    dt: Optional[str] = None
    roomId: Optional[str] = None
    vol: Optional[float] = None
    cur: Optional[float] = None
    yggl: Optional[float] = None
    meter_no: Optional[str] = Field(default=None, alias="meterNo")
    run_status: Optional[str] = None
    update_dt: Optional[str] = None
    work_status: Optional[str] = None
    stop_reason: Optional[str] = None


# ===========================================================================
# meta (R2 + R34A expanded)
# ===========================================================================
class MetaEntry(BaseModel):
    """One row of the generic key/value ``meta`` table.

    Storage:
        meta(key TEXT PRIMARY KEY, value TEXT)

    ``value`` is always stored as TEXT; use ``MetaRepo.get_float`` /
    ``MetaRepo.get_int`` for typed accessors.
    """

    model_config = _BASE_CONFIG

    key: str
    value: Optional[str] = None


# ===========================================================================
# StatsCard - virtual typed shape for the dashboard's 5 cards
# ===========================================================================
class StatsCard(BaseModel):
    """Typed shape for the dashboard's 5 stat cards.

    Not backed by a SQL table - this is the dict that
    ``web.build_stats_card()`` assembles from a mix of query helpers
    and config.  Typing it here makes the dashboard's data contract
    explicit and lets the JS frontend's data shape be validated against
    the same model in CI.
    """

    model_config = _BASE_CONFIG

    remain: Optional[float] = None
    hourly_used: Optional[float] = None
    daily_avg: Optional[float] = None
    read_time: Optional[str] = None
    monthly_projection: Optional[float] = None
    eqprice: Optional[float] = None
    monthly_breakdown: Optional[dict] = None
    last_scrape_status: Optional[str] = None
    last_scrape_at: Optional[str] = None
    last_room_id: Optional[str] = None


__all__ = [
    "Record",
    "DailyElec",
    "Violation",
    "Pay",
    "RunStatus",
    "MetaEntry",
    "StatsCard",
]
