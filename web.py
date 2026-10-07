"""web.py — Round 64 documented.

Flask dashboard + JSON API + Feishu inbound endpoint.  Single Flask
app serves 4 user-facing routes and 1 bot endpoint; same module is
imported by ``dorm_power`` (for OOBE) and ``feishu_bot`` (for the
Pillow-backed card renderer).

主要功能 / Key responsibilities:
  * HTML dashboard (``/``) — 4 stat cards, Chart.js line, history table.
  * JSON read API (``/api/data``, ``/api/live``) consumed by the
    front-end JS every 30s.
  * ``/api/refresh`` (R26b) — manual force-scrape without Feishu
    notifications.
  * ``/healthz`` (R26a) — deep health check (db / school / feishu).
  * OOBE wizard (R34B) — first-run /api/oobe/* setup.
  * ``/feishu/event`` (R3) — inbound webhook dispatcher.
  * R64 — 5 admin pages + 5 admin JSON APIs + login UI + CSRF.

数据流 / Data flow:
  browser (HTML / fetch)  <-> web.py <-> db helpers <-> records.db
  Feishu user -> POST /feishu/event -> web.py -> feishu_bot

依赖 / Dependencies:
  * stdlib: ``json``, ``hmac``, ``secrets``, ``urllib.parse``
  * 3rd party: ``flask``, ``requests``
  * Local: ``config``, ``db``, ``dorm_power``, ``feishu_bot``

Round history:
  * R0   - scaffold + /feishu/event stub
  * R1   - 4 stat cards + Chart.js
  * R2   - 5 数据源 render + table
  * R3   - 飞书 bot inbound
  * R12  - 3 decrypt/verify/handle bug fixes
  * R26a - /healthz + retry + stale flag
  * R26b - /api/refresh + eqprice / monthly_projection in /api/live
  * R34A - L3 日报/周报/月报 + 双 cron endpoints
  * R34B - /api/refresh token + OOBE wizard (B1-B5+B8)
  * R34C - feishu fail-open 收紧
  * R34D - db.py cleanup
  * R36   - db.insert 3->2 参数修复
  * R37   - OOBE B1-B5+B8 业务代码
  * R39   - backfill UI
  * R41   - monthly_projection 算法 fix（用 last-first 而非累加）
  * R42   - _read_eqprice fallback chain (meta→env→0.5)
  * R62   - templates/ + static/ split + import-time _load_template
  * R63   - session-cookie auth (users / sessions / lockout / audit_log)
  * R64   - 5 admin pages + 5 admin APIs + login UI + CSRF protection

The original per-route docstring follows below for reference.

---

Flask dashboard for the dorm electricity monitor.

Exposes:
  GET /             HTML dashboard (4 stat cards + Chart.js line + table)
  GET /api/data     JSON, ?hours=24|72|168|720
  GET /api/live     JSON, latest stat-card values + live meter snapshot
                    (lightweight, polled every 30s by the dashboard JS)
                    Round 26a also reports ``stale`` so the Pillow card
                    can show a ⚠ marker when the cron is degraded.
                    Round 26b also reports ``eqprice`` + ``monthly_projection``
                    so the new "本月预计" stat card can render without
                    an extra round trip.
  POST /api/refresh Round 26b — force an immediate school scrape from
                    the browser.  Skips all Feishu notifications so a
                    user reloading the page doesn't flood the group.
  GET /healthz      Round 26a — deep health check (db / school / feishu).
                    Returns 200 when every dependency is OK, 503
                    otherwise.  Replaces the old plain "ok" liveness
                    probe.
  POST /feishu/event  Feishu bot inbound events (url_verification + commands)
  GET  /feishu/event  url_verification challenge echo (used during URL save)
  GET  /login         R64 — login form
  GET  /admin         R64 — admin landing (redirects to /admin/config)
  GET  /admin/users   R64 — user CRUD page
  GET  /admin/config  R64 — scrape / push / password config page
  GET  /admin/test    R64 — test-scrape / test-push page
  GET  /admin/audit   R64 — audit_log viewer
  POST/PUT/DELETE /api/admin/users[/<id>]  R64 — user CRUD
  GET/PUT /api/admin/config                 R64 — system config JSON
  GET  /api/admin/audit                     R64 — audit_log paginated
  POST /api/admin/test-scrape               R64 — manual scrape trigger
  POST /api/admin/test-push                 R64 — manual webhook test
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from calendar import monthrange
from datetime import datetime, date
from functools import wraps
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests
from flask import Flask, Response, jsonify, redirect, render_template_string, request, session

import auth
from auth import require_auth
from auth import require_csrf
import config
import db
import feishu_bot

# Round 26a — module-level Session for /healthz's outbound probe.
# Re-using one Session keeps the dashboard's TCP/TLS pool warm; we
# only need a single probe per /healthz call.
_HEALTHZ_SESSION = requests.Session()
_HEALTHZ_SESSION.headers.update({
    "User-Agent": "dorm-power-monitor/1.0 (healthz)",
    "Accept": "text/html,application/x-www-form-urlencoded,*/*",
})

# /healthz: report "stale" when the most-recent successful scrape
# landed more than this many seconds ago.  Set generously above the
# cron interval (600s) so an expected delay (e.g. cron coalesces) does
# not flip the endpoint red.
_HEALTHZ_STALE_THRESHOLD_SEC = 30 * 60

# Round 33b — chart scope tightened:
#  * ``_DAILY_CHART_MIN_DT`` anchors the chart at the user's explicit
#    "starts from 9/8" floor.  Rows before this anchor do NOT update
#    ``prev_total`` so the first row in the valid range becomes a
#    clean baseline (its used_today is None and is skipped by the
#    existing Round 33 filter below).
#  * ``_DAILY_CHART_MAX_KWH = 50.0`` rejects per-day deltas above
#    50 kW·h.  Real per-day usage for a single dorm is ≈ 27–30 kW·h
#    (Round 31 stats); 50 is the existing Round 21
#    ``_MAX_DAILY_KWH`` cap, kept conservative.  This catches the
#    F1-backfill "useEq jumps from 0 (baseline) to 7613.90
#    (cumulative since install)" outlier at 9/2 11:48 that broke the
#    Y axis at 0–8000 in the user's Round 33 screenshot.
_DAILY_CHART_MIN_DT = "2026-09-08"
_DAILY_CHART_MAX_KWH = 50.0

app = Flask(__name__)
logger = logging.getLogger("web")

# Round 63 — wire session-cookie auth into Flask.
#   - SECRET_KEY is read from FLASK_SECRET_KEY (.env).  ``auth.flask_secret_key``
#     generates a random one at import time and stashes it into os.environ
#     so subsequent restarts reuse the same value (existing signed cookies
#     stay valid across process restarts).
#   - ``SESSION_COOKIE_HTTPONLY=True`` blocks JS access to the cookie
#     (mitigates XSS-driven session theft).
#   - ``SESSION_COOKIE_SAMESITE='Lax'`` is the right balance: the cookie
#     rides on top-level GET navigations but NOT on cross-site POST
#     (mitigates CSRF on /api/auth/* POSTs).
#   - ``PERMANENT_SESSION_LIFETIME`` aligns with ``auth.DEFAULT_SESSION_HOURS``
#     so Flask's cookie expiration matches the server-side session row.
#   - ``app.config['JSON_AS_ASCII'] = False`` keeps non-ASCII usernames
#     (Chinese / emoji) readable in error messages.
app.secret_key = auth.flask_secret_key()
# R69 — default to Secure cookie (HTTPS-only).  Local plain-HTTP dev
# can flip ``DORM_COOKIE_SECURE=0`` in .env to keep the cookie
# rideable on http://127.0.0.1:5000.
_cookie_secure = os.environ.get("DORM_COOKIE_SECURE", "1") == "1"
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=_cookie_secure,  # NEW — R69
    PERMANENT_SESSION_LIFETIME=auth.DEFAULT_SESSION_HOURS * 3600,  # seconds
    JSON_AS_ASCII=False,
)


# ---------------------------------------------------------------------------
# Round 64 — CSRF cookie propagation.
#
# ``auth.csrf_cookie_token()`` stashes a transient ``make_response``
# object on ``flask.g._csrf_cookie_response`` whenever it mints a new
# token.  This ``after_request`` hook copies the ``Set-Cookie`` header
# from that stash onto the real outgoing response so the browser
# receives ``dorm_csrf`` on the very first GET that triggers a fresh
# token.  Once planted, the cookie rides on subsequent requests (Lax
# SameSite) so the front-end's ``fetch`` wrapper can echo it back as
# ``X-CSRF-Token`` on every mutating call.
# ---------------------------------------------------------------------------
@app.after_request
def _propagate_csrf_cookie(response):
    """Copy the staged CSRF Set-Cookie header onto the real response."""
    try:
        from flask import g
        staged = getattr(g, "_csrf_cookie_response", None)
        if staged is not None:
            cookie_header = staged.headers.get("Set-Cookie")
            if cookie_header:
                response.headers.add("Set-Cookie", cookie_header)
    except Exception:  # pragma: no cover — never break the response path
        pass
    return response


# Round 64 — Jinja context processor so every template can render
# ``<meta name="csrf-token" content="{{ csrf_token() }}">`` without
# importing the auth module directly.  Keeps the templates free of
# Python-side dependencies.
@app.context_processor
def _inject_csrf_token():
    return {"csrf_token": auth.csrf_token}

# Round 63 — bootstrap: run db.init() so the 4 new auth tables
# (users / sessions / failed_attempts / audit_log) exist before any
# auth helper touches them.  Then ensure_initial_admin() seeds the
# first admin user when ``users`` is empty AND
# ``AUTH_INITIAL_ADMIN_PASSWORD`` is set in .env.
#
# This call is idempotent (CREATE TABLE IF NOT EXISTS) and safe on
# existing deployments — see db/_legacy.py:_SCHEMA.
db.init()
auth.ensure_initial_admin()

DEFAULT_HOURS = 24
ALLOWED_HOURS = (24, 72, 168, 720)


def query(
    hours: int = DEFAULT_HOURS,
    start_dt: Optional[str] = None,
    end_dt: Optional[str] = None,
) -> list[dict]:
    """Return rows within the requested time window.

    Implementation note (the web.py query() fix):
        The earliest version of this project passed `hours` straight
        into `LIMIT ?`, so `hours=24` meant "the most recent 24 rows"
        rather than "rows in the last 24 hours".  Once the scraper had
        been running for a few days, the chart and the stat cards both
        showed the wrong slice of data.  The underlying SQL is now a
        time-windowed `WHERE ts >= datetime('now', ?)` (see
        db.query()); `hours` is the window length, never a row count.

    Round 33d — extended with ``start_dt`` / ``end_dt`` for the
    dashboard's 采集记录 (records) panel.  When either is provided the
    explicit range takes precedence over ``hours``.  See ``db.query``
    for the full precedence contract.
    """
    # Round 33d — when an explicit range is given, bypass the
    # ALLOWED_HOURS gate (the dashboard's records panel accepts any
    # datetime window, not just 24/72/168/720).
    if start_dt is None and end_dt is None and hours not in ALLOWED_HOURS:
        hours = DEFAULT_HOURS
    return [dict(r) for r in db.query(hours, start_dt=start_dt, end_dt=end_dt)]


def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _stats(rows: list[dict]) -> dict:
    """Compute the 4 stat-card values from the rows."""
    valid = [(r["ts"], _safe_float(r["remain"])) for r in rows]
    valid = [(t, v) for t, v in valid if v is not None]

    if not valid:
        return {
            "remain": None,
            "hourly_used": None,
            "read_time": None,
            "daily_avg": None,
        }

    latest_ts, latest_remain = valid[-1]
    read_time = next(
        (r.get("read_time") for r in reversed(rows) if r.get("read_time")),
        None,
    )

    # Past-hour usage: difference vs the row at least ~1h before the latest.
    hourly_used: Optional[float] = None
    if len(valid) >= 2:
        try:
            latest_dt = datetime.fromisoformat(latest_ts)
        except ValueError:
            latest_dt = None
        for ts, v in reversed(valid[:-1]):
            try:
                d = datetime.fromisoformat(ts)
            except ValueError:
                continue
            if latest_dt is None or (latest_dt - d).total_seconds() >= 3500:
                hourly_used = round(v - latest_remain, 2) if v >= latest_remain else None
                break

    # Daily average: (oldest - newest) / span in days, if span >= 1 day.
    daily_avg: Optional[float] = None
    if len(valid) >= 2:
        try:
            oldest_dt = datetime.fromisoformat(valid[0][0])
            newest_dt = datetime.fromisoformat(latest_ts)
            span_days = (newest_dt - oldest_dt).total_seconds() / 86400
            if span_days >= 1:
                daily_avg = round((valid[0][1] - latest_remain) / span_days, 2)
        except ValueError:
            pass

    return {
        "remain": round(latest_remain, 2),
        "hourly_used": hourly_used,
        "read_time": read_time,
        "daily_avg": daily_avg,
    }


def _round2(v) -> Optional[float]:
    """Round a value to 2dp; pass through None unchanged."""
    if v is None:
        return None
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return None


def _round3(v) -> Optional[float]:
    """Round a value to 3dp; pass through None unchanged."""
    if v is None:
        return None
    try:
        return round(float(v), 3)
    except (TypeError, ValueError):
        return None


def _read_eqprice() -> Optional[float]:
    """Read the cached electricity unit price (¥/kW·h).

    Round 42 — fallback chain matches ``dorm_power._read_eqprice`` so
    the dashboard and the cron agree on the same value regardless of
    which one wrote it first::

        1. ``db.get_meta("eqprice")`` — cached by ``dorm_power.run_once``
           on the first successful scrape (Round 34A contract);
        2. ``os.environ["DORM_EQPRICE"]`` — operator override;
        3. ``0.5`` — hard default (matches 福建职业技术学院 dorm rate).

    Returns ``None`` only if every source above is empty AND the default
    also fails to parse (defensive — should never happen).  Before Round
    42 a missing meta key returned ``None`` directly and starved
    ``/api/live``'s ``monthly_projection`` → dashboard "¥—".
    """
    raw = db.get_meta("eqprice")
    if raw is None or not raw:
        raw = os.environ.get("DORM_EQPRICE", "")
    if not raw:
        raw = "0.5"  # Round 34A B1 default
    try:
        return round(float(raw), 4)
    except (TypeError, ValueError):
        return None


def _compute_monthly_projection(
    eqprice: Optional[float],
    stats: dict,
    room_id: Optional[str],
) -> dict:
    """Compute the "本月预计电费" payload.

    Round 41 — projection formula:
        monthly_projection = (used_kwh + avg_daily * days_left) * eqprice

    Where:
      * ``used_kwh`` is the *delta* of cumulative meter readings
        (F2 ``daily_elec.eebm`` → ``zong_eq``) between the first and
        last day observed this calendar month.  Round 41 fixes a
        Round-26b-era bug where the function summed cumulative
        readings (e.g. 7745.97 + 7775.53 + ... + 7863.78 = 39030)
        and then divided by the day count, producing a meaningless
        "average cumulative meter" of ~7806 kWh/day instead of the
        real ~29 kWh/day the school actually bills.

        With 1 day observed there is no delta — used_kwh stays
        None (no projection possible).
        With 2+ days: used_kwh = last_zong - first_zong.
        Negative delta (school swapped the meter) → used_kwh=None,
        days_observed reset to 0 to keep the projection honest.
      * ``avg_daily`` is the most informative of:
          1. used_kwh / (days_observed - 1) — month-to-date average.
             ``days_observed - 1`` because the first observed day has
             no prior reading to subtract from, so it contributes 0
             deltas.  This makes the math explicit instead of
             accidentally double-counting an "extra" zero delta.
          2. ``stats["daily_avg"]`` (windowed average from /api/data)
             — used only when we have < 2 days of monthly data so
             the projection isn't stranded.
          3. ``None`` if both are missing.
      * ``days_left`` is ``calendar_days_in_month - today.day``.

    The result is a dict with all three components so the dashboard
    can label them; ``monthly_projection`` itself is the rounded ¥ value.
    Returns a dict with all fields ``None`` when ``eqprice`` is missing
    (the dashboard renders "—" rather than 0).
    """
    today = date.today()
    days_in_month = monthrange(today.year, today.month)[1]
    days_left = max(days_in_month - today.day, 0)

    # Collect this-month (day, zong_eq) pairs.  zong_eq is the *cumulative*
    # meter reading as of that day, NOT the per-day consumption — the
    # per-day consumption has to be reconstructed by subtracting.
    month_zongs: list[tuple[date, float]] = []
    if room_id:
        for r in db.recent_daily_elec(room_id, days=31):
            raw_dt = r.get("dt")
            if not raw_dt:
                continue
            try:
                d = date.fromisoformat(str(raw_dt)[:10])
            except ValueError:
                continue
            if d.year != today.year or d.month != today.month:
                continue
            if d.day > today.day:
                continue
            z = _safe_float(r.get("zong_eq"))
            if z is None or z <= 0:
                continue
            month_zongs.append((d, z))

    # Default: no data → projection unavailable.
    used_kwh: Optional[float] = None
    days_observed = 0

    if len(month_zongs) >= 2:
        days_observed = len(month_zongs)
        delta = round(month_zongs[-1][1] - month_zongs[0][1], 3)
        if delta < 0:
            # Negative delta — the school swapped the meter / reset
            # the cumulative counter mid-month.  Don't pretend we have
            # a real consumption number; force the fallback path below.
            used_kwh = None
            days_observed = 0
        else:
            used_kwh = delta
    elif len(month_zongs) == 1:
        # Single day observed: no delta is computable, but the day
        # count is non-zero so the frontend can show a "今天已记录" hint.
        days_observed = 1
        used_kwh = None

    # avg_daily precedence: month-to-date (when we have 2+ days so the
    # delta is meaningful) → stats.daily_avg (windowed historical) → None.
    if used_kwh is not None and days_observed > 1:
        # days_observed - 1 = number of inter-day deltas that sum to used_kwh.
        # E.g. 5 rows (9/11..9/15) → 4 deltas → avg_daily = total/4.
        avg_daily = round(used_kwh / (days_observed - 1), 3)
    else:
        avg_daily = _safe_float(stats.get("daily_avg"))

    if eqprice is None or eqprice <= 0 or avg_daily is None:
        return {
            "eqprice": eqprice,
            "used_kwh": round(used_kwh, 2) if used_kwh else None,
            "days_observed": days_observed,
            "days_left": days_left,
            "avg_daily": avg_daily,
            "monthly_projection": None,
        }

    # used_kwh may still be None here (only happens if stats.daily_avg
    # rescued us from a 1-day month).  Treat None as 0 for the ¥ math —
    # the user still gets a projection based on the historical average.
    base = used_kwh if used_kwh is not None else 0.0
    projected_kwh = base + avg_daily * days_left
    monthly_projection = round(projected_kwh * eqprice, 2)
    return {
        "eqprice": eqprice,
        "used_kwh": round(base, 2) if base else None,
        "days_observed": days_observed,
        "days_left": days_left,
        "avg_daily": round(avg_daily, 3),
        "monthly_projection": monthly_projection,
    }


def _current_room_id() -> Optional[str]:
    """Read the roomId the scraper most recently discovered.

    Stored in ``meta.last_room_id`` by ``dorm_power.run_once()`` after
    the finduser-shell page is parsed.  Returns None before the first
    scrape, in which case the new dashboard sections render empty.
    """
    return db.get_meta("last_room_id")


def _build_round2_context() -> dict:
    """Assemble the four new context variables for the template.

    All four are best-effort: if the scraper hasn't run yet, or the
    meta key is missing, the corresponding section renders an empty
    state ("暂无...").  We round numerics here so the template can
    trust the values are already display-ready.
    """
    room_id = _current_room_id()
    if not room_id:
        return {
            "daily_elec": [],
            "violations": [],
            "pay_history": [],
            "run_status": None,
        }

    # Round 32 — daily_elec stores *cumulative* meter readings in
    # zong_eq / total_eq (the school portal emits the cumulative meter
    # number, not per-day kWh).  The "daily chart" used to plot zong_eq
    # directly → Y axis landed at the cumulative total (≈ 7800 kWh
    # since install), not the per-day delta the user expects
    # (≈ 27 kWh / day).  Compute used_today as the delta vs the
    # previous day's row.  Rows come back ordered ASC so prev_total
    # stays in step.
    #
    # Round 33 — exclude rows with no comparable delta from the chart
    # entirely.  The school's daily_elec payload has occasional reset
    # readings (e.g. 2026-09-07 where zong_eq < prev_total) and we
    # can't fabricate a delta.  prev_total is still updated so the
    # next row's delta is anchored against the latest known cumulative
    # reading.  The chart naturally starts from the first row that
    # has a valid delta — if 9/7 is the only anomaly, that's 9/8.
    #
    # Round 33b — daily_elec carries two flavors of rows:
    #   * F2 ``getEmDayElectQuery`` rows — dt is a date string
    #     (``2026-09-09``), zong_eq is the cumulative meter reading;
    #     deltas between consecutive rows are real per-day kWh.
    #   * F1-backfill ``dormEmQuery`` rows — dt is a full timestamp
    #     (``2026-09-02 11:48:44``), the first row after install has
    #     useEq=0 (baseline) and the next row has useEq = cumulative
    #     since install (≈ 7613.90 kWh at 9/2 in the Round 33
    #     screenshot) — those produce a fake delta far outside the
    #     real 27–30 kWh/day band and crush the Y axis to 0–8000.
    # ``_DAILY_CHART_MIN_DT`` (the user's "starts from 9/8" floor)
    # excludes both flavors of pre-anchor rows AND prevents them from
    # updating ``prev_total``, so the first in-range row becomes a
    # clean baseline (its used_today is None and the Round 33
    # filter below drops it).  ``_DAILY_CHART_MAX_KWH`` then catches
    # any in-range outlier caused by a future data-source switch.
    daily = []
    prev_total: Optional[float] = None
    in_valid_range = False
    for r in db.recent_daily_elec(room_id, days=30):
        raw_dt = str(r.get("dt", "") or "")
        eebm = _safe_float(r.get("eebm"))
        # Round 33c — F1-backfill rows (no eebm, useEq = cumulative since
        # install) MUST NOT be mixed with F2 rows (eebm = cumulative
        # end-of-day) in the chart.  Filter to F2 only.
        if eebm is None:
            continue
        # Round 33b — pre-anchor rows are out-of-chart AND out-of-state
        if not in_valid_range:
            if raw_dt < _DAILY_CHART_MIN_DT:
                continue
            in_valid_range = True
        # Round 33c — use eebm directly as the cumulative value (it's
        # already authoritative from F2).  Falls back to zong_eq for any
        # legacy row that doesn't carry eebm (defensive — should not
        # happen now that record_daily_elec always stores eebm when F2
        # supplies it, and the F1 path no longer writes daily_elec).
        zong = eebm
        if zong is None:
            zong = _safe_float(r.get("zong_eq"))
        if zong is None:
            zong = _safe_float(r.get("total_eq"))
        if prev_total is not None and zong is not None and zong >= prev_total:
            used_today: Optional[float] = round(zong - prev_total, 2)
        else:
            used_today = None  # first row in range OR reset
        if zong is not None:
            prev_total = zong
        if used_today is None:
            continue  # Round 33: skip — neither X-axis nor Y-data
        # Round 33b — outlier filter
        if used_today > _DAILY_CHART_MAX_KWH:
            continue
        daily.append({
            "dt":         r.get("dt"),
            "zong_eq":    _round2(r.get("zong_eq")),
            "total_eq":   _round2(r.get("total_eq")),
            "esbm":       _round2(r.get("esbm")),
            "eebm":       _round2(r.get("eebm")),
            "used_today": used_today,
        })

    violations = [
        {
            "dt":       r.get("dt"),
            "wg_reason": r.get("wg_reason"),
            "wg_power": _round2(r.get("wg_power")),
        }
        for r in db.recent_violations(room_id, days=30)
    ]

    pay = [
        {
            "dt":       r.get("dt"),
            "pay_type": r.get("pay_type"),
            "fee_type": r.get("fee_type"),
            "money":    _round2(r.get("money")),
        }
        for r in db.recent_pay(room_id, days=30)
    ]

    rs = db.get_run_status(room_id)
    if rs is not None:
        # Round 32 — "最后上报时间" was rendering "—" even when the
        # meter had clearly been probed.  The school portal sometimes
        # omits updateDt (the runStatus endpoint field name), leaving
        # the DB column NULL.  Fall back to rs.dt (the row's own data
        # timestamp) and then to the most-recent scrape row's
        # read_time / ts so the meter card never shows a blank when
        # the meter has reported anything at all.
        latest_row_fb = db.latest()
        meter_update_dt = (
            rs.get("update_dt")
            or rs.get("dt")
            or (latest_row_fb.get("read_time") if latest_row_fb else None)
            or (latest_row_fb.get("ts")        if latest_row_fb else None)
        )
        run_status = {
            "meter_no":    rs.get("meter_no"),
            "dt":          rs.get("dt"),
            "run_status":  rs.get("run_status"),
            "work_status": rs.get("work_status"),
            "update_dt":   meter_update_dt,
            "vol":         _round3(rs.get("vol")),
            "cur":         _round3(rs.get("cur")),
            "yggl":        _round3(rs.get("yggl")),
            "stop_reason": rs.get("stop_reason"),
        }
    else:
        run_status = None

    return {
        "daily_elec":   daily,
        "violations":   violations,
        "pay_history":  pay,
        "run_status":   run_status,
    }


# R62 — HTML/CSS/JS moved to templates/ + static/.  Python loads each
# Jinja-bearing template once at module import time and keeps using
# render_template_string(...) so the route handlers stay unchanged.
# A future R63 can flip these to render_template(...) + an explicit
# ``app.template_folder`` if we want Flask's own loader caching.
def _load_template(name: str) -> str:
    """Read templates/<name> from disk; called once at module import."""
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "templates", name
    )
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


INDEX_HTML = _load_template("dashboard.html")



@app.route("/")
def index():
    # Round 34B — OOBE redirect.  Until the user finishes the 6-step
    # wizard we send them to /oobe so the dashboard doesn't render
    # with broken / empty meta values.  This is the only public-
    # facing route; once OOBE is done, /admin/* becomes the
    # password-protected one for ongoing tweaks.
    #
    # Round 65 — the canonical wizard entry is now /oobe (not
    # /admin/oobe).  The decision logic reads BOTH the legacy
    # ``db.get_meta('oobe_completed')`` AND the R65
    # ``session['oobe_complete']`` flag so a user who completed
    # the wizard in this session does not get bounced back into it
    # after a refresh.
    oobe_done = (
        session.get("oobe_complete") is True
        or db.get_meta("oobe_completed") == "1"
    )
    if not oobe_done:
        return redirect("/oobe")

    hours = int(request.args.get("hours", DEFAULT_HOURS))
    rows = query(hours)
    stats = _stats(rows)
    round2 = _build_round2_context()
    return render_template_string(
        INDEX_HTML,
        stats=stats,
        hours=hours,
        rows=rows,
        daily_elec=round2["daily_elec"],
        violations=round2["violations"],
        pay_history=round2["pay_history"],
        run_status=round2["run_status"],
    )


@app.route("/api/data")
def api_data():
    hours = int(request.args.get("hours", DEFAULT_HOURS))
    # Round 33d — explicit range filter takes precedence over hours.
    start = request.args.get("start")
    end = request.args.get("end")
    rows = query(hours, start_dt=start, end_dt=end)
    return jsonify({
        "hours": hours,
        "stats": _stats(rows),
        "rows": [
            {
                "id": r["id"],
                "ts": r["ts"],
                "read_time": r["read_time"],
                "remain": r["remain"],
            }
            for r in rows
        ],
    })


@app.route("/api/live")
def api_live():
    """Return live data for AJAX polling (called every 30s by the dashboard).

    Combines the most-recent scrape row (for the "在线" pill and the
    stat cards) and the most-recent meter snapshot (for the 实时电表
    panel).  The shape is intentionally separate from /api/data so
    polling is cheap and doesn't ship the full history every tick.

    Round 26a: also reports ``scrape_status`` ("ok" / "stale" / "failed")
    so the Pillow render can overlay a ⚠ marker when the cron is in
    degraded mode.  ``stale`` is a convenience bool derived from the
    same meta key.

    Round 26b: also reports ``eqprice`` and ``monthly_projection`` so
    the new "本月预计" stat card can render without a separate round
    trip to the dashboard.  ``monthly_projection`` is ``None`` when
    either ``eqprice`` or the daily average is missing (we never
    silently render ¥0 — the card shows "—" instead).
    """
    latest_row = db.latest()  # most recent F1 (records) row, may be None
    room_id = _current_room_id()
    run_status = db.get_run_status(room_id) if room_id else None
    scrape_status = db.get_meta("last_scrape_status") or "unknown"
    # Round 31 — feed _stats() a 7-day window so hourly_used and daily_avg
    # can be computed.  Before this change the route passed a single-row
    # list, which made both stats always None (they require len(valid)>=2)
    # and cascaded into monthly_projection=None.  See /api/data?hours=168
    # for the same shape (already correct there).
    stats_rows = query(168)
    stats = _stats(stats_rows)
    eqprice = _read_eqprice()
    monthly = _compute_monthly_projection(eqprice, stats, room_id)
    # latest_ts: prefer db.latest() so we surface the freshest row even
    # if the last scrape is older than 168h (defensive — shouldn't happen
    # in practice because the cron runs every 30 min).
    latest_ts = (
        latest_row["ts"] if latest_row
        else (stats_rows[-1]["ts"] if stats_rows else "")
    )
    # Round 32 — fall back for the same reason as _build_round2_context:
    # the school portal sometimes omits updateDt, leaving the DB row's
    # update_dt column NULL even when the meter has reported a fresh
    # reading.  Walk down update_dt → dt → read_time → ts so the JS
    # updateMeter() never paints "—" for an actively-reporting meter.
    live_update_dt = None
    if run_status is not None:
        live_update_dt = (
            run_status.get("update_dt")
            or run_status.get("dt")
            or (latest_row.get("read_time") if latest_row else None)
            or (latest_row.get("ts")        if latest_row else None)
        )
    return jsonify({
        "stats":  stats,
        "run_status": {
            "vol":    _round3(run_status.get("vol"))    if run_status else None,
            "cur":    _round3(run_status.get("cur"))    if run_status else None,
            "yggl":   _round3(run_status.get("yggl"))   if run_status else None,
            "run_status":  run_status.get("run_status") if run_status else None,
            "update_dt":   live_update_dt,
        } if run_status else None,
        "latest_ts": latest_ts,
        "scrape_status": scrape_status,
        "stale": scrape_status == "stale",
        # Round 26b — cost projection + unit price for the new stat card
        "eqprice": eqprice,
        "monthly_projection": monthly["monthly_projection"],
        "monthly_breakdown": monthly,
    })


@app.route("/api/refresh", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def api_refresh():
    """Force an immediate school scrape from the browser.

    Round 26b — landing page triggers this so users never wait up to
    30 minutes (cron cadence) for the dashboard to show fresh data.
    Internally calls :func:`dorm_power.fetch_once` which is a thin
    wrapper around :func:`run_once` with ``fetch_only=True``: same
    network work + DB persist, but every Feishu notification is
    suppressed.  This avoids spamming the user's group chat every
    time someone reloads the page.

    Round 34B — added ``_check_internal_token`` guard at the very top
    so Nginx-exposed POST /api/refresh requires an internal shared
    secret.  Without this, anyone reaching the public endpoint could
    force the school scrape (rate-limit bait + tiny DDoS surface).

    Returns:
      * HTTP 200 + ``{"ok": true, "scrape_status": ..., "ts": ...}``
        on a successful scrape.
      * HTTP 200 + ``{"ok": false, ...}`` only when the scrape ran
        but returned a non-ok ``scrape_status`` (e.g. school returned
        200 with stale data) — the dashboard should show a warning,
        not retry.
      * HTTP 401 + ``{"ok": false, "error": "X-Internal-Token invalid"}``
        when the request lacks / has a wrong X-Internal-Token header.
      * HTTP 500 + ``{"ok": false, "error": ...}`` when the scrape
        itself raised (no data was written).

    The error message is truncated to 200 chars so a verbose
    traceback can't blow past the response size budget; the full
    traceback is in the server log (logger.exception) for the
    operator to inspect.
    """
    _check_internal_token()
    try:
        from dorm_power import fetch_once
    except ImportError as exc:  # pragma: no cover — defensive
        logger.exception("dorm_power import failed: %r", exc)
        return jsonify({"ok": False, "error": "scraper module missing"}), 500

    try:
        result = fetch_once()
    except Exception as exc:  # noqa: BLE001
        logger.exception("force refresh failed: %r", exc)
        return jsonify({
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }), 500

    body = result.get("body") or {}
    scrape_status = result.get("scrape_status") or "unknown"
    return jsonify({
        "ok": scrape_status == "ok",
        "scrape_status": scrape_status,
        "ts": result.get("dt") or body.get("dt") or "",
        "remain": _round2(body.get("remainEq")),
    })


@app.route("/healthz")
def healthz():
    """Round 26a — deep health endpoint for uptimerobot / k8s probes.

    Probes three dependencies in parallel timeouts so a stuck one
    cannot wedge the whole endpoint:

      * **db**: opens the SQLite file and reads the
        ``last_scrape_status`` meta key.  Tells the operator whether
        the cron has ever landed a row.
      * **school_api**: light-weight GET ``/finduser`` (the same
        endpoint the scraper uses on cold-start).  5s timeout, so a
        flaky school cannot hang the health check.
      * **feishu_token**: optional.  When ``FEISHU_APP_ID`` /
        ``FEISHU_APP_SECRET`` are set, ask the auth endpoint for a
        tenant_access_token; report whether the call succeeded.

    Returns HTTP 200 when db_ok AND school_api_ok are both True,
    HTTP 503 otherwise.  ``last_scrape_age_sec`` is included so
    grafana can graph the recency.
    """
    checks: dict = {
        "db_ok": False,
        "school_api_ok": False,
        "feishu_token_ok": None,  # None = not configured
        "last_scrape_age_sec": None,
        "scrape_status": None,
    }

    # ---- db probe -----------------------------------------------------
    try:
        status = db.get_meta("last_scrape_status")
        checks["scrape_status"] = status
        last_ts_raw = db.get_meta("last_scrape_at")
        if last_ts_raw:
            ts = datetime.fromisoformat(last_ts_raw)
            # Round 47 — ``last_scrape_at`` is naive local (CST);
            # ``datetime.now()`` is also naive local, so the gap is on
            # the user's wall clock.  No tz tag needed.
            checks["last_scrape_age_sec"] = (
                datetime.now() - ts
            ).total_seconds()
        checks["db_ok"] = True
    except Exception as exc:  # noqa: BLE001
        checks["db_error"] = f"{type(exc).__name__}: {exc}"

    # ---- school API probe (5s budget) --------------------------------
    openid = (config.DORM_OPENID or "").strip()
    base = (config.DORM_BASE_URL or "").rstrip("/")
    if not openid or not base:
        # No credentials in the dashboard process — don't probe; the
        # scraper (which DOES have openid) will go red instead.
        checks["school_api_ok"] = True
        checks["school_api_skipped"] = "no DORM_OPENID in web process"
    else:
        url = (
            f"{base}/campus/webchat/dormEmRealRead/finduser"
            f"?openid={openid}"
        )
        t0 = time.time()
        try:
            r = _HEALTHZ_SESSION.get(url, timeout=5)
            checks["school_api_ok"] = r.status_code == 200
            checks["school_api_status"] = r.status_code
            checks["school_api_ms"] = int((time.time() - t0) * 1000)
        except Exception as exc:  # noqa: BLE001
            checks["school_api_ok"] = False
            checks["school_api_error"] = f"{type(exc).__name__}: {exc}"

    # ---- feishu token probe (optional, 5s budget) --------------------
    if config.FEISHU_APP_ID and config.FEISHU_APP_SECRET:
        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        try:
            r = _HEALTHZ_SESSION.post(
                token_url,
                json={
                    "app_id": config.FEISHU_APP_ID,
                    "app_secret": config.FEISHU_APP_SECRET,
                },
                timeout=5,
            )
            ok = r.status_code == 200 and (
                r.json().get("code", -1) == 0
            )
            checks["feishu_token_ok"] = ok
        except Exception as exc:  # noqa: BLE001
            checks["feishu_token_ok"] = False
            checks["feishu_token_error"] = f"{type(exc).__name__}: {exc}"

    # Decide HTTP status.  ``feishu_token_ok`` is optional, so we don't
    # fail the 503 on it.
    healthy = checks["db_ok"] and checks["school_api_ok"] is True
    return jsonify(checks), (200 if healthy else 503)


# ---------------------------------------------------------------------------
# Round 3 — Feishu bot (inbound events)
# ---------------------------------------------------------------------------

@app.route("/feishu/event", methods=["POST"])
def feishu_event_post():
    """Handle Feishu inbound events (plain OR encrypted).

    When the Feishu console has encryption enabled, every POST body is
    wrapped in `{"encrypt": "<base64-ciphertext>"}`.  We decrypt with
    FEISHU_ENCRYPT_KEY first, then look for the challenge.  When
    encryption is off, the body is plain JSON and we skip the
    decryption step.
    """
    # === Round 9 fix: read raw body FIRST ===
    # Previously this handler called `request.get_json(...)` (which consumes
    # the WSGI input stream) BEFORE the capture block, so the subsequent
    # `request.get_data(as_text=True)` returned "" and
    # /opt/dorm-power-monitor/feishu_capture.log never contained any body.
    # Now we grab the raw body up front, capture it (with the real bytes),
    # and then parse it ourselves.
    raw = request.get_data(as_text=True)

    # === Round 10: capture path moved off /tmp ===
    # Capture every incoming Feishu request verbatim (headers + body) BEFORE
    # any parsing/decryption so we can analyze the real ciphertext offline.
    # This helps diagnose "Padding is incorrect" errors when FEISHU_ENCRYPT_KEY
    # is wrong / the wrong algorithm is in use.
    #
    # Path choice: /tmp/feishu_capture.log was unreadable from the host shell
    # because the systemd unit has PrivateTmp=true (the service sees a
    # private tmpfs mount, while the host /tmp is a separate namespace — so
    # `cat /tmp/feishu_capture.log` from the operator's shell only ever
    # showed an empty stale file).  Writing inside the project directory
    # (which is whitelisted by the unit's ReadWritePaths=/opt/dorm-power-monitor)
    # keeps the file visible to `cat` / `tail` for live debugging.
    try:
        import datetime as _dt
        with open("/opt/dorm-power-monitor/feishu_capture.log", "a", encoding="utf-8") as _f:
            _f.write(f"=== {_dt.datetime.now().isoformat()} ===\n")
            _f.write(f"Headers: Content-Type={request.headers.get('Content-Type','')}\n")
            for _h in (
                "X-Lark-Request-Timestamp",
                "X-Lark-Request-Nonce",
                "X-Lark-Signature",
                "X-Request-Id",
            ):
                _v = request.headers.get(_h, "")
                if _v:
                    _f.write(f"  {_h}={_v}\n")
            _f.write(f"Body: {raw}\n")
            _f.write("\n")
    except Exception as _e:  # noqa: BLE001
        logger.error("feishu capture log failed: %s", _e)

    body = json.loads(raw) if raw else {}

    if feishu_bot.is_encrypted(body):
        try:
            body = feishu_bot.decrypt_payload(
                body["encrypt"],
                config.FEISHU_ENCRYPT_KEY,
            )
        except Exception as exc:  # noqa: BLE001
            # Log the failure but do not echo the (potentially sensitive)
            # ciphertext back.  Feishu doesn't care about the response
            # for encrypted events it can't decrypt on the server side;
            # it just retries the URL probe later.
            logger.error("feishu decrypt failed: %s: %s", type(exc).__name__, exc)
            return jsonify({"code": 500, "msg": "decrypt failed"}), 500
    challenge = body.get("challenge")
    if isinstance(challenge, str) and challenge:
        return jsonify({"challenge": challenge})
    room_id = db.get_meta("last_room_id")
    result = feishu_bot.handle_event(body, room_id)
    return jsonify(result)


@app.route("/feishu/event", methods=["GET"])
def feishu_event_get():
    """Feishu also probes with GET on URL save.  Mirror the same challenge response."""
    return jsonify({"challenge": request.args.get("challenge", "")})


# ---------------------------------------------------------------------------
# Round 63 — Session-cookie auth API.
# ---------------------------------------------------------------------------
#
# Three routes form the user-facing auth surface:
#   * POST /api/auth/login  { username, password }
#         → 200 { ok: true,  user: {...} } on success
#         → 401 { ok: false, error: "locked" } during lockout
#         → 401 { ok: false, error: "invalid_credentials" } on bad creds
#         → 400 { ok: false, error: "..." } on missing / malformed body
#   * POST /api/auth/logout
#         → 200 { ok: true }  (no-op if not logged in; revokes the
#                                current session row + clears the cookie)
#   * GET  /api/auth/me
#         → 200 { user: {...} } on success
#         → 401 { error: "not_authenticated" } otherwise
#
# Lockout + audit + session creation are all handled inside
# ``auth.attempt_login`` so web.py stays a thin HTTP wrapper.
# ---------------------------------------------------------------------------
@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    """Round 63 — login endpoint with lockout + audit + session creation."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        # Return a 400 so the frontend can distinguish "missing field"
        # from "bad credentials" (which is 401).  We deliberately do
        # NOT 401 here — that would mask programmer errors in the JS.
        return jsonify({
            "ok": False,
            "error": "username and password required",
        }), 400
    ip = auth.client_ip()
    ua = request.headers.get("User-Agent", "") or ""
    result = auth.attempt_login(username, password, ip=ip, user_agent=ua)
    if result.get("ok"):
        # Plant the session token in Flask's signed cookie.  The token
        # is the opaque secret; the cookie is its tamper-evident envelope.
        session[auth.SESSION_COOKIE_NAME] = result["token"]
        session.permanent = True
        # ``result`` carries the user payload but not the token itself
        # (so a leaked /api/auth/login response does not leak the
        # token if SameSite fails for some reason).  The cookie is the
        # only thing the client should use to authenticate.
        return jsonify({
            "ok": True,
            "user": result["user"],
        })
    # Failure path — lockout vs invalid_credentials.
    err = result.get("error", "invalid_credentials")
    return jsonify({"ok": False, "error": err}), 401


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    """Round 63 — revoke the current session + clear the cookie."""
    token = session.get(auth.SESSION_COOKIE_NAME)
    if token:
        auth.revoke_session(token)
    session.pop(auth.SESSION_COOKIE_NAME, None)
    return jsonify({"ok": True})


@app.route("/api/auth/me", methods=["GET"])
def api_auth_me():
    """Round 63 — return the current user or 401.

    Used by the front-end to decide whether to show the login form.
    The full User object minus ``password_hash`` is returned so the
    dashboard can label the role without an extra round-trip.

    Round 64 — also returns the CSRF token so the front-end can
    mirror it onto every subsequent ``fetch``.  The token is planted
    in a non-HttpOnly ``dorm_csrf`` cookie (via :func:`auth.csrf_cookie_token`)
    AND echoed in the JSON body so callers can choose either path
    (cookie read by JS, body read for the rare case the cookie was
    stripped by an intermediate proxy).
    """
    user = auth.get_current_user()
    if user is None:
        # Still plant a CSRF cookie so the login page can echo one
        # back without an extra round-trip.
        auth.csrf_cookie_token()
        return jsonify({"error": "not_authenticated"}), 401
    csrf = auth.csrf_cookie_token()
    return jsonify({
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "last_login_at": user.last_login_at,
        },
        "csrf_token": csrf,
    })


# ---------------------------------------------------------------------------
# Round 34B — Admin UI + OOBE + scrape / push config + URL import
# ---------------------------------------------------------------------------
#
# This block adds:
#   * ``_check_internal_token`` — shared-secret guard on /api/refresh so
#     the school scrape can't be triggered by anyone who hits the public
#     Nginx endpoint.
#   * ``_admin_required`` — HTTP Basic Auth decorator for every /admin/*
#     route.  Reads the password from meta (set during OOBE step 6) or
#     ADMIN_PASSWORD env var.  Falls open during OOBE so the user can
#     configure the system on first launch.
#   * ``_parse_school_url`` — accepts a WeChat openid-bearing finduser
#     URL, fetches the HTML, and extracts ``roomId`` / ``roomNo`` /
#     ``EqPrice`` from the hidden form inputs.  Used by both the OOBE
#     step 2 form and the dedicated /admin/api/import-url endpoint.
#   * OOBE wizard (6 steps) — covers school binding, Feishu webhook,
#     push schedule, test message, completion.
#   * /admin landing page — read/write scrape + push config + test push.
# ---------------------------------------------------------------------------

# Admin user is hard-coded "admin"; password is stored in meta (or env).
# We intentionally don't support multiple users — this is a single-tenant
# home dashboard, not a multi-tenant SaaS.
ADMIN_USER = "admin"


def _check_internal_token() -> None:
    """Round 34B — Nginx 反代后任何人 POST /api/refresh 都能触发抓取.
    用 X-Internal-Token header 校验，token 优先读 meta.api_internal_token,
    fallback env API_INTERNAL_TOKEN，再 fallback .env 默认值.
    失败返 401.

    当 token 未配置时（meta / env 都空），跳过校验 → 部署后立即配置.
    """
    expected = (
        db.get_meta("api_internal_token")
        or os.environ.get("API_INTERNAL_TOKEN", "")
        or ""
    )
    if not expected:
        # 未配置 = 无鉴权（部署后立即配）
        return
    provided = request.headers.get("X-Internal-Token", "")
    if not provided or not secrets.compare_digest(provided, expected):
        # Raise — Flask turns this into a 401 JSON response automatically.
        from flask import abort
        abort(401, description="X-Internal-Token invalid")


def _abort_401(msg: str):
    """Helper that returns a Flask (Response, status) tuple.

    Returning a JSON body with status 401 — same shape as the rest of the
    API surface so the dashboard's fetch() error path doesn't crash.
    """
    return jsonify({"ok": False, "error": msg}), 401


def _safe_url_for_log(url: str) -> str:
    """Strip the ``?openid=...`` query string from a URL before logging.

    The openid is a credential (see the security notes at the top of
    ``dorm_power.py``), so we never want to write it to the log.  Round
    37 — added because the new SSRF defenses in ``_parse_school_url``
    log the offending URL on rejection.
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        # Keep scheme + netloc + path; drop query + fragment.
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    except Exception:  # pragma: no cover — defensive
        return "<unparseable>"


def _admin_required(fn):
    """Decorator — HTTP Basic Auth gate for /admin/* routes.

    Password lookup order:
      1. meta.admin_password (set during OOBE step 6 or via the Admin UI).
      2. ADMIN_PASSWORD env var (fallback for headless deploys).

    When no password is configured AND OOBE is not yet completed, the
    request passes through unauthenticated so the user can finish the
    wizard.

    Round 37 B8 — once OOBE is done, an empty password USED to 401
    every /admin/* request, including ``/admin/api/admin-password``.
    That created a deadlock for users who SQL-skipped OOBE
    (``INSERT OR REPLACE meta(oobe_completed='1')``) without first
    setting a password — they could neither set one nor change config.
    Now we still 401 the data surface, but ``/admin/set-password`` is
    a separate route that intentionally bypasses this decorator so the
    user can bootstrap a password after a SQL skip.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        stored = db.get_meta("admin_password") or os.environ.get("ADMIN_PASSWORD", "")
        if not stored:
            if db.get_meta("oobe_completed") != "1":
                # OOBE 阶段允许通过
                return fn(*args, **kwargs)
            return _abort_401("Set admin password first")
        auth = request.authorization
        if not auth or auth.username != ADMIN_USER or auth.password != stored:
            resp = Response(
                "Auth required",
                401,
                {"WWW-Authenticate": 'Basic realm="dorm-power-monitor"'},
            )
            return resp
        return fn(*args, **kwargs)
    return wrapper


def _parse_school_url(url: str) -> dict:
    """Parse openid from query string, fetch HTML, extract roomId/roomNo/eqprice.

    Round 34B — used by both the OOBE step 2 form and the dedicated
    /admin/api/import-url endpoint.  Returns a dict so the caller can
    pick out the fields they want to persist to meta.

    Failure modes:
      * URL has no ``?openid=...``  → returns ``{"openid": None, ...}``.
        The caller decides whether to reject (the API route does).
      * network failure or non-200 → returns what we have so far; the
        caller can prompt the user to paste the missing pieces by hand.

    Round 37 B5 — SSRF protection.  The URL is fetched server-side via
    ``requests.get``, so a crafted ``school_url`` could otherwise point
    at ``http://127.0.0.1:6379/...`` or ``http://169.254.169.254/...``
    and exfiltrate local services.  Defenses:
      1. Host whitelist — must end in the configured school host
         (``config.DORM_BASE_URL``'s netloc).  School is a single-
         tenant deployment; any other host is an attack.
      2. Scheme allowlist — http or https only (no ``file://``, no
         ``gopher://``).
      3. IP literal rejection — when the netloc parses as an IP
         (v4 or v6), refuse loopback / link-local / private / cloud
         metadata addresses.  Belt-and-braces in case a future DNS
         rebinding bypass makes the host check pass while the actual
         socket connects somewhere internal.
      4. ``allow_redirects=False`` — the school never redirects, so any
         3xx response is suspicious.  Following the redirect would let
         the attacker route around our IP check by going through an
         open proxy.  Also covers Round 35 B11 (open-redirect).

    The hidden-input regex is forgiving about attribute order (id and
    value can swap) and quote style (' vs ").  We do not use a real HTML
    parser because the finduser page is server-rendered with consistent
    shapes we can match with a 2-line regex.
    """
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    openid = qs.get("openid", [None])[0]
    base_url = f"{parsed.scheme}://{parsed.netloc}"

    result: dict = {"openid": openid, "base_url": base_url}
    if not openid:
        return result

    # Round 37 B5 — host whitelist.  We compare on the lowercase
    # hostname suffix so a user pasting ``http://ybhqcz.fjny.edu.cn``
    # or a future alias (``http://www.ybhqcz.fjny.edu.cn``) both pass.
    # The configured school host is the single source of truth — if
    # the school moves, the operator updates ``DORM_BASE_URL`` and
    # both the scraper and the URL import stay in sync.
    try:
        school_host = urlparse(config.get_dorm_base_url()).hostname or ""
    except Exception:  # pragma: no cover — defensive
        school_host = urlparse(config.DORM_BASE_URL).hostname or ""
    school_host = school_host.lower()
    target_host = (parsed.hostname or "").lower()
    if not school_host or not target_host:
        logger.warning(
            "Round37 SSRF: empty host, url=%r", _safe_url_for_log(url),
        )
        return result
    if not (target_host == school_host or target_host.endswith("." + school_host)):
        logger.warning(
            "Round37 SSRF: host %r not in school whitelist %r",
            target_host, school_host,
        )
        return result

    # Round 37 B5 — IP-literal rejection.  Even after the host check
    # passes (school_host *is* a literal IP in some deployments), we
    # refuse loopback / link-local / private / metadata ranges.
    import ipaddress
    try:
        ip = ipaddress.ip_address(target_host)
    except ValueError:
        ip = None
    if ip is not None:
        # Single check: is it a private/loopback/multicast/reserved
        # address?  ``is_global`` is False for ALL of those plus a
        # few benign ones (e.g. TEST-NET ranges) — we err on the side
        # of refusing, since the school is reachable by hostname.
        if not ip.is_global:
            logger.warning(
                "Round37 SSRF: IP literal %r refused (not global)",
                target_host,
            )
            return result

    # Scheme allowlist (defensive — the parser already accepts almost
    # anything, but ``file://`` / ``gopher://`` etc. should never reach
    # requests.get).
    if parsed.scheme not in ("http", "https"):
        logger.warning(
            "Round37 SSRF: scheme %r refused", parsed.scheme,
        )
        return result

    try:
        resp = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 dorm-power-monitor"},
            allow_redirects=False,
        )
        # Round 37 B5 / B11 — refuse redirects outright.  The school
        # never redirects finduser; if we get a 3xx the URL was either
        # wrong or hostile.  This also closes the "open redirect via
        # compromised school page" angle.
        if resp.is_redirect or resp.status_code >= 300:
            logger.warning(
                "Round37 SSRF: redirect status %s from school URL refused",
                resp.status_code,
            )
            return result
        resp.raise_for_status()
        html = resp.text
        for field_id in ("roomId", "roomNo", "flag", "EqPrice"):
            m = re.search(
                rf'<input[^>]*\bid=["\']{field_id}["\'][^>]*\bvalue=["\']([^"\']+)["\']',
                html,
            )
            if not m:
                # Try the reverse attribute order: value before id.
                m = re.search(
                    rf'<input[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bid=["\']{field_id}["\']',
                    html,
                )
            if m:
                result[field_id] = m.group(1)
    except requests.RequestException:
        pass

    return result


# R62 — OOBE / admin templates also live in templates/ + static/.
OOBE_HTML = _load_template("oobe.html")



ADMIN_HTML = _load_template("admin.html")
# Round 64 — login page template.
LOGIN_HTML = _load_template("login.html")


# =========================================================================
# Round 65 — OOBE session state helpers + new wizard API.
#
# Background
# ==========
# The pre-R65 wizard used ``?step=N`` URL params + ``db.get_meta`` keys
# (``oobe_step`` / ``oobe_completed``).  That broke the moment the user
# refreshed — the URL reset to step=1 and the in-page form state was
# gone.  R65 moves wizard state into the signed Flask session:
#
#   session['oobe'] = {
#       'step':  <int 1..6>,
#       'data':  {
#           '1': {...},  # step 1 (welcome) — no fields
#           '2': {app_id, app_secret, verification_token, encrypt_key},
#           '3': {url, secret},
#           '4': {cron_expr, timezone},
#           '5': {records_days, monthly_backups, [skipped]},
#           '6': {username, password_hash}   # password is NOT stored in
#                                          # session plaintext; only the
#                                          # final /complete endpoint reads
#                                          # it from the request body.
#       },
#   }
#
#   session['oobe_complete'] = True   # set after step 6 succeeds
#
# Once ``oobe_complete`` is True, every ``/api/oobe/*`` route returns 403.
# The wizard state is then cleared from the session (``session.pop('oobe')``)
# so subsequent reloads stay short.
#
# URL params are ignored — the step is ALWAYS read from session, so
# bookmarking /oobe?step=5 has no effect when session.step == 1.
#
# Backward compatibility
# ----------------------
# - ``/admin/oobe`` (legacy R34B entry) now redirects to ``/oobe`` so
#   any older bookmark still works.
# - ``/admin/api/oobe/save`` (legacy R63 backend) is kept UNCHANGED —
#   R37 + R63 test suites still POST against it and expect specific
#   step-by-step behavior.  We do not delete or alter that route.
# - ``/`` redirect still consults ``db.get_meta('oobe_completed')`` AND
#   ``session['oobe_complete']``; the legacy meta key stays the source
#   of truth for the index-page redirect.
# =========================================================================

# Step 6 password strength: >= 8 chars, mixed case, digit, special char.
_PASSWORD_SPECIAL_CHARS = r"!@#$%^&*()_+\-=\[\]{};':\"\\|,.<>\/?~`"
_PASSWORD_SPECIAL_RE = re.compile(r"[" + _PASSWORD_SPECIAL_CHARS + r"]")


def _validate_password_strength(password):
    """Return None on OK, else an error message.

    The front-end (static/js/oobe.js#checkPasswordStrength) uses the same
    rules so the meter stays consistent with the server's rejection
    reasons.  This helper is deliberately local to web.py — auth.py is
    owned by R63 and is not to be modified in R65.
    """
    if not isinstance(password, str) or not password:
        return "password required"
    if len(password) < 8:
        return "password must be at least 8 characters"
    if len(password) > 128:
        return "password must be 128 characters or less"
    if not re.search(r"[a-z]", password):
        return "password must contain a lowercase letter"
    if not re.search(r"[A-Z]", password):
        return "password must contain an uppercase letter"
    if not re.search(r"[0-9]", password):
        return "password must contain a digit"
    if not _PASSWORD_SPECIAL_RE.search(password):
        return "password must contain a special character"
    return None


# Step 3 webhook URL whitelist: ``.tssplus.top`` + loopback only.
# Mirrors the SSRF defenses on ``_parse_school_url`` — the wizard is the
# only public-write surface for webhook URLs so the guard must be strict.
_WEBHOOK_HOST_ALLOWLIST = (
    "localhost",
    "127.0.0.1",
    "::1",
)


def _webhook_host_allowed(host):
    """Return True when ``host`` (already lowercased) is on the allowlist."""
    if not host:
        return False
    if host in _WEBHOOK_HOST_ALLOWLIST:
        return True
    if host.endswith(".tssplus.top"):
        return True
    return False


def _validate_webhook_url(url):
    """Return None on OK, else an error message.  SSRF guard."""
    if not isinstance(url, str) or not url.strip():
        return "webhook URL required"
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return "webhook URL must start with http:// or https://"
    try:
        parsed = urlparse(url)
    except Exception:
        return "webhook URL is not parseable"
    host = (parsed.hostname or "").lower()
    if not host:
        return "webhook URL must have a host"
    if not _webhook_host_allowed(host):
        return "webhook host must be .tssplus.top or localhost"
    return None


def _validate_cron_expr(expr):
    """Return None on OK, else an error message.  5-field crontab shape."""
    if not isinstance(expr, str) or not expr.strip():
        return "cron expression required"
    parts = expr.strip().split()
    if len(parts) != 5:
        return "cron must have exactly 5 fields (minute hour day month dow)"
    return None


def _oobe_state():
    """Return the in-progress wizard state stored in the Flask session.

    The returned dict is a copy — callers must use :func:`_oobe_set_state`
    to persist changes.  ``step`` defaults to 1; ``data`` defaults to
    ``{}``.  The shape is the one documented at the top of this section.
    """
    raw = session.get("oobe")
    if not isinstance(raw, dict):
        return {"step": 1, "data": {}}
    step = raw.get("step", 1)
    try:
        step = int(step)
    except (TypeError, ValueError):
        step = 1
    step = max(1, min(6, step))
    data = raw.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    return {"step": step, "data": data}


def _oobe_set_state(state):
    """Persist a new wizard state dict to the Flask session."""
    if not isinstance(state, dict):
        state = {"step": 1, "data": {}}
    session["oobe"] = state
    session.permanent = True


def _oobe_completed():
    """Return True when the wizard is finished.

    Sources of truth (any one is enough):
      * ``session['oobe_complete']`` — set by ``/api/oobe/complete``.
      * ``db.get_meta('oobe_completed') == '1'`` — pre-R65 / SQL-skip.
      * ``auth.count_users() > 0`` — covers the case where the operator
        ran ``ensure_initial_admin()`` directly without touching the
        ``oobe_completed`` meta key.

    All three are consulted because the session flag can be lost when
    the cookie expires (default 24 h) but the DB row outlives that.
    """
    if session.get("oobe_complete"):
        return True
    try:
        if db.get_meta("oobe_completed") == "1":
            return True
        if auth.count_users() > 0:
            return True
    except Exception:  # pragma: no cover — defensive
        return False
    return False


def _oobe_validate_step(step, step_data):
    """Server-side validation of the data bucket for ``step``.

    Returns None on OK or an error message string.  Only steps with
    fields are checked (steps 1 / 6 use different endpoints).
    """
    if step == 2:
        if not (step_data or {}).get("app_id"):
            return "app_id required"
        if not (step_data or {}).get("app_secret"):
            return "app_secret required"
        return None
    if step == 3:
        return _validate_webhook_url((step_data or {}).get("url", ""))
    if step == 4:
        return _validate_cron_expr((step_data or {}).get("cron_expr", ""))
    if step == 5:
        if step_data.get("skipped"):
            return None  # user explicitly chose defaults
        days = step_data.get("records_days")
        if not isinstance(days, int) or days < 5:
            return "records retention days must be an integer >= 5"
        return None
    return None


# ---- R65 new wizard GET + JSON API endpoints -------------------

@app.route("/oobe")
def oobe_wizard():
    """R65 — render the OOBE wizard at the user's current session step.

    Step state lives in ``session['oobe']`` (Flask signed cookie).  Once
    the wizard is finished (session flag / db meta / users exist) we
    redirect to ``/admin``.  URL params are ignored on purpose so a
    user can't bookmark ``/oobe?step=5`` to skip ahead.
    """
    if _oobe_completed():
        return redirect("/admin")
    state = _oobe_state()
    step = state["step"]
    return render_template_string(OOBE_HTML, step=step)


@app.route("/admin/oobe")
def oobe_wizard_legacy():
    """R65 — back-compat alias; redirects to the new ``/oobe`` entry.

    Pre-R65 bookmarks / dashboard redirects may still point here.  The
    actual wizard state now lives in the Flask session, so we just
    bounce to the canonical path.
    """
    return redirect("/oobe")


@app.route("/api/oobe/save-state", methods=["POST"])
def oobe_save_state():
    """R65 — persist one form's data to the session (no advance).

    Body: ``{"step": <int>, "data": {<field>: <value>}}``

    The data bucket is merged into ``session['oobe']['data'][step]``.
    Returns ``{"ok": true}`` on success, 403 once ``oobe_complete`` is
    set, 400 on bad input.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        step = int(payload.get("step", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "step must be an integer"}), 400
    if not (1 <= step <= 6):
        return jsonify({"ok": False, "error": "step out of range"}), 400
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "data must be an object"}), 400
    state = _oobe_state()
    bucket = state["data"].setdefault(str(step), {})
    if not isinstance(bucket, dict):
        bucket = {}
        state["data"][str(step)] = bucket
    bucket.update({k: v for k, v in data.items()})
    _oobe_set_state(state)
    return jsonify({"ok": True})


@app.route("/api/oobe/next", methods=["POST"])
def oobe_next():
    """R65 — validate current step, advance ``session['oobe']['step']``.

    Body: ``{"step": <int>}``

    Re-runs :func:`_oobe_validate_step` against the saved bucket so a
    client that skipped client-side validation cannot bypass the gate.
    On success the session step is bumped to ``step + 1`` (capped at 6).
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        step = int(payload.get("step", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "step must be an integer"}), 400
    if not (1 <= step <= 6):
        return jsonify({"ok": False, "error": "step out of range"}), 400
    state = _oobe_state()
    bucket = (state.get("data") or {}).get(str(step), {}) or {}
    err = _oobe_validate_step(step, bucket)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    new_step = step + 1 if step < 6 else 6
    state["step"] = new_step
    _oobe_set_state(state)
    return jsonify({"ok": True, "step": new_step})


@app.route("/api/oobe/prev", methods=["POST"])
def oobe_prev():
    """R65 — go back one step in the session.

    Body: ``{"step": <int>}`` — the client tells us where it is now;
    we set ``session['o']['step'] = max(1, step - 1)``.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        step = int(payload.get("step", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "step must be an integer"}), 400
    new_step = max(1, step - 1)
    state = _oobe_state()
    state["step"] = new_step
    _oobe_set_state(state)
    return jsonify({"ok": True, "step": new_step})


@app.route("/api/oobe/skip-step", methods=["POST"])
def oobe_skip_step():
    """R65 — mark a step as skipped (uses defaults) and advance.

    Currently only step 5 (data retention) is skippable — every other
    step has required fields and must be validated explicitly.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    try:
        step = int(payload.get("step", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "step must be an integer"}), 400
    if step != 5:
        return jsonify({"ok": False, "error": "step not skippable"}), 400
    state = _oobe_state()
    bucket = state["data"].setdefault("5", {})
    bucket["skipped"] = True
    bucket.setdefault("records_days", 90)
    bucket.setdefault("monthly_backups", 12)
    state["step"] = 6
    _oobe_set_state(state)
    return jsonify({"ok": True, "step": 6, "skipped": True})


@app.route("/api/oobe/validate-feishu", methods=["POST"])
def oobe_validate_feishu():
    """R65 — test Feishu credentials by requesting tenant_access_token.

    Body: ``{"app_id": "...", "app_secret": "...", "verification_token":
    "...", "encrypt_key": "..."}``.  We POST only app_id/app_secret to
    the official Feishu auth endpoint — verification_token / encrypt_key
    are NOT sent (they're used for inbound event verification, not the
    app-level auth).  Returns ``{"ok": true, "valid": true,
    "bot_name": "..."}`` or 400 with an error.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    app_id = (payload.get("app_id") or "").strip()
    app_secret = (payload.get("app_secret") or "").strip()
    if not app_id or not app_secret:
        return jsonify({
            "ok": False, "valid": False,
            "error": "app_id and app_secret required",
        }), 400
    token_url = (
        "https://open.feishu.cn/open-apis/auth/v3/"
        "tenant_access_token/internal"
    )
    try:
        resp = requests.post(
            token_url,
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=5,
        )
    except Exception as exc:  # noqa: BLE001 — any network error
        return jsonify({
            "ok": False, "valid": False,
            "error": f"network: {type(exc).__name__}: {exc}",
        }), 400
    if resp.status_code != 200:
        return jsonify({
            "ok": False, "valid": False,
            "error": f"HTTP {resp.status_code}",
        }), 400
    try:
        body = resp.json()
    except ValueError:
        return jsonify({
            "ok": False, "valid": False,
            "error": "non-JSON response from feishu",
        }), 400
    if body.get("code") != 0:
        return jsonify({
            "ok": False, "valid": False,
            "error": body.get("msg") or "invalid credentials",
        }), 400
    token = body.get("tenant_access_token") or ""
    return jsonify({
        "ok": True, "valid": True,
        "bot_name": body.get("bot_name") or "ok",
        # First 8 chars of the token so the UI can show "got a token,
        # looks like cli_xxx..." without revealing the whole secret.
        "tenant_access_token_prefix": token[:8] + ("..." if len(token) > 8 else ""),
    })


@app.route("/api/oobe/validate-webhook", methods=["POST"])
def oobe_validate_webhook():
    """R65 — POST a test payload to the user's webhook URL.

    Body: ``{"url": "...", "secret": "..."}``.  When ``secret`` is
    provided we sign the payload with the same Feishu HMAC-SHA256
    scheme the inbound bot uses (``X-Lark-Signature`` header), so the
    test exercises the full crypto path.  Returns ``{"ok": true,
    "valid": true}`` or 400 with an error string.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    secret = (payload.get("secret") or "").strip()
    err = _validate_webhook_url(url)
    if err:
        return jsonify({"ok": False, "valid": False, "error": err}), 400
    ts = str(int(time.time()))
    body_obj = {
        "timestamp": ts,
        "msg_type": "text",
        "content": {"text": "dorm-power-monitor OOBE \u6d4b\u8bd5\u63a8\u9001"},
    }
    body_bytes = json.dumps(
        body_obj, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if secret:
        string_to_sign = "{}\n{}\n{}".format(ts, secret, body_bytes.decode("utf-8"))
        digest = hmac.new(
            string_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).digest()
        headers["X-Lark-Signature"] = base64.b64encode(digest).decode("utf-8")
        headers["X-Lark-Request-Timestamp"] = ts
    try:
        resp = requests.post(url, data=body_bytes, headers=headers, timeout=5)
    except Exception as exc:  # noqa: BLE001
        return jsonify({
            "ok": False, "valid": False,
            "error": f"network: {type(exc).__name__}: {exc}",
        }), 400
    if resp.status_code != 200:
        # Truncate the body so a long HTML error page doesn't bloat the
        # JSON toast the front-end renders.
        snippet = (resp.text or "")[:200]
        return jsonify({
            "ok": False, "valid": False,
            "error": f"HTTP {resp.status_code}: {snippet}",
        }), 400
    return jsonify({"ok": True, "valid": True})


@app.route("/api/oobe/complete", methods=["POST"])
def oobe_complete():
    """R65 — finalize OOBE: validate + create admin + auto-login + flag.

    Body: ``{"username": "...", "password": "...",
    "password_confirm": "..."}``.

    Behaviour:
      * Validate username (alphanumerics + ``_.-``, 2-32 chars) and
        password (length + complexity).
      * If the ``users`` table is already non-empty (SQL skip / earlier
        ``ensure_initial_admin`` seed) we mark ``oobe_complete`` but
        skip user creation — the existing user must log in via
        ``/login`` to receive a session.
      * Otherwise call :func:`auth.create_user` with role='admin' and
        auto-login via :func:`auth.create_session` + a session cookie.
      * Both branches write ``session['oobe_complete'] = True`` AND
        ``db.set_meta('oobe_completed', '1')`` so the ``GET /`` redirect
        stops firing.

    Audit: writes an ``oobe.complete`` row (or ``oobe.complete_failed``
    on failure) with the IP + UA so the operator's audit dashboard
    surfaces who finished setup.
    """
    if _oobe_completed():
        return jsonify({"ok": False, "error": "oobe_completed"}), 403
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    password_confirm = payload.get("password_confirm") or ""
    if not username:
        return jsonify({"ok": False, "error": "username required"}), 400
    if not re.match(r"^[A-Za-z0-9_.\-]{2,32}$", username):
        return jsonify({
            "ok": False,
            "error": "username must be 2-32 chars [A-Za-z0-9_.-]",
        }), 400
    pw_err = _validate_password_strength(password)
    if pw_err:
        return jsonify({"ok": False, "error": pw_err}), 400
    if password != password_confirm:
        return jsonify({
            "ok": False, "error": "password confirm mismatch",
        }), 400

    ip = auth.client_ip()
    ua = request.headers.get("User-Agent", "") or ""

    # Branch 1 — users table not empty (SQL skip / earlier seed).
    # Don't try to create a duplicate user; just mark complete and
    # tell the front-end to redirect to /login.
    if auth.count_users() > 0:
        session["oobe_complete"] = True
        session.pop("oobe", None)
        session.permanent = True
        db.set_meta("oobe_completed", "1")
        auth.write_audit(
            "oobe.complete",
            target=username,
            ip=ip, user_agent=ua,
            details={"skipped_create": True, "reason": "users table not empty"},
        )
        return jsonify({
            "ok": True,
            "redirect": "/login",
            "skipped_create": True,
        })

    # Branch 2 — fresh install: create + auto-login.
    try:
        new_user = auth.create_user(
            username=username, password=password, role=auth.ROLE_ADMIN,
        )
    except Exception as exc:  # noqa: BLE001 — likely IntegrityError
        auth.write_audit(
            "oobe.complete_failed",
            target=username,
            ip=ip, user_agent=ua,
            details={"error": type(exc).__name__, "message": str(exc)[:120]},
        )
        return jsonify({
            "ok": False,
            "error": f"create user: {type(exc).__name__}",
        }), 400

    token = auth.create_session(new_user.id, ip=ip, user_agent=ua)
    auth.mark_user_logged_in(new_user.id)
    auth.write_audit(
        "oobe.complete",
        user_id=new_user.id,
        target=username,
        ip=ip, user_agent=ua,
        details={"role": new_user.role, "skipped_create": False},
    )
    session[auth.SESSION_COOKIE_NAME] = token
    session["oobe_complete"] = True
    session.pop("oobe", None)
    session.permanent = True
    db.set_meta("oobe_completed", "1")
    return jsonify({
        "ok": True,
        "redirect": "/admin",
        "skipped_create": False,
    })


# ---- Legacy R34B / R37 / R63 OOBE backend (UNCHANGED) ----------
# R37 + R63 test suites POST against ``/admin/api/oobe/save`` with
# specific step payloads (step 2 missing roomId → 400, step 5 must
# NOT call _post_feishu, etc.) so this route stays exactly as it was
# before R65.  New wizard clients use the ``/api/oobe/*`` endpoints
# above; the legacy endpoint is the back-compat path for any older
# scripts / curl one-shots.

@app.route("/admin/api/oobe/save", methods=["POST"])
@_admin_required
def oobe_save():
    """Round 34B + Round 37 — save current OOB step, advance to next.

    Each step persists different keys to meta (see the per-step
    branches).  On success we increment ``oobe_step``; on step 6 we set
    ``oobe_completed=1`` so the next ``GET /`` no longer redirects.

    Round 37 changes:
      * B3 — step 5 no longer fires the webhook here.  The user now
        clicks "发送测试消息" to hit ``/admin/api/test-push`` (which has
        its own success/error toast).  This branch just advances the
        wizard like every other step.
      * B4 — step 2 / step 3 reject bad input with a 400 instead of
        silently advancing with an empty meta.  The frontend already
        toasts ``json.error`` on ``!json.ok`` so the user sees why the
        "下一步" button did nothing.
    """
    data = request.get_json(silent=True) or {}
    try:
        step = int(data.get("step", "0"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "step must be an integer"}), 400
    if not (1 <= step <= 6):
        return jsonify({"ok": False, "error": "step out of range"}), 400
    payload = data.get("data") or {}

    if step == 2:
        url = (payload.get("school_url") or "").strip()
        if not url:
            return jsonify({"ok": False,
                            "error": "URL 不能为空"}), 400
        parsed = _parse_school_url(url)
        # Round 37 B4 — reject when the URL doesn't yield usable
        # school config.  An empty openid means the user pasted the
        # wrong URL; a missing roomId means the finduser HTML didn't
        # load (network, expired openid, or flag=0).  Either way the
        # wizard must NOT advance — otherwise the user leaves step 2
        # with no room_id and the scraper falls back to discovery
        # every cycle.
        if not parsed.get("openid"):
            return jsonify({"ok": False,
                            "error": "URL 必须含 ?openid=..."}), 400
        if not parsed.get("roomId"):
            return jsonify({"ok": False,
                            "error": ("HTML 没找到 roomId（openid 可能过期或 "
                                      "flag=0 未绑定）")}), 400
        db.set_meta("dorm_openid", parsed["openid"])
        if parsed.get("base_url"):
            db.set_meta("dorm_base_url", parsed["base_url"])
        # Round 35 — write to ``last_room_id`` so the scraper picks
        # it up on the next cycle (it reads from there, not the
        # ``dorm_room_id`` key).
        db.set_meta("last_room_id", parsed["roomId"])
        if parsed.get("EqPrice"):
            db.set_meta("eqprice", parsed["EqPrice"])
    elif step == 3:
        webhook = (payload.get("webhook_url") or "").strip()
        # Round 37 B4 — empty webhook used to silently advance.  Now
        # reject so the user notices they forgot to paste the URL.
        if not webhook:
            return jsonify({"ok": False,
                            "error": "Webhook URL 不能为空"}), 400
        db.set_meta("feishu_webhook_url", webhook)
    elif step == 4:
        for k in (
            "l2_enable", "daily_enable", "weekly_enable", "monthly_enable",
            "quiet_hours_start", "quiet_hours_end",
        ):
            if k in payload:
                db.set_meta(f"push_{k}" if not k.startswith("push_") else k,
                            str(payload[k]))
    elif step == 5:
        # Round 37 B3 — step 5 used to unconditionally fire the
        # webhook on "下一步".  That made the test push impossible to
        # skip (annoying for users who just want to advance) and also
        # silently swallowed failures because `_post_feishu` defaulted
        # to log-and-swallow.  Now: step 5 advances the wizard like
        # every other step; the actual push happens when the user
        # clicks "发送测试消息" → POST /admin/api/test-push.
        pass
    elif step == 6:
        # Round 63 — step 6 now ALSO seeds the admin user so the
        # dashboard has a real auth subject to log in as.  The wizard's
        # JS in this round still posts an empty payload for step 6
        # (the login-UI rewrite is R64's job), so we tolerate both
        # shapes:
        #
        #   * payload carries ``admin_username`` + ``admin_password`` +
        #     ``admin_password_confirm`` → create the user, auto-login,
        #     and return the session token so the front-end can plant
        #     the cookie without an extra round-trip.
        #
        #   * payload is empty (current OOBE JS) → just mark OOBE
        #     complete.  The ``auth.ensure_initial_admin()`` bootstrap
        #     at module-import time already seeded an admin from
        #     ``AUTH_INITIAL_ADMIN_PASSWORD`` when one was configured,
        #     so this branch is correct for .env-driven deployments.
        admin_username = (payload.get("admin_username") or "").strip()
        admin_password = payload.get("admin_password") or ""
        admin_password_confirm = (
            payload.get("admin_password_confirm") or ""
        )
        if admin_username or admin_password or admin_password_confirm:
            if not admin_username:
                return jsonify({
                    "ok": False,
                    "error": "admin username required",
                }), 400
            if not admin_password:
                return jsonify({
                    "ok": False,
                    "error": "admin password required",
                }), 400
            if admin_password != admin_password_confirm:
                return jsonify({
                    "ok": False,
                    "error": "admin password confirm mismatch",
                }), 400
            try:
                new_user = auth.create_user(
                    username=admin_username,
                    password=admin_password,
                    role=auth.ROLE_ADMIN,
                )
            except Exception as exc:  # noqa: BLE001
                # Likely IntegrityError (username UNIQUE) — surface a
                # 400 so the JS can show "用户名已存在" or similar.
                auth.write_audit(
                    "oobe_admin_create_failed",
                    target=admin_username,
                    ip=auth.client_ip(),
                    user_agent=request.headers.get("User-Agent", ""),
                    details={"error": type(exc).__name__},
                )
                return jsonify({
                    "ok": False,
                    "error": f"create user: {type(exc).__name__}",
                }), 400
            # Auto-login the freshly created admin so the next page load
            # lands inside /admin instead of the login screen.
            ip = auth.client_ip()
            ua = request.headers.get("User-Agent", "") or ""
            token = auth.create_session(new_user.id, ip=ip, user_agent=ua)
            auth.mark_user_logged_in(new_user.id)
            auth.write_audit(
                "oobe_admin_create",
                user_id=new_user.id,
                target=admin_username,
                ip=ip, user_agent=ua,
                details={"role": new_user.role},
            )
            session[auth.SESSION_COOKIE_NAME] = token
            session.permanent = True
        db.set_meta("oobe_completed", "1")

    next_step = "done" if step >= 6 else str(step + 1)
    db.set_meta("oobe_step", str(6 if next_step == "done" else step + 1))
    return jsonify({"ok": True, "next_step": next_step})


@app.route("/admin")
@require_auth(role='admin')
def admin_index():
    """Round 64 — admin landing redirect.

    Pre-R64 this rendered the single ``admin.html`` template that
    combined scrape / push / password / test on one page.  R64 splits
    that surface into 5 dedicated templates + 5 JSON APIs; we keep
    ``/admin`` as the URL entry point and 302-redirect to the config
    page so existing bookmarks still work.
    """
    return redirect("/admin/config")


@app.route("/admin/api/scrape/config", methods=["GET", "POST"])
@require_auth(role='admin')
@require_csrf
def admin_scrape_config():
    """GET / POST — read/write the school-scraping knobs."""
    if request.method == "GET":
        return jsonify({
            "dorm_base_url": db.get_meta("dorm_base_url") or config.DORM_BASE_URL,
            "dorm_openid": db.get_meta("dorm_openid") or config.DORM_OPENID,
            # Round 35 — read the key the scraper actually writes
            # (``last_room_id``).  The legacy ``dorm_room_id`` meta key
            # was only ever written from the admin form; the scraper
            # never read it, so the field used to come back blank and
            # the scraper kept falling through to ``_discover_room``.
            "dorm_room_id": db.get_meta("last_room_id") or "",
            "eqprice": db.get_meta("eqprice") or "0.5",
            "feishu_webhook_url": (
                db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK
            ),
        })
    data = request.get_json(silent=True) or {}
    for k in (
        "dorm_base_url", "dorm_openid", "dorm_room_id",
        "eqprice", "feishu_webhook_url",
    ):
        if k in data:
            # Round 35 — remap the admin form field ``dorm_room_id`` to
            # the meta key the scraper reads from (``last_room_id``).
            # Keeping the form field name stable so the existing HTML
            # form JS keeps working without change.
            meta_key = "last_room_id" if k == "dorm_room_id" else k
            db.set_meta(meta_key, str(data[k]))
    return jsonify({"ok": True})


@app.route("/admin/api/push/config", methods=["GET", "POST"])
@require_auth(role='admin')
@require_csrf
def admin_push_config():
    """GET / POST — read/write the push-schedule knobs."""
    if request.method == "GET":
        keys = (
            "push_l1_enable", "push_l2_enable",
            "push_daily_enable", "push_weekly_enable", "push_monthly_enable",
            "push_daily_time", "push_weekly_time", "push_monthly_time",
            "quiet_hours_start", "quiet_hours_end",
        )
        defaults = {
            "push_l1_enable": "0",
            "push_l2_enable": "1",
            "push_daily_enable": "1",
            "push_weekly_enable": "1",
            "push_monthly_enable": "1",
            "push_daily_time": "09:00",
            "push_weekly_time": "09:00",
            "push_monthly_time": "09:00",
            "quiet_hours_start": "23:00",
            "quiet_hours_end": "07:00",
        }
        out = {k: db.get_meta(k) or defaults[k] for k in keys}
        return jsonify(out)
    data = request.get_json(silent=True) or {}
    for k, v in data.items():
        db.set_meta(k, str(v))
    return jsonify({"ok": True})


@app.route("/admin/api/test-push", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def admin_test_push():
    """Fire a one-off test message to the currently-configured webhook.

    Round 37 B2 — passes ``raise_on_error=True`` to ``_post_feishu`` so a
    4xx / 5xx / connection error from the Feishu side bubbles up here
    and we can toast the actual error string to the user.  Before this
    fix, ``_post_feishu`` always swallowed ``RequestException`` and the
    OOBE step 5 button would always show "已发送 ✓" even when the
    webhook was wrong (404 / invalid signature / typo'd URL).
    """
    try:
        from dorm_power import _post_feishu
    except ImportError as exc:  # pragma: no cover — defensive
        return jsonify({"ok": False, "error": f"import: {exc}"}), 500
    webhook = (
        db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK or ""
    )
    if not webhook:
        # No webhook configured — return ok with a notice so the UI can
        # still proceed (the test "succeeds" by reaching the empty path).
        return jsonify({"ok": True, "notice": "no webhook configured"})
    _orig = config.FEISHU_WEBHOOK
    config.FEISHU_WEBHOOK = webhook
    try:
        # Round 37 — raise_on_error=True so a failed POST surfaces to
        # the user instead of silently logging + reporting success.
        _post_feishu(
            {
                "msg_type": "text",
                "content": {"text": "dorm-power-monitor 测试推送"},
            },
            raise_on_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        config.FEISHU_WEBHOOK = _orig
        return jsonify({"ok": False, "error": str(exc)}), 500
    config.FEISHU_WEBHOOK = _orig
    return jsonify({"ok": True})


@app.route("/admin/api/admin-password", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def admin_set_password():
    """Round 34B — set the admin password (stored in meta as plain text).

    We deliberately use plain text rather than bcrypt for two reasons:
      1. The password protects only the /admin/* surface, not the data
         plane.  /api/data, /api/live, /feishu/event are unprotected
         on purpose (Feishu needs to reach /feishu/event, and the data
         feed is read-only scrapes with no PII beyond the room number).
      2. Bcrypt requires a non-trivial dependency for a single-tenant
         home dashboard.  Not worth the maintenance cost.

    The password travels over HTTPS only — the X-Internal-Token /
    Nginx pair ensures the Basic Auth header never leaves TLS.
    """
    data = request.get_json(silent=True) or {}
    pwd = (data.get("admin_password") or "").strip()
    if not pwd:
        return jsonify({"ok": False, "error": "password empty"}), 400
    db.set_meta("admin_password", pwd)
    return jsonify({"ok": True})


@app.route("/admin/set-password", methods=["GET", "POST"])
def admin_set_password_recovery():
    """Round 37 B8 — password-recovery route for the SQL-skip deadlock.

    Background:
      A user who SQL-skipped OOBE (``INSERT OR REPLACE meta(oobe_completed='1')``)
      without first setting a password could never reach the Admin UI
      to set one — ``/admin`` and ``/admin/api/admin-password`` both go
      through ``_admin_required`` which 401s when ``oobe_completed='1'``
      AND no password is configured.  Classic deadlock.

    This route intentionally does NOT use ``_admin_required``.  It is
    only reachable when:
      * OOBE is already completed (``oobe_completed == '1'``), AND
      * no admin password is configured yet (meta + ADMIN_PASSWORD env).

    Once the operator has set a password via this route, the regular
    ``_admin_required`` gate kicks back in (the
    ``admin_password is now set`` branch in the decorator).

    POST body: ``{"admin_password": "..."}``
    GET response: a tiny HTML form so a browser session can recover
    without writing curl by hand.  No JS, no CSS — operator-facing
    recovery page only.

    Security notes:
      * The page is only useful on first deployment (no password yet)
        so the "anyone can reach this" window is small.
      * Nginx is expected to expose /admin on HTTPS only; in this
        transient state we accept the trade-off because the
        alternative is "user can never configure the system".
      * Once a password is set, this route still accepts POST (so an
        operator can rotate the password via this page too) but the
        old password is NOT checked — set a new one and you're in.
        That matches the existing ``/admin/api/admin-password`` POST
        behavior (also doesn't verify the old password).
    """
    if db.get_meta("oobe_completed") != "1":
        # Normal users should never see this — they go through the OOBE
        # wizard which sets the password on step 6.  Redirect home so
        # the wizard isn't accidentally bypassed.
        return redirect("/")
    stored = db.get_meta("admin_password") or os.environ.get("ADMIN_PASSWORD", "")
    if stored and request.method == "GET":
        # Already configured — the regular /admin surface is the right
        # place to change the password from now on.  Send the user
        # there; if their password is wrong, the basic-auth prompt
        # will catch it.
        return redirect("/admin")

    if request.method == "GET":
        # Minimal recovery form.  No branding — this is a "you've
        # SQL-skipped past the wizard, set a password to continue"
        # page, intentionally ugly so it's obvious it's a fallback.
        return Response(
            """<!doctype html>
<html lang=zh><head>
<meta charset=utf-8>
<title>设置 Admin 密码 — dorm-power-monitor</title>
<style>
body {
  font-family: -apple-system, "PingFang SC", sans-serif;
  background: #0f172a; color: #f8fafc;
  display: flex; align-items: center; justify-content: center;
  min-height: 100vh; margin: 0;
}
form {
  background: rgba(30,41,59,.8); padding: 32px; border-radius: 14px;
  border: 1px solid rgba(96,165,250,.3); width: 360px;
}
h1 { font-size: 18px; margin: 0 0 18px; }
input[type=password] {
  width: 100%; padding: 12px 14px; border-radius: 10px;
  border: 1px solid #334155; background: #0f172a; color: #f8fafc;
  font-size: 14px; margin-bottom: 12px;
}
button {
  padding: 10px 18px; border-radius: 10px; border: none;
  background: linear-gradient(120deg, #60a5fa, #6366f1);
  color: white; font-size: 14px; font-weight: 600; cursor: pointer;
}
small { color: #94a3b8; display: block; margin-top: 12px; }
</style>
</head><body>
<form method=post>
  <h1>设置 Admin 密码</h1>
  <p style="color:#94a3b8;font-size:13px;">OOBE 已通过 SQL 标记为完成，但 admin_password 尚未设置。设置密码后才能进入 /admin。</p>
  <input type=password name=admin_password placeholder="新密码" required>
  <button type=submit>设置并进入 /admin</button>
  <small>Round 37 B8 — 该路由刻意不走 admin 认证，方便 SQL skip 后第一次设密码。</small>
</form>
</body></html>""",
            mimetype="text/html",
        )

    # POST — accept JSON or form-encoded so both curl and the HTML
    # form above work.
    data = request.get_json(silent=True) or {}
    if not data:
        # Fall back to form-encoded (the recovery page uses that).
        data = request.form.to_dict() if request.form else {}
    pwd = (data.get("admin_password") or "").strip()
    if not pwd:
        return jsonify({"ok": False, "error": "password empty"}), 400
    db.set_meta("admin_password", pwd)
    # Round 37 B8 — after setting a password, redirect back to /admin
    # so the browser session can authenticate normally.  The basic-
    # auth prompt will fire because we did NOT send a header here.
    return redirect("/admin")


@app.route("/admin/api/import-url", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def admin_import_url():
    """Round 34B — parse a WeChat openid URL into school config values.

    POST body: ``{"url": "http://.../finduser?openid=..."}``

    Validates that the path looks like a finduser shell page, then
    delegates to :func:`_parse_school_url` to fetch the HTML and
    extract ``roomId`` / ``roomNo`` / ``EqPrice``.

    Returns the parsed dict on success, or a JSON error on failure.
    """
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL 不能为空"}), 400
    if not re.search(r"/dormEm\w*/finduser", url):
        return jsonify({"error": "URL 必须是 .../dormEm*/finduser 格式"}), 400

    parsed = _parse_school_url(url)
    if not parsed.get("openid"):
        return jsonify({"error": "URL 必须含 ?openid=..."}), 400
    if not parsed.get("roomId"):
        return jsonify({"error": "HTML 没找到 roomId（openid 可能过期或 flag=0 未绑定）"}), 400

    return jsonify(parsed)


# ---------------------------------------------------------------------------
# Round 64 — Login UI (HTML form).
#
# Background
# ==========
# Pre-R64 the dashboard's "admin" surface required HTTP Basic Auth
# (legacy ``_admin_required`` decorator on /admin/api/oobe/save and
# /admin/api/import-url).  R63 added session-cookie auth but kept the
# same form-based flow for OOBE; the cookie-auth login screen lived
# only as an AJAX endpoint (``POST /api/auth/login``).  R64 ships a
# proper HTML login page so a user who is not logged in lands on it
# instead of a JSON 401.
#
# Flow
# ----
#   * GET /login        → renders templates/login.html.  If the user
#                         is ALREADY logged in (cookie valid) we
#                         302 to /admin so they don't re-enter creds.
#   * POST /login       → legacy form-encoded fallback (the actual
#                         submit uses AJAX so the lockout toast can
#                         appear inline).  On success we plant the
#                         session cookie and redirect to /admin.
# ---------------------------------------------------------------------------
@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Round 64 — HTML login form + form-encoded POST fallback.

    The JavaScript at ``static/js/login.js`` submits via AJAX so the
    inline error toast can surface lockout / invalid-credential
    messages.  This route handles the fallback path (no JS, curl,
    weird browsers) by reading form-encoded ``username`` + ``password``
    from the POST body.

    On every GET we mint a fresh CSRF cookie (via the
    ``csrf_token`` context processor) so the JS can echo it back.
    """
    if auth.get_current_user() is not None:
        # Already logged in — skip the form and bounce to /admin.
        return redirect("/admin")
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ip = auth.client_ip()
        ua = request.headers.get("User-Agent", "") or ""
        result = auth.attempt_login(username, password, ip=ip, user_agent=ua)
        if result.get("ok"):
            session[auth.SESSION_COOKIE_NAME] = result["token"]
            session.permanent = True
            # Plant the CSRF cookie now so the destination /admin
            # doesn't have to ask for it on the landing GET.
            auth.csrf_cookie_token()
            return redirect("/admin")
        err = result.get("error", "invalid_credentials")
        # R64 — surface the lockout / invalid_credentials reason via
        # a tiny inline error so curl users see something useful.
        return Response(
            _login_error_html(err),
            status=401,
            mimetype="text/html",
        )
    return render_template_string(LOGIN_HTML)


def _login_error_html(err: str) -> str:
    """Tiny HTML page for curl-style POST /login failures (no AJAX)."""
    if err == "locked":
        msg = "账号已锁定，请 15 分钟后再试。"
    else:
        msg = "用户名或密码错误。"
    safe = msg.replace("<", "&lt;").replace(">", "&gt;")
    return (
        "<!doctype html><meta charset=utf-8>"
        "<title>登录失败</title>"
        "<p>" + safe + "</p>"
        "<p><a href='/login'>← 返回登录</a></p>"
    )


# ---------------------------------------------------------------------------
# Round 64 — 5 admin page templates + 5 admin JSON APIs.
#
# Each admin page route renders one of the new R64 templates and
# each admin API endpoint exposes the CRUD surface it consumes.
#
# Decorator stacking: every admin route carries
#   ``@require_auth(role='admin') @require_csrf``
# so the route is both authenticated AND CSRF-protected (for non-GET
# methods).  GET admin pages don't need CSRF; the new API surface
# does because every mutating route (POST / PUT / DELETE) changes
# server state.
# ---------------------------------------------------------------------------
ADMIN_USERS_HTML = _load_template("admin_users.html")
ADMIN_CONFIG_HTML = _load_template("admin_config.html")
ADMIN_TEST_HTML = _load_template("admin_test.html")
ADMIN_AUDIT_HTML = _load_template("admin_audit.html")


# ---------------------------------------------------------------------------
# /admin/users — user CRUD page
# ---------------------------------------------------------------------------
@app.route("/admin/users")
@require_auth(role='admin')
def admin_users_page():
    """Render the user-management page."""
    csrf = auth.csrf_cookie_token()
    return render_template_string(
        ADMIN_USERS_HTML,
        csrf_token_value=csrf,
        current_user=auth.get_current_user(),
    )


@app.route("/api/admin/users", methods=["GET"])
@require_auth(role='admin')
@require_csrf
def api_admin_users_list():
    """Round 64 — list every user with id / username / role / timestamps."""
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at, last_login_at "
            "FROM users ORDER BY id ASC"
        ).fetchall()
    return jsonify({
        "users": [
            {
                "id": r["id"],
                "username": r["username"],
                "role": r["role"],
                "created_at": r["created_at"] or "",
                "last_login_at": r["last_login_at"],
            }
            for r in rows
        ],
    })


@app.route("/api/admin/users", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def api_admin_users_create():
    """Round 64 — create a new user (admin or viewer)."""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    role = (data.get("role") or "").strip()
    # Validation — fail fast with explicit error keys.
    if not (3 <= len(username) <= 32):
        return jsonify({
            "ok": False,
            "error": "validation_failed",
            "field": "username",
        }), 400
    if len(password) < 8:
        return jsonify({
            "ok": False,
            "error": "validation_failed",
            "field": "password",
        }), 400
    if role not in auth.VALID_ROLES:
        return jsonify({
            "ok": False,
            "error": "validation_failed",
            "field": "role",
        }), 400
    try:
        user = auth.create_user(
            username=username, password=password, role=role,
        )
    except Exception as exc:  # noqa: BLE001
        # Most likely IntegrityError (UNIQUE on username) — surface as
        # ``username_exists`` so the JS can show a friendly toast.
        auth.write_audit(
            "user.create.failed",
            target=username,
            ip=auth.client_ip(),
            user_agent=request.headers.get("User-Agent", ""),
            details={"error": type(exc).__name__},
        )
        return jsonify({
            "ok": False,
            "error": "username_exists",
        }), 400
    auth.write_audit(
        "user.create",
        user_id=user.id,
        target=username,
        ip=auth.client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        details={"role": role},
    )
    return jsonify({
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
        },
    }), 201


@app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
@require_auth(role='admin')
@require_csrf
def api_admin_users_update(user_id: int):
    """Round 64 — update an existing user's role / password."""
    target = auth.get_user_by_id(user_id)
    if target is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    data = request.get_json(silent=True) or {}
    new_role = data.get("role")
    new_password = data.get("password")
    if new_role is None and not new_password:
        return jsonify({
            "ok": False,
            "error": "nothing_to_update",
        }), 400
    if new_role is not None:
        if new_role not in auth.VALID_ROLES:
            return jsonify({
                "ok": False,
                "error": "validation_failed",
                "field": "role",
            }), 400
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET role = ? WHERE id = ?",
                (new_role, user_id),
            )
    if new_password:
        if len(new_password) < 8:
            return jsonify({
                "ok": False,
                "error": "validation_failed",
                "field": "password",
            }), 400
        new_hash = auth.hash_password(new_password)
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, user_id),
            )
    refreshed = auth.get_user_by_id(user_id)
    auth.write_audit(
        "user.update",
        user_id=refreshed.id if refreshed else user_id,
        target=target.username,
        ip=auth.client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        details={
            "role_changed": new_role is not None,
            "password_changed": bool(new_password),
        },
    )
    return jsonify({
        "ok": True,
        "user": {
            "id": refreshed.id,
            "username": refreshed.username,
            "role": refreshed.role,
            "created_at": refreshed.created_at,
            "last_login_at": refreshed.last_login_at,
        },
    })


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@require_auth(role='admin')
@require_csrf
def api_admin_users_delete(user_id: int):
    """Round 64 — delete a user + revoke every session row for them.

    Guards:
      * cannot delete the currently-logged-in user (would 401 the
        operator mid-request).
      * cannot delete the LAST remaining admin — the system would
        be unreachable for further user management.
    """
    current = auth.get_current_user()
    if current is None or current.id != _current_user_id_from_request():
        # Defensive — the @require_auth should already have ensured
        # current is set.  We re-read from g.current_user for the
        # comparison.
        pass
    target = auth.get_user_by_id(user_id)
    if target is None:
        return jsonify({"ok": False, "error": "not_found"}), 404
    # Rule 1 — no self-delete.
    if target.id == current.id:
        return jsonify({
            "ok": False,
            "error": "cannot_delete_self",
        }), 400
    # Rule 2 — no deleting the last admin.
    if target.role == auth.ROLE_ADMIN:
        admin_count = 0
        with db.get_conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE role = ?",
                (auth.ROLE_ADMIN,),
            ).fetchone()
            admin_count = int(row["n"] or 0) if row else 0
        if admin_count <= 1:
            return jsonify({
                "ok": False,
                "error": "cannot_delete_last_admin",
            }), 400
    # Revoke every session row owned by this user, then delete the row.
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    auth.write_audit(
        "user.delete",
        user_id=current.id,
        target=target.username,
        ip=auth.client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        details={"role": target.role},
    )
    return jsonify({"ok": True})


def _current_user_id_from_request() -> int:
    """Best-effort ``current_user.id`` lookup without importing g."""
    user = auth.get_current_user()
    return user.id if user else -1


# ---------------------------------------------------------------------------
# /admin/config — scrape / push / admin password page
# ---------------------------------------------------------------------------
@app.route("/admin/config")
@require_auth(role='admin')
def admin_config_page():
    """Render the unified system-config page."""
    csrf = auth.csrf_cookie_token()
    # Pre-load scrape + push config so the JS can hydrate without an
    # extra round-trip (the ``__INITIAL_DATA__`` pattern from R66).
    scrape = {
        "dorm_base_url": db.get_meta("dorm_base_url") or config.DORM_BASE_URL,
        "dorm_openid": db.get_meta("dorm_openid") or config.DORM_OPENID,
        "dorm_room_id": db.get_meta("last_room_id") or "",
        "eqprice": db.get_meta("eqprice") or "0.5",
        "feishu_webhook_url": (
            db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK
        ),
    }
    push_keys = (
        "push_l2_enable", "push_daily_enable", "push_weekly_enable",
        "push_monthly_enable",
        "push_daily_time", "push_weekly_time", "push_monthly_time",
        "quiet_hours_start", "quiet_hours_end",
    )
    push_defaults = {
        "push_l2_enable": "1",
        "push_daily_enable": "1",
        "push_weekly_enable": "1",
        "push_monthly_enable": "1",
        "push_daily_time": "09:00",
        "push_weekly_time": "09:00",
        "push_monthly_time": "09:00",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:00",
    }
    push = {k: db.get_meta(k) or push_defaults[k] for k in push_keys}
    initial = {
        "scrape": scrape,
        "push": push,
    }
    return render_template_string(
        ADMIN_CONFIG_HTML,
        csrf_token_value=csrf,
        current_user=auth.get_current_user(),
        initial_data=initial,
    )


# ---------------------------------------------------------------------------
# /admin/test — manual scrape + push page
# ---------------------------------------------------------------------------
@app.route("/admin/test")
@require_auth(role='admin')
def admin_test_page():
    """Render the manual-trigger page (test scrape + test push)."""
    csrf = auth.csrf_cookie_token()
    return render_template_string(
        ADMIN_TEST_HTML,
        csrf_token_value=csrf,
        current_user=auth.get_current_user(),
    )


@app.route("/api/admin/test-scrape", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def api_admin_test_scrape():
    """Round 64 — trigger an immediate school scrape (force_refresh).

    Thin wrapper over ``dorm_power.fetch_once`` so the admin can run a
    scrape WITHOUT having to walk back to the dashboard and click the
    refresh button.  Same network work + DB persist, same Feishu
    suppression (``fetch_only=True`` semantics inside ``fetch_once``).
    """
    # Re-use the same internal-token guard as /api/refresh so an Nginx
    # bypass doesn't accidentally expose this surface.
    _check_internal_token()
    try:
        from dorm_power import fetch_once
    except ImportError as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"import: {exc}"}), 500
    try:
        result = fetch_once()
    except Exception as exc:  # noqa: BLE001
        logger.exception("admin test-scrape failed: %r", exc)
        return jsonify({
            "ok": False,
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        }), 500
    body = result.get("body") or {}
    scrape_status = result.get("scrape_status") or "unknown"
    auth.write_audit(
        "admin.test_scrape",
        user_id=(auth.get_current_user().id if auth.get_current_user() else None),
        target="/api/admin/test-scrape",
        ip=auth.client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        details={"scrape_status": scrape_status},
    )
    return jsonify({
        "ok": scrape_status == "ok",
        "scrape_status": scrape_status,
        "ts": result.get("dt") or body.get("dt") or "",
        "remain": _round2(body.get("remainEq")),
    })


@app.route("/api/admin/test-push", methods=["POST"])
@require_auth(role='admin')
@require_csrf
def api_admin_test_push():
    """Round 64 — fire a one-off webhook test message.

    Same implementation as the legacy ``/admin/api/test-push`` so the
    new R64 admin test page renders the same outcome.  We keep the
    legacy route intact for back-compat (it remains the path used by
    the OOBE wizard step 5).
    """
    try:
        from dorm_power import _post_feishu
    except ImportError as exc:  # pragma: no cover
        return jsonify({"ok": False, "error": f"import: {exc}"}), 500
    webhook = (
        db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK or ""
    )
    if not webhook:
        return jsonify({
            "ok": True,
            "notice": "no webhook configured",
        })
    _orig = config.FEISHU_WEBHOOK
    config.FEISHU_WEBHOOK = webhook
    try:
        _post_feishu(
            {
                "msg_type": "text",
                "content": {"text": "dorm-power-monitor 测试推送"},
            },
            raise_on_error=True,
        )
    except Exception as exc:  # noqa: BLE001
        config.FEISHU_WEBHOOK = _orig
        return jsonify({"ok": False, "error": str(exc)}), 500
    config.FEISHU_WEBHOOK = _orig
    auth.write_audit(
        "admin.test_push",
        user_id=(auth.get_current_user().id if auth.get_current_user() else None),
        target="/api/admin/test-push",
        ip=auth.client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        details={"ok": True},
    )
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# /admin/audit — audit_log viewer page
# ---------------------------------------------------------------------------
@app.route("/admin/audit")
@require_auth(role='admin')
def admin_audit_page():
    """Render the audit_log viewer."""
    csrf = auth.csrf_cookie_token()
    return render_template_string(
        ADMIN_AUDIT_HTML,
        csrf_token_value=csrf,
        current_user=auth.get_current_user(),
    )


@app.route("/api/admin/audit", methods=["GET"])
@require_auth(role='admin')
@require_csrf
def api_admin_audit_list():
    """Round 64 — paginated + filterable audit_log view.

    Query params (all optional):
      * ``page``        1-indexed page number (default 1).
      * ``per_page``    rows per page (default 50, capped at 200).
      * ``action``      exact-match filter (e.g. ``user.create``).
      * ``user_id``     restrict to actions performed by one user.
      * ``since``       ``YYYY-MM-DD HH:MM:SS`` floor on created_at.

    Returns ``{"rows": [...], "total": N, "page": p, "per_page": pp}``
    so the front-end can paint a "page 3 of 12" footer.
    """
    try:
        page = max(1, int(request.args.get("page", "1") or "1"))
    except (TypeError, ValueError):
        page = 1
    try:
        per_page = max(1, min(200, int(
            request.args.get("per_page", "50") or "50"
        )))
    except (TypeError, ValueError):
        per_page = 50
    action_filter = (request.args.get("action") or "").strip()
    user_filter = (request.args.get("user_id") or "").strip()
    since_filter = (request.args.get("since") or "").strip()

    clauses = []
    params: list = []
    if action_filter:
        clauses.append("action = ?")
        params.append(action_filter)
    if user_filter:
        try:
            clauses.append("user_id = ?")
            params.append(int(user_filter))
        except (TypeError, ValueError):
            pass
    if since_filter:
        clauses.append("created_at >= ?")
        params.append(since_filter)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    with db.get_conn() as conn:
        total_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM audit_log {where_sql}",
            params,
        ).fetchone()
        total = int(total_row["n"] or 0) if total_row else 0
        rows = conn.execute(
            f"SELECT id, user_id, action, target, ip, user_agent, "
            f"       created_at, details "
            f"FROM audit_log {where_sql} "
            f"ORDER BY id DESC "
            f"LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    return jsonify({
        "rows": [
            {
                "id": r["id"],
                "user_id": r["user_id"],
                "action": r["action"],
                "target": r["target"],
                "ip": r["ip"],
                "user_agent": r["user_agent"],
                "created_at": r["created_at"] or "",
                "details": r["details"],
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


# ---------------------------------------------------------------------------
# Round 64 — system-config JSON API.
#
# Unified GET / PUT over the ``meta`` table; the admin config page
# uses it to read scrape / push / password in one round-trip and to
# write changes with audit.  All mutating routes carry @require_csrf.
# ---------------------------------------------------------------------------
@app.route("/api/admin/config", methods=["GET"])
@require_auth(role='admin')
@require_csrf
def api_admin_config_get():
    """Return the unified config snapshot the admin pages consume."""
    scrape = {
        "dorm_base_url": db.get_meta("dorm_base_url") or config.DORM_BASE_URL,
        "dorm_openid": db.get_meta("dorm_openid") or config.DORM_OPENID,
        "dorm_room_id": db.get_meta("last_room_id") or "",
        "eqprice": db.get_meta("eqprice") or "0.5",
        "feishu_webhook_url": (
            db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK
        ),
    }
    push_defaults = {
        "push_l2_enable": "1",
        "push_daily_enable": "1",
        "push_weekly_enable": "1",
        "push_monthly_enable": "1",
        "push_daily_time": "09:00",
        "push_weekly_time": "09:00",
        "push_monthly_time": "09:00",
        "quiet_hours_start": "23:00",
        "quiet_hours_end": "07:00",
    }
    push = {
        k: db.get_meta(k) or v
        for k, v in push_defaults.items()
    }
    return jsonify({
        "ok": True,
        "scrape": scrape,
        "push": push,
        "admin_password_set": bool(
            db.get_meta("admin_password")
            or os.environ.get("ADMIN_PASSWORD", "")
        ),
    })


@app.route("/api/admin/config", methods=["PUT"])
@require_auth(role='admin')
@require_csrf
def api_admin_config_put():
    """Update one or more config keys; audit each write.

    Body shape::

        { "scrape": { "dorm_base_url": "...", ... },
          "push":   { "push_l2_enable": "1", ... },
          "admin_password": "new-password"    # optional
        }
    """
    data = request.get_json(silent=True) or {}
    changes: list[dict] = []
    scrape = data.get("scrape") or {}
    for k, v in scrape.items():
        meta_key = "last_room_id" if k == "dorm_room_id" else k
        db.set_meta(meta_key, str(v))
        changes.append({"key": meta_key, "section": "scrape"})
    push = data.get("push") or {}
    for k, v in push.items():
        db.set_meta(k, str(v))
        changes.append({"key": k, "section": "push"})
    new_pwd = data.get("admin_password")
    if new_pwd:
        db.set_meta("admin_password", str(new_pwd))
        changes.append({"key": "admin_password", "section": "auth"})
    if changes:
        auth.write_audit(
            "config.update",
            user_id=(auth.get_current_user().id if auth.get_current_user() else None),
            target="/api/admin/config",
            ip=auth.client_ip(),
            user_agent=request.headers.get("User-Agent", ""),
            details={"keys": changes},
        )
    return jsonify({"ok": True, "changed": len(changes)})


def main() -> None:
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)


if __name__ == "__main__":
    main()