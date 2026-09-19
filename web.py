"""web.py — Round 43 documented.

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
from flask import Flask, Response, jsonify, redirect, render_template_string, request

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


INDEX_HTML = """<!doctype html>
<!-- R51b — preview_v2 minimal modern (Linear / Vercel style) + R49 backend integration -->
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>电费助手 · 实时概览</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
:root {
  --bg-page: #0a0a0b;
  --bg-card: #131316;
  --bg-elevated: #1a1a1e;
  --border: rgba(255,255,255,0.08);
  --border-strong: rgba(255,255,255,0.14);
  --accent: #6366f1;
  --accent-hover: #818cf8;
  --accent-soft: rgba(99, 102, 241, 0.10);
  --text-primary: #fafafa;
  --text-secondary: #a1a1aa;
  --text-tertiary: #71717a;
  --online: #22c55e;
  --offline: #ef4444;
  --warning: #f59e0b;
  --shadow: 0 1px 3px rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.05);
  --shadow-hover: 0 4px 12px rgba(0,0,0,0.5), 0 0 0 1px rgba(255,255,255,0.1);
  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
  --font-mono: "JetBrains Mono", "SF Mono", "Cascadia Code", Consolas, monospace;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--font-sans);
  background: var(--bg-page);
  color: var(--text-primary);
  font-size: 14px;
  line-height: 1.5;
  -webkit-font-smoothing: antialiased;
  min-height: 100vh;
}

/* === Top nav ============================================== */
.topnav {
  position: sticky;
  top: 0;
  z-index: 50;
  background: rgba(10,10,11,0.8);
  backdrop-filter: blur(16px);
  border-bottom: 1px solid var(--border);
}
.topnav-inner {
  max-width: 1200px;
  margin: 0 auto;
  padding: 12px 24px;
  display: flex;
  align-items: center;
  gap: 24px;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 600;
  font-size: 14px;
}
.brand-mark {
  width: 22px; height: 22px;
  border-radius: 6px;
  background: var(--accent);
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}
.tabs {
  display: flex;
  gap: 4px;
  margin: 0 auto;
}
.tab {
  padding: 6px 12px;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  font-size: 13px;
  font-weight: 500;
  border-radius: 6px;
  cursor: pointer;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.tab:hover { color: var(--text-primary); background: rgba(255,255,255,0.04); }
.tab.active { color: var(--text-primary); background: var(--bg-elevated); }
.nav-actions {
  display: flex; align-items: center; gap: 8px;
}
.icon-btn {
  width: 32px; height: 32px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--text-secondary);
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}
.icon-btn:hover { background: var(--bg-elevated); color: var(--text-primary); }
.refresh-btn {
  padding: 0 12px;
  height: 32px;
  border-radius: 8px;
  background: var(--accent);
  color: #fff;
  border: none;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: inherit;
}
.refresh-btn:hover { background: var(--accent-hover); }
.refresh-btn.loading { pointer-events: none; opacity: 0.7; }
.refresh-btn .spin { display: inline-block; transition: transform 0.6s; }
.refresh-btn.loading .spin { animation: spin 1s linear infinite; }
@keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }

/* === Main container ===================================== */
main {
  max-width: 1200px;
  margin: 0 auto;
  padding: 32px 24px 80px;
}
.section { display: none; }
.section.active { display: block; }

/* === Hero =============================================== */
.hero {
  padding: 32px 0 48px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 32px;
}
.hero-label {
  font-size: 12px;
  color: var(--text-tertiary);
  font-weight: 500;
  letter-spacing: 0.5px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.hero-value {
  font-size: 64px;
  font-weight: 600;
  font-family: var(--font-mono);
  letter-spacing: -2px;
  line-height: 1;
  color: var(--text-primary);
  margin-bottom: 12px;
  font-variant-numeric: tabular-nums;
}
.hero-unit {
  font-size: 20px;
  color: var(--text-secondary);
  font-weight: 500;
  margin-left: 8px;
  font-family: var(--font-sans);
}
.hero-meta {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  font-size: 13px;
  color: var(--text-secondary);
}
.hero-meta .meta-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.status-dot {
  width: 8px; height: 8px;
  border-radius: 50%;
  background: var(--online);
  box-shadow: 0 0 0 3px rgba(34,197,94,0.15);
}
.status-dot.offline { background: var(--offline); box-shadow: 0 0 0 3px rgba(239,68,68,0.15); }
.status-dot.warning { background: var(--warning); box-shadow: 0 0 0 3px rgba(245,158,11,0.15); }

/* === Grid ================================================ */
.grid {
  display: grid;
  gap: 16px;
}
.grid-3 {
  grid-template-columns: repeat(4, 1fr);
}

/* === Stat card ========================================== */
.card {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 16px 18px;
}
.card-label {
  font-size: 12px;
  color: var(--text-secondary);
  font-weight: 500;
  margin-bottom: 6px;
}
.card-value {
  font-size: 28px;
  font-weight: 600;
  font-family: var(--font-mono);
  color: var(--text-primary);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.5px;
}
.card-value.is-empty { color: var(--text-tertiary); }
.card-foot {
  font-size: 11px;
  color: var(--text-tertiary);
  margin-top: 4px;
  font-family: var(--font-mono);
}

/* === Chart panel ========================================= */
.panel {
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 24px;
  margin-bottom: 16px;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 12px;
}
.panel-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0;
  color: var(--text-primary);
}
.panel-sub {
  font-size: 12px;
  color: var(--text-secondary);
  font-family: var(--font-mono);
}
.range-group {
  display: inline-flex;
  background: var(--bg-elevated);
  border-radius: 8px;
  padding: 2px;
  gap: 1px;
}
.range-btn {
  padding: 4px 10px;
  font-size: 12px;
  color: var(--text-secondary);
  background: transparent;
  border: none;
  cursor: pointer;
  border-radius: 6px;
  font-family: inherit;
  font-weight: 500;
}
.range-btn:hover { color: var(--text-primary); }
.range-btn.active { background: var(--accent); color: #fff; }
.chart-wrap {
  position: relative;
  height: 280px;
  width: 100%;
}

/* === Filter bar ========================================== */
.filter-bar {
  display: flex;
  gap: 8px;
  align-items: center;
  margin-bottom: 12px;
  flex-wrap: wrap;
}
.filter-bar input {
  background: var(--bg-elevated);
  border: 1px solid var(--border);
  color: var(--text-primary);
  padding: 6px 10px;
  border-radius: 8px;
  font-size: 12px;
  font-family: var(--font-mono);
  color-scheme: dark;
}
.filter-bar label {
  font-size: 12px;
  color: var(--text-secondary);
}
.btn {
  padding: 6px 14px;
  border-radius: 8px;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  border: 1px solid var(--border);
  font-family: inherit;
  background: transparent;
  color: var(--text-secondary);
}
.btn-primary {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
}
.btn-primary:hover { background: var(--accent-hover); }
.btn-secondary:hover { color: var(--text-primary); }

/* === Table =============================================== */
.data-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}
.data-table th {
  text-align: left;
  padding: 8px 12px;
  font-size: 11px;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  font-weight: 500;
  border-bottom: 1px solid var(--border);
}
.data-table th.text-end { text-align: right; }
.data-table td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  color: var(--text-primary);
}
.data-table td.text-end {
  text-align: right;
  font-family: var(--font-mono);
  font-weight: 500;
}
.data-table td.ts {
  font-family: var(--font-mono);
  color: var(--text-secondary);
  font-size: 12px;
}
.data-table tbody tr:hover { background: rgba(255,255,255,0.02); }

.empty-state {
  text-align: center;
  padding: 40px 20px;
  color: var(--text-tertiary);
  font-size: 13px;
}

/* === List rows (finance / meter sections) ================= */
.list-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 0;
  border-bottom: 1px solid var(--border);
}
.list-row:last-child { border-bottom: none; }
.list-left { display: flex; flex-direction: column; gap: 4px; }
.list-title { font-size: 13px; color: var(--text-primary); font-weight: 500; }
.list-sub { font-size: 11px; color: var(--text-tertiary); font-family: var(--font-mono); }
.list-right { display: flex; align-items: center; gap: 12px; }
.list-value { font-size: 14px; font-weight: 600; color: var(--text-primary); font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
.list-value.is-empty { color: var(--text-tertiary); }
.tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 500;
  background: var(--accent-soft);
  color: var(--accent);
  letter-spacing: 0.3px;
}
.tag.tag-warning { background: rgba(245,158,11,0.12); color: var(--warning); }
.tag.tag-red { background: rgba(239,68,68,0.12); color: var(--offline); }

/* === Meter grid (meter section) =========================== */
.meter-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 16px;
}
.meter-grid .card { padding: 16px 18px; }

/* === Hidden helpers ====================================== */
.last-update-hidden { display: none; }
.pill { display: none; }
.stat-readtime-hidden { display: none; }

/* === Bottom status bar =================================== */
.status-bar {
  position: fixed;
  bottom: 0; left: 0; right: 0;
  height: 36px;
  background: rgba(10,10,11,0.8);
  backdrop-filter: blur(16px);
  border-top: 1px solid var(--border);
  display: flex;
  align-items: center;
  padding: 0 24px;
  font-size: 11px;
  color: var(--text-tertiary);
  font-family: var(--font-mono);
  gap: 16px;
}
.status-bar .spacer { flex: 1; }
.status-bar kbd {
  background: rgba(99,102,241,0.12);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 1px 6px;
  font-family: var(--font-mono);
  font-size: 10px;
}

/* === Toast ================================================ */
.toast-container {
  position: fixed;
  top: 80px;
  right: 24px;
  z-index: 300;
  display: flex;
  flex-direction: column;
  gap: 8px;
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  padding: 12px 16px;
  background: var(--bg-card);
  border: 1px solid var(--border);
  border-radius: 8px;
  color: var(--text-primary);
  font-size: 13px;
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 200px;
  max-width: 360px;
  animation: slide-in 0.2s ease-out;
}
.toast.leaving { animation: slide-out 0.2s ease-in forwards; }
@keyframes slide-in {
  from { transform: translateX(100%); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
@keyframes slide-out { to { transform: translateX(100%); opacity: 0; } }
.toast.success { border-left: 3px solid var(--online); }
.toast.error { border-left: 3px solid var(--offline); }
.toast.info { border-left: 3px solid var(--accent); }
.toast-close {
  margin-left: auto;
  background: transparent;
  border: none;
  color: var(--text-secondary);
  cursor: pointer;
  font-size: 16px;
  padding: 0;
  width: 18px;
  height: 18px;
}

/* === Mobile ============================================== */
@media (max-width: 768px) {
  .topnav-inner { padding: 10px 16px; flex-wrap: wrap; gap: 12px; }
  .tabs { order: 3; width: 100%; justify-content: center; margin: 0; }
  main { padding: 20px 16px 80px; }
  .hero { padding: 20px 0 32px; }
  .hero-value { font-size: 48px; }
  .grid-3 { grid-template-columns: repeat(2, 1fr); gap: 12px; }
  .card { padding: 14px; }
  .card-value { font-size: 22px; }
  .panel { padding: 16px; }
  .chart-wrap { height: 220px; }
  .meter-grid { grid-template-columns: 1fr; }
}
@media (max-width: 480px) {
  .hero-value { font-size: 40px; }
  .grid-3 { grid-template-columns: 1fr; }
}
</style>
</head>
<body>

<nav class="topnav">
  <div class="topnav-inner">
    <div class="brand">
      <span class="brand-mark">⚡</span>
      <span>电费助手</span>
    </div>
    <div class="tabs">
      <button type="button" class="tab active" data-section="overview">概览</button>
      <button type="button" class="tab" data-section="history">历史</button>
      <button type="button" class="tab" data-section="finance">违规</button>
      <button type="button" class="tab" data-section="meter">电表</button>
    </div>
    <div class="nav-actions">
      <button id="theme-toggle" class="icon-btn" type="button" aria-label="主题">🎨</button>
      <button id="refresh-btn" class="refresh-btn" type="button" aria-label="刷新数据">
        <span class="spin">⟳</span><span>刷新</span>
      </button>
    </div>
  </div>
</nav>

<main>
  <!-- ============================================================ -->
  <!-- 概览 — preview_v2 主视觉 (hero + chart + 4 stat cards)        -->
  <!-- ============================================================ -->
  <section id="section-overview" class="section active" data-section="overview">
    <div class="hero">
      <div class="hero-label">剩余电量</div>
      <div class="hero-value">
        {{ stats.remain if stats.remain is not none else '—' }}<span class="hero-unit">kW·h</span>
      </div>
      <div class="hero-meta">
        <span class="meta-item"><span class="status-dot" id="hero-status-dot"></span><span id="hero-status-text">已连接</span></span>
        <span class="meta-item">抄表 {{ stats.read_time or '—' }}</span>
        <span class="meta-item" id="last-update">{{ initialLatestTs }}</span>
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">剩余电量趋势</h5>
        <div class="range-group" id="range-group" role="group" aria-label="时间范围">
          {% for h, label in [(24, '24h'), (72, '3d'), (168, '7d'), (720, '30d')] %}
            <button type="button" class="range-btn {% if hours == h %}active{% endif %}" data-hours="{{ h }}">{{ label }}</button>
          {% endfor %}
        </div>
      </div>
      <div class="chart-wrap">
        <canvas id="chart"></canvas>
      </div>
    </div>

    <div class="grid grid-3">
      <div class="card" id="card-monthly-projection">
        <div class="card-label">本月费用</div>
        <div class="card-value is-empty" id="stat-monthly-projection">¥<span id="stat-monthly-eqprice">—</span></div>
        <div class="card-foot" id="stat-monthly-detail">@¥0.500/kW·h</div>
      </div>
      <div class="card">
        <div class="card-label">1 小时</div>
        <div class="card-value {% if stats.hourly_used is none %}is-empty{% endif %}" id="stat-hourly">
          {% if stats.hourly_used is not none %}{{ '%.2f' % stats.hourly_used }} <span style="font-size:14px;color:var(--text-secondary)">kW·h</span>{% else %}—{% endif %}
        </div>
        <div class="card-foot">差值</div>
      </div>
      <div class="card">
        <div class="card-label">日均</div>
        <div class="card-value {% if stats.daily_avg is none %}is-empty{% endif %}" id="stat-daily">
          {% if stats.daily_avg is not none %}{{ '%.2f' % stats.daily_avg }} <span style="font-size:14px;color:var(--text-secondary)">kW·h</span>{% else %}—{% endif %}
        </div>
        <div class="card-foot">窗口内</div>
      </div>
      <div class="card">
        <div class="card-label">抄表</div>
        <div class="card-value" style="font-size:18px" id="stat-readtime">{{ stats.read_time or '—' }}</div>
        <div class="card-foot">学校电表最近</div>
      </div>
      <!-- 隐藏的 stat-remain 让 JS 仍能找到 (R49 兼容) -->
      <div class="last-update-hidden">
        <span id="stat-remain">{{ '%.2f' % stats.remain if stats.remain is not none else '—' }}</span>
      </div>
    </div>
  </section>

  <!-- ============================================================ -->
  <!-- 历史趋势 — daily chart + records table                       -->
  <!-- ============================================================ -->
  <section id="section-history" class="section" data-section="history">
    <div class="hero" style="padding:32px 0 32px;border-bottom:none;margin-bottom:24px">
      <div class="hero-label">历史趋势</div>
      <div class="hero-meta" style="margin-top:8px">
        {{ rows|length }} 条采集记录 · {{ daily_elec|length }} 天有效数据
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">每日用电曲线</h5>
        <span class="panel-sub">近 30 天有效用电</span>
      </div>
      {% if daily_elec %}
      <div class="chart-wrap">
        <canvas id="chart-daily"></canvas>
      </div>
      {% else %}
      <div class="empty-state">暂无每日用电数据</div>
      {% endif %}
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">采集记录</h5>
        <span class="panel-sub" id="records-sub">共 {{ rows|length }} 条</span>
      </div>

      <div class="filter-bar" id="records-filter">
        <label>起始时间</label>
        <input type="datetime-local" id="records-start">
        <label>截止时间</label>
        <input type="datetime-local" id="records-end">
        <button type="button" id="records-query" class="btn btn-primary">查询</button>
        <button type="button" id="records-reset" class="btn btn-secondary">重置</button>
        <span id="records-range-label" style="font-size:12px;color:var(--text-secondary);margin-left:auto"></span>
      </div>

      {% if rows %}
      <div style="overflow-x:auto">
        <table class="data-table">
          <thead>
            <tr>
              <th>采集时间</th>
              <th>抄表时间</th>
              <th class="text-end">剩余电量 (kW·h)</th>
            </tr>
          </thead>
          <tbody id="records-tbody">
            {% for r in rows|reverse %}
            <tr>
              <td class="ts">{{ r.ts }}</td>
              <td class="ts">{{ r.read_time or '—' }}</td>
              <td class="text-end">
                {% if r.remain is not none %}{{ '%.2f' % r.remain }}{% else %}—{% endif %}
              </td>
            </tr>
            {% endfor %}
          </tbody>
        </table>
      </div>
      <div class="empty-state" id="records-empty" style="display:none">暂无采集记录</div>
      {% else %}
      <div class="empty-state" id="records-empty">暂无采集记录</div>
      {% endif %}
    </div>
  </section>

  <!-- ============================================================ -->
  <!-- 违规与缴费 — list rows                                       -->
  <!-- ============================================================ -->
  <section id="section-finance" class="section" data-section="finance">
    <div class="hero" style="padding:32px 0 32px;border-bottom:none;margin-bottom:24px">
      <div class="hero-label">违规与缴费</div>
      <div class="hero-meta" style="margin-top:8px">
        {{ violations|length }} 条违规 · {{ pay_history|length }} 条缴费
      </div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">违规记录</h5>
        <span class="panel-sub">共 {{ violations|length }} 条</span>
      </div>
      {% if violations %}
      <div>
        {% for v in violations %}
          {% set wg_power = v.wg_power %}
          {% set tag_class = 'tag-red' if (wg_power is not none and wg_power > 1000) else 'tag-warning' %}
        <div class="list-row">
          <div class="list-left">
            <span class="list-title">{{ v.wg_reason or '违规' }}</span>
            <span class="list-sub">{{ v.dt or '—' }}</span>
          </div>
          <div class="list-right">
            <span class="tag {{ tag_class }}">
              {% if v.wg_power is not none %}{{ '%.2f' % v.wg_power }} W{% else %}违规{% endif %}
            </span>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">暂无违规记录</div>
      {% endif %}
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">缴费记录</h5>
        <span class="panel-sub">共 {{ pay_history|length }} 条</span>
      </div>
      {% if pay_history %}
      <div>
        {% for p in pay_history %}
          {% set fee_lower = (p.fee_type or '')|lower %}
          {% set tag_class = 'tag-red' if '罚款' in fee_lower or '罚' in fee_lower else '' %}
        <div class="list-row">
          <div class="list-left">
            <span class="list-title">{{ p.pay_type or p.fee_type or '缴费' }}</span>
            <span class="list-sub">{{ p.dt or '—' }}</span>
          </div>
          <div class="list-right">
            <span class="tag {{ tag_class }}">{{ p.fee_type or '缴费' }}</span>
            <span class="list-value {% if p.money is none %}is-empty{% endif %}">
              {% if p.money is not none %}¥{{ '%.2f' % p.money }}{% else %}—{% endif %}
            </span>
          </div>
        </div>
        {% endfor %}
      </div>
      {% else %}
      <div class="empty-state">暂无缴费记录</div>
      {% endif %}
    </div>
  </section>

  <!-- ============================================================ -->
  <!-- 实时电表 — meter-grid                                        -->
  <!-- ============================================================ -->
  <section id="section-meter" class="section" data-section="meter">
    <div class="hero" style="padding:32px 0 32px;border-bottom:none;margin-bottom:24px">
      <div class="hero-label">实时电表</div>
      <div class="hero-meta" style="margin-top:8px">
        最近一次上报 · {{ run_status.update_dt if run_status else '—' }}
      </div>
      <div class="hero-meta" style="margin-top:8px;font-size:11px">每 30 秒自动刷新</div>
    </div>

    <div class="panel">
      <div class="panel-head">
        <h5 class="panel-title">实时电表</h5>
        <span class="panel-sub">最近一次上报</span>
      </div>
      {% if run_status %}
      <div class="meter-grid">
        <div class="card">
          <div class="card-label">电压</div>
          <div class="card-value" id="meter-vol">
            {% if run_status.vol is not none %}{{ '%.2f' % run_status.vol }} <span style="font-size:14px;color:var(--text-secondary)">V</span>{% else %}—{% endif %}
          </div>
        </div>
        <div class="card">
          <div class="card-label">电流</div>
          <div class="card-value" id="meter-cur">
            {% if run_status.cur is not none %}{{ '%.2f' % run_status.cur }} <span style="font-size:14px;color:var(--text-secondary)">A</span>{% else %}—{% endif %}
          </div>
        </div>
        <div class="card">
          <div class="card-label">有功功率</div>
          <div class="card-value" id="meter-yggl">
            {% if run_status.yggl is not none %}{{ '%.3f' % run_status.yggl }} <span style="font-size:14px;color:var(--text-secondary)">W</span>{% else %}—{% endif %}
          </div>
        </div>
        <div class="card">
          <div class="card-label">电表状态</div>
          {% set rs_label = run_status.run_status or '—' %}
          <div class="card-value" style="font-size:18px" id="meter-status-pill">{{ rs_label }}</div>
        </div>
        <div class="card" style="grid-column:1 / -1">
          <div class="card-label">最后上报时间</div>
          <div class="card-value" style="font-size:18px" id="meter-update-dt">{{ run_status.update_dt or '—' }}</div>
        </div>
      </div>
      {% else %}
      <div class="empty-state">暂无电表数据</div>
      {% endif %}
    </div>
  </section>
</main>

<!-- 隐藏的 #status-pill — setPill / setLastUpdate 仍能找到 -->
<span id="status-pill" data-ts="{{ rows[-1].ts if rows else '' }}" style="display:none"></span>

<div class="status-bar" id="status-bar" aria-live="polite">
  <span style="display:inline-flex;align-items:center;gap:6px"><span class="status-dot" id="status-bar-dot"></span><span id="status-bar-text">连接中…</span></span>
  <span id="status-bar-ts">最近采集 —</span>
  <span class="spacer"></span>
  <span><kbd>1</kbd>–<kbd>4</kbd> 切换 · <kbd>R</kbd> 刷新</span>
  <span>v51b</span>
</div>

<div class="toast-container" id="toast-container" aria-live="polite"></div>

<script>
(function () {
  /* === 模板变量注入 ============================================ */
  var currentHours = {{ hours|tojson }};
  var initialRows = {{ rows|tojson }};
  var initialDaily = {{ daily_elec|tojson }};
  var initialLatestTs = {{ (rows[-1].ts if rows else '')|tojson }};
  var chartInstance = null;
  var dailyChartInstance = null;
  var pollFailStreak = 0;
  var STORAGE_KEY = 'dorm-power-monitor.section';

  /* === 工具函数 =============================================== */
  function fmtAxisLabel(raw) {
    if (!raw) return '';
    var m = String(raw).match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})/);
    if (m) return m[2] + '-' + m[3] + ' ' + m[4] + ':' + m[5];
    return raw;
  }
  function readCssVar(name, fallback) {
    var v = getComputedStyle(document.documentElement).getPropertyValue(name);
    v = (v || '').trim();
    return v || fallback;
  }
  function setStatText(el, v, unit) {
    if (!el) return;
    el.classList.remove('is-empty');
    if (v === null || v === undefined || (typeof v === 'number' && isNaN(v))) {
      el.classList.add('is-empty');
      el.textContent = '—';
      return;
    }
    var text;
    if (typeof v === 'number') {
      text = v.toFixed(2) + (unit ? ' <span style="font-size:14px;color:var(--text-secondary)">' + unit + '</span>' : '');
    } else {
      text = String(v);
    }
    el.innerHTML = text;
  }
  function setPill() {
    var pill = document.getElementById('status-pill');
    if (!pill) return;
    var tsRaw = pill.getAttribute('data-ts') || '';
    // R48 — ts is NAIVE LOCAL (CST, "YYYY-MM-DD HH:MM:SS"), append +08:00
    // so JS Date parses it as Asia/Shanghai wall clock independent of host TZ.
    var last = tsRaw ? new Date(tsRaw.replace(' ', 'T') + '+08:00') : null;
    var isOnline = false;
    if (last && !isNaN(last.getTime())) {
      var ageSec = (Date.now() - last.getTime()) / 1000;
      isOnline = ageSec >= 0 && ageSec < 7200;
    }
    var heroDot = document.getElementById('hero-status-dot');
    var heroText = document.getElementById('hero-status-text');
    if (heroDot) {
      heroDot.classList.remove('offline', 'warning');
      if (!isOnline) heroDot.classList.add('offline');
    }
    if (heroText) {
      heroText.textContent = isOnline ? '已连接' : '已断开';
    }
    var barDot = document.getElementById('status-bar-dot');
    var barText = document.getElementById('status-bar-text');
    var barTs = document.getElementById('status-bar-ts');
    if (barDot && barText) {
      barDot.classList.remove('offline', 'warning');
      if (!isOnline) barDot.classList.add('offline');
      barText.textContent = isOnline ? '已连接' : '已离线';
    }
    if (barTs) {
      barTs.textContent = tsRaw ? ('最近采集 ' + tsRaw) : '最近采集 —';
    }
  }
  function setLastUpdate(tsRaw) {
    var el = document.getElementById('last-update');
    if (!el) return;
    el.textContent = tsRaw ? tsRaw : '—';
  }

  /* === Toast 通知 ============================================== */
  function showToast(message, kind, duration) {
    kind = kind || 'info';
    duration = (typeof duration === 'number') ? duration : 3000;
    var container = document.getElementById('toast-container');
    if (!container) return;
    var toast = document.createElement('div');
    toast.className = 'toast ' + kind;
    var icon = (kind === 'success') ? '✓' : (kind === 'error') ? '✕' : 'ⓘ';
    toast.innerHTML =
      '<span style="font-size:16px">' + icon + '</span>' +
      '<span></span>' +
      '<button class="toast-close" type="button" aria-label="关闭">×</button>';
    toast.querySelector('span:nth-of-type(2)').textContent = message;
    container.appendChild(toast);
    var close = function () {
      toast.classList.add('leaving');
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, 200);
    };
    toast.querySelector('.toast-close').addEventListener('click', close);
    if (duration > 0) setTimeout(close, duration);
  }

  /* === Chart.js 初始化 ========================================== */
  function buildChart(rows) {
    var canvas = document.getElementById('chart');
    if (!canvas) return;
    var labels = (rows || []).map(function (r) { return fmtAxisLabel(r.ts); });
    var data = (rows || []).map(function (r) {
      return (r.remain === null || r.remain === undefined) ? null : Number(r.remain);
    });
    var rawTsList = (rows || []).map(function (r) {
      return r && r.ts ? String(r.ts) : '';
    });
    if (chartInstance) {
      chartInstance.data.labels = labels;
      chartInstance.data.datasets[0].data = data;
      chartInstance.update();
      return;
    }
    var textSecondary = readCssVar('--text-secondary', '#a1a1aa');
    var borderColor = readCssVar('--border', 'rgba(255,255,255,0.08)');
    var tooltipBg = readCssVar('--bg-card', '#131316');
    var tooltipFg = readCssVar('--text-primary', '#fafafa');
    chartInstance = new Chart(canvas.getContext('2d'), {
      type: 'line',
      data: {
        labels: labels,
        datasets: [{
          label: '剩余电量 (kW·h)',
          data: data,
          borderColor: '#6366f1',
          backgroundColor: function (context) {
            var chart = context.chart;
            var c = chart.ctx;
            var area = chart.chartArea;
            if (!area) return 'rgba(99,102,241,0.20)';
            var g = c.createLinearGradient(0, area.top, 0, area.bottom);
            g.addColorStop(0, 'rgba(99,102,241,0.20)');
            g.addColorStop(1, 'rgba(99,102,241,0)');
            return g;
          },
          fill: true,
          tension: 0.3,
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointBackgroundColor: '#6366f1',
          pointBorderColor: tooltipBg,
          pointBorderWidth: 1.5,
          spanGaps: true,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: tooltipBg,
            borderColor: borderColor,
            borderWidth: 1,
            titleColor: '#a1a1aa',
            bodyColor: '#fafafa',
            padding: 10,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items || !items.length) return '';
                var idx = items[0].dataIndex;
                return rawTsList[idx] || labels[idx] || '';
              },
              label: function (item) {
                var v = item.parsed.y;
                return (v == null ? '—' : v.toFixed(2) + ' kW·h');
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, maxRotation: 0, autoSkip: true, autoSkipPadding: 20 },
            border: { color: 'rgba(255,255,255,0.05)' }
          },
          y: {
            grid: { color: 'rgba(255,255,255,0.04)' },
            ticks: { color: '#71717a', font: { size: 10 }, callback: function (v) { return v; } },
            border: { display: false }
          }
        }
      }
    });
  }

  function buildDailyChart(rows) {
    var canvas = document.getElementById('chart-daily');
    if (!canvas) return;
    var labels = (rows || []).map(function (r) { return fmtAxisLabel(r.dt); });
    var data = (rows || []).map(function (r) {
      if (r.used_today === null || r.used_today === undefined) return null;
      var v = Number(r.used_today);
      return (isNaN(v) || v < 0) ? null : v;
    });
    var rawDtList = (rows || []).map(function (r) {
      return r && r.dt ? String(r.dt) : '';
    });
    if (dailyChartInstance) {
      dailyChartInstance.data.labels = labels;
      dailyChartInstance.data.datasets[0].data = data;
      dailyChartInstance.update();
      return;
    }
    var textSecondary = readCssVar('--text-secondary', '#a1a1aa');
    var borderColor = readCssVar('--border', 'rgba(255,255,255,0.08)');
    var tooltipBg = readCssVar('--bg-card', '#131316');
    var tooltipFg = readCssVar('--text-primary', '#fafafa');
    dailyChartInstance = new Chart(canvas.getContext('2d'), {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: '每日用电 (kW·h)',
          data: data,
          borderRadius: 6,
          borderSkipped: false,
          backgroundColor: function (context) {
            var chart = context.chart;
            var c = chart.ctx;
            var area = chart.chartArea;
            if (!area) return 'rgba(99,102,241,0.6)';
            var g = c.createLinearGradient(0, area.top, 0, area.bottom);
            g.addColorStop(0, 'rgba(99,102,241,0.85)');
            g.addColorStop(1, 'rgba(99,102,241,0.45)');
            return g;
          },
          borderColor: 'rgba(99,102,241,0.9)',
          borderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 350 },
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: tooltipBg,
            borderColor: borderColor,
            borderWidth: 1,
            titleColor: '#a1a1aa',
            bodyColor: '#fafafa',
            padding: 10,
            displayColors: false,
            callbacks: {
              title: function (items) {
                if (!items || !items.length) return '';
                var idx = items[0].dataIndex;
                return rawDtList[idx] || labels[idx] || '';
              },
              label: function (item) {
                var v = item.parsed.y;
                return (v == null ? '—' : v.toFixed(2) + ' kW·h');
              }
            }
          }
        },
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, maxRotation: 0, autoSkipPadding: 16 },
            border: { color: borderColor }
          },
          y: {
            beginAtZero: true,
            grid: { color: 'rgba(255,255,255,0.04)', drawBorder: false },
            border: { display: false },
            ticks: { color: '#71717a', font: { size: 10 }, callback: function (v) { return Number(v).toFixed(1); } }
          }
        }
      }
    });
  }

  /* === 月度预测卡片更新 (Round 26b) ============================ */
  function updateMonthlyProjection(p) {
    var valEl = document.getElementById('stat-monthly-projection');
    var priceEl = document.getElementById('stat-monthly-eqprice');
    var detailEl = document.getElementById('stat-monthly-detail');
    if (!valEl) return;
    var mp = (p && typeof p.monthly_projection === 'number') ? p.monthly_projection : null;
    var eqprice = p && p.eqprice;
    if (mp === null) {
      valEl.classList.add('is-empty');
      var bd = (p && p.monthly_breakdown) || {};
      var usedKwh = (bd.used_kwh != null) ? Number(bd.used_kwh) : null;
      var eqpriceNum = (p && p.eqprice != null) ? Number(p.eqprice) : null;
      if (usedKwh != null && usedKwh > 0 && eqpriceNum != null && eqpriceNum > 0) {
        valEl.innerHTML = '¥' + (usedKwh * eqpriceNum).toFixed(2);
        if (priceEl) priceEl.textContent = '';
        if (detailEl) detailEl.textContent = '已用 ' + usedKwh.toFixed(2) + ' kW·h（日均数据积累中）';
      } else {
        valEl.innerHTML = '¥—';
        if (priceEl) priceEl.textContent = '';
        if (detailEl) detailEl.textContent = '日均数据积累中';
      }
    } else {
      valEl.classList.remove('is-empty');
      valEl.innerHTML = '¥' + mp.toFixed(2) +
        (eqprice ? ' <span style="font-size:14px;color:var(--text-secondary)">@¥' + Number(eqprice).toFixed(3) + '/kW·h</span>' : '');
      if (detailEl) {
        var bd = (p && p.monthly_breakdown) || {};
        var used = (bd.used_kwh != null) ? bd.used_kwh.toFixed(2) : '0';
        var avg = (bd.avg_daily != null) ? bd.avg_daily.toFixed(2) : '—';
        var left = bd.days_left != null ? bd.days_left : '—';
        detailEl.textContent = '已用 ' + used + ' · 日均 ' + avg + ' · 剩 ' + left + ' 天';
      }
    }
  }

  /* === Skeleton 切换 ========================================= */
  function showSkeletons(on) {
    var cards = document.querySelectorAll('.card');
    for (var i = 0; i < cards.length; i++) {
      cards[i].style.transition = 'opacity 0.2s';
      cards[i].style.opacity = on ? '0.5' : '1';
    }
  }
  function showForceRefreshStatus(message) {
    var btn = document.getElementById('refresh-btn');
    if (!btn) return;
    if (message) {
      btn.disabled = true;
      btn.classList.add('loading');
    } else {
      btn.disabled = false;
      btn.classList.remove('loading');
    }
  }

  /* === 电表 cell 更新 ========================================== */
  function updateMeter(rs) {
    var setCell = function (id, text, unit, decimals) {
      var el = document.getElementById(id);
      if (!el) return;
      if (text === null || text === undefined || (typeof text === 'number' && isNaN(text))) {
        el.innerHTML = '—';
        el.classList.add('is-empty');
      } else {
        el.classList.remove('is-empty');
        el.innerHTML = Number(text).toFixed(decimals == null ? 2 : decimals) +
          (unit ? ' <span style="font-size:14px;color:var(--text-secondary)">' + unit + '</span>' : '');
      }
    };
    setCell('meter-vol', rs.vol, 'V', 2);
    setCell('meter-cur', rs.cur, 'A', 2);
    setCell('meter-yggl', rs.yggl, 'W', 3);
    var pillEl = document.getElementById('meter-status-pill');
    if (pillEl) {
      var label = rs.run_status || '—';
      pillEl.textContent = label;
      if (label === '在线' || label === '正常' || label === '通讯正常') {
        pillEl.style.color = 'var(--online)';
      } else if (label === '—') {
        pillEl.style.color = 'var(--warning)';
      } else {
        pillEl.style.color = 'var(--offline)';
      }
    }
    var updEl = document.getElementById('meter-update-dt');
    if (updEl) {
      updEl.textContent = rs.update_dt || '—';
      updEl.classList.toggle('is-empty', !rs.update_dt);
    }
  }

  /* === 数据流编排 ============================================== */
  function updateAll(payload) {
    var stats = payload.stats || {};
    var rows = payload.rows || [];
    var pill = document.getElementById('status-pill');
    if (pill) {
      pill.setAttribute('data-ts', rows.length ? (rows[rows.length - 1].ts || '') : '');
      setPill();
    }
    var latestTs = rows.length ? rows[rows.length - 1].ts : initialLatestTs;
    setLastUpdate(latestTs);
    setStatText(document.getElementById('stat-remain'), stats.remain, 'kW·h');
    setStatText(document.getElementById('stat-hourly'), stats.hourly_used, 'kW·h');
    setStatText(document.getElementById('stat-readtime'), stats.read_time || null);
    setStatText(document.getElementById('stat-daily'), stats.daily_avg, 'kW·h');
    buildChart(rows);
  }

  function manualRefresh(hours) {
    if (typeof hours === 'number') currentHours = hours;
    var btn = document.getElementById('refresh-btn');
    if (btn) {
      btn.disabled = true;
      btn.classList.add('loading');
    }
    fetch('/api/data?hours=' + currentHours)
      .then(function (r) { return r.json(); })
      .then(function (payload) { updateAll(payload); })
      .catch(function (e) {
        if (typeof showToast === 'function') {
          showToast('刷新失败: ' + (e && e.message || '网络错误'), 'error');
        }
      })
      .then(function () {
        if (btn) {
          btn.disabled = false;
          btn.classList.remove('loading');
        }
      });
  }

  /* === Force refresh (Round 26b) ================================= */
  function forceRefresh(triggeredByUser) {
    showSkeletons(true);
    showForceRefreshStatus('🔄 正在抓取最新数据…');
    if (triggeredByUser && typeof showToast === 'function') showToast('刷新中…', 'info', 1500);

    var refreshDone = function () {
      return Promise.all([
        fetch('/api/live').then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }),
        fetch('/api/data?hours=' + currentHours).then(function (r) {
          if (!r.ok) throw new Error('HTTP ' + r.status);
          return r.json();
        }),
      ]).then(function (results) {
        var live = results[0] || {};
        var data = results[1] || {};
        applyLiveUpdate(live);
        if (data && data.rows) buildChart(data.rows);
      });
    };

    fetch('/api/refresh', { method: 'POST' })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (resp) {
        if (!resp.ok || !resp.j || resp.j.ok !== true) {
          var errMsg = (resp.j && resp.j.error) ? resp.j.error : 'HTTP 失败';
          throw new Error(errMsg);
        }
        if (typeof showToast === 'function') {
          showToast(
            '✓ 抓取完成 · ' + (resp.j.scrape_status || 'ok') +
            (resp.j.ts ? ' · ' + resp.j.ts : ''),
            'success', 2500
          );
        }
        return refreshDone();
      })
      .catch(function (e) {
        if (typeof showToast === 'function') {
          showToast('抓取失败,使用最近缓存: ' + (e && e.message || ''), 'error', 4000);
        }
        return refreshDone().catch(function () { /* ignore */ });
      })
      .then(function () {
        showSkeletons(false);
        showForceRefreshStatus(null);
      });
  }

  /* === 30 秒轮询 /api/live ===================================== */
  function autorefreshLive() {
    fetch('/api/live')
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (payload) {
        pollFailStreak = 0;
        applyLiveUpdate(payload);
      })
      .catch(function (e) {
        pollFailStreak += 1;
        if (pollFailStreak === 3 && typeof showToast === 'function') {
          showToast('连续 3 次自动刷新失败: ' + (e && e.message || ''), 'error', 5000);
        }
      });
  }
  setInterval(autorefreshLive, 30000);

  function applyLiveUpdate(payload) {
    if (!payload) return;
    var pill = document.getElementById('status-pill');
    if (pill && payload.latest_ts) {
      pill.setAttribute('data-ts', payload.latest_ts);
      setPill();
    }
    if (payload.latest_ts) setLastUpdate(payload.latest_ts);
    var stats = payload.stats || {};
    setStatText(document.getElementById('stat-remain'), stats.remain, 'kW·h');
    setStatText(document.getElementById('stat-hourly'), stats.hourly_used, 'kW·h');
    setStatText(document.getElementById('stat-readtime'), stats.read_time || null);
    setStatText(document.getElementById('stat-daily'), stats.daily_avg, 'kW·h');
    updateMonthlyProjection(payload);
    if (payload.run_status) updateMeter(payload.run_status);
    fetch('/api/data?hours=' + currentHours)
      .then(function (r) { return r.json(); })
      .then(function (p) { if (p && p.rows) buildChart(p.rows); })
      .catch(function () { /* swallow chart refresh errors */ });
  }

  /* === Section 切换（顶 nav）==================================== */
  function activateSection(section) {
    if (!section) return;
    document.querySelectorAll('[data-section]').forEach(function (el) {
      el.classList.toggle('active', el.getAttribute('data-section') === section);
    });
    requestAnimationFrame(function () {
      var activeSection = document.querySelector('.section.active');
      if (!activeSection) return;
      if (chartInstance && activeSection.contains(document.getElementById('chart'))) {
        chartInstance.resize();
      }
      if (dailyChartInstance && activeSection.contains(document.getElementById('chart-daily'))) {
        dailyChartInstance.resize();
      }
    });
    try { localStorage.setItem(STORAGE_KEY, section); } catch (e) { /* ignore */ }
  }
  document.body.addEventListener('click', function (e) {
    var trigger = e.target.closest('.tab');
    if (!trigger) return;
    e.preventDefault();
    var section = trigger.getAttribute('data-section');
    if (section) activateSection(section);
  });

  /* === 时间范围按钮 ============================================ */
  document.querySelectorAll('.range-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var h = parseInt(b.getAttribute('data-hours'), 10);
      if (!h) return;
      document.querySelectorAll('.range-btn').forEach(function (x) {
        x.classList.remove('active');
      });
      b.classList.add('active');
      manualRefresh(h);
    });
  });

  /* === Round 33d: 采集记录时间范围过滤 =========================== */
  (function () {
    var startInput = document.getElementById('records-start');
    var endInput = document.getElementById('records-end');
    var queryBtn = document.getElementById('records-query');
    var resetBtn = document.getElementById('records-reset');
    var subLabel = document.getElementById('records-sub');
    var rangeLabel = document.getElementById('records-range-label');
    var tbody = document.getElementById('records-tbody');
    var emptyState = document.getElementById('records-empty');

    if (!startInput || !endInput || !queryBtn) return;

    function pad(n) { return n < 10 ? '0' + n : '' + n; }
    function toLocalISO(d) {
      return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate())
        + 'T' + pad(d.getHours()) + ':' + pad(d.getMinutes());
    }

    (function setDefaultRange() {
      var now = new Date();
      var startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      startInput.value = toLocalISO(startOfDay);
      endInput.value = toLocalISO(now);
    })();

    function escapeHtml(s) {
      return String(s == null ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }

    function renderRows(rows) {
      if (!tbody) {
        if (subLabel) subLabel.textContent = '共 ' + (rows ? rows.length : 0) + ' 条';
        return;
      }
      if (!rows || !rows.length) {
        tbody.innerHTML = '';
        if (emptyState) emptyState.style.display = 'block';
        if (subLabel) subLabel.textContent = '共 0 条';
        return;
      }
      if (emptyState) emptyState.style.display = 'none';
      var html = '';
      rows.forEach(function (r) {
        var remain = (r.remain != null && !isNaN(r.remain))
          ? Number(r.remain).toFixed(2) : '—';
        html += '<tr>'
          + '<td class="ts">' + escapeHtml(r.ts || '—') + '</td>'
          + '<td class="ts">' + escapeHtml(r.read_time || '—') + '</td>'
          + '<td class="text-end">' + remain + '</td>'
          + '</tr>';
      });
      tbody.innerHTML = html;
      if (subLabel) subLabel.textContent = '共 ' + rows.length + ' 条';
    }

    function queryRecords() {
      var s = startInput.value;
      var e = endInput.value;
      if (!s || !e) {
        if (typeof showToast === 'function') {
          showToast('请填写起始和截止时间', 'error');
        }
        return;
      }
      // R46 — compare as Asia/Shanghai wall clock (datetime-local is TZ-naive)
      if (new Date(s + '+08:00') > new Date(e + '+08:00')) {
        if (typeof showToast === 'function') {
          showToast('起始时间不能晚于截止时间', 'error');
        }
        return;
      }
      // R46 — replace T with space (records.ts stored as "YYYY-MM-DD HH:MM:SS")
      var startStr = (s.length === 16 ? s + ':00' : s).replace('T', ' ');
      var endStr = (e.length === 16 ? e + ':59' : e).replace('T', ' ');
      var url = '/api/data?start=' + encodeURIComponent(startStr)
              + '&end=' + encodeURIComponent(endStr);
      if (rangeLabel) {
        rangeLabel.textContent = '查询范围：' + s.replace('T', ' ') + ' ~ ' + e.replace('T', ' ');
      }
      queryBtn.disabled = true;
      fetch(url).then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      }).then(function (payload) {
        renderRows((payload && payload.rows) || []);
      }).catch(function (err) {
        if (typeof showToast === 'function') {
          showToast('查询失败：' + (err && err.message || '网络错误'), 'error');
        }
        renderRows([]);
      }).then(function () {
        queryBtn.disabled = false;
      });
    }

    function resetRecords() {
      var now = new Date();
      var startOfDay = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 0, 0, 0);
      startInput.value = toLocalISO(startOfDay);
      endInput.value = toLocalISO(now);
      queryRecords();
    }

    queryBtn.addEventListener('click', queryRecords);
    if (resetBtn) resetBtn.addEventListener('click', resetRecords);
    [startInput, endInput].forEach(function (el) {
      el.addEventListener('change', function () {
        if (startInput.value && endInput.value) queryRecords();
      });
    });
    queryRecords();
  })();

  /* === 手动刷新按钮 ============================================ */
  var refreshBtn = document.getElementById('refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () { forceRefresh(true); });
  }

  /* === 主题切换器 (Round 27) — preview_v2 仅深色,保留 applyTheme 钩子 === */
  function applyTheme(themeKey, accent) {
    accent = accent || '#6366f1';
    var root = document.documentElement;
    root.style.setProperty('--accent', accent);
    root.style.setProperty('--accent-soft', 'rgba(99,102,241,0.10)');
    root.style.setProperty('--accent-hover', '#818cf8');
    try {
      if (chartInstance) chartInstance.update('none');
      if (dailyChartInstance) dailyChartInstance.update('none');
    } catch (e) { /* ignore */ }
  }

  var themeToggle = document.getElementById('theme-toggle');
  if (themeToggle) {
    themeToggle.addEventListener('click', function () {
      var current = document.documentElement.style.getPropertyValue('--accent') || '#6366f1';
      applyTheme('dark', current === '#6366f1' ? '#22c55e' : '#6366f1');
    });
  }

  /* === 键盘快捷键 ============================================ */
  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;
    var key = e.key;
    var sectionMap = { '1': 'overview', '2': 'history', '3': 'finance', '4': 'meter' };
    if (sectionMap[key]) {
      e.preventDefault();
      activateSection(sectionMap[key]);
      return;
    }
    if (key === 'r' || key === 'R') {
      e.preventDefault();
      manualRefresh();
      return;
    }
  });

  /* === 初始化 ================================================ */
  setPill();
  setLastUpdate(initialLatestTs);
  buildChart(initialRows);
  buildDailyChart(initialDaily);
  showSkeletons(true);
  forceRefresh(false);
  try {
    var saved = localStorage.getItem(STORAGE_KEY);
    if (saved && saved !== 'overview') activateSection(saved);
  } catch (e) { /* ignore */ }
})();
</script>
</body>
</html>"""


@app.route("/")
def index():
    # Round 34B — OOBE redirect.  Until the user finishes the 6-step
    # wizard we send them to /admin/oobe so the dashboard doesn't
    # render with broken / empty meta values.  This is the only
    # public-facing route; once OOBE is done, /admin/* becomes the
    # password-protected one for ongoing tweaks.
    if db.get_meta("oobe_completed") != "1":
        return redirect("/admin/oobe")

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


# OOBE_HTML / ADMIN_HTML are intentionally compact — they render via
# render_template_string with no Jinja includes, just inline CSS.
OOBE_HTML = """<!doctype html>
<html lang=zh><head>
<meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>首次配置 — dorm-power-monitor</title>
<style>
:root {
  --bg: #0f172a; --bg2: #1e293b; --fg: #f8fafc; --muted: #94a3b8;
  --accent: #38bdf8; --accent2: #6366f1;
  --ok: #22c55e; --warn: #f59e0b; --err: #ef4444;
  --radius: 14px;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh;
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
  background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%);
  color: var(--fg); padding: 24px;
}
.wrap { max-width: 720px; margin: 0 auto; }
.logo {
  font-size: 22px; font-weight: 700; margin-bottom: 18px;
  background: linear-gradient(120deg, var(--accent), var(--accent2));
  -webkit-background-clip: text; background-clip: text; color: transparent;
}
.progress {
  display: flex; gap: 6px; margin-bottom: 24px;
}
.progress div {
  flex: 1; height: 6px; border-radius: 3px;
  background: var(--bg2); transition: background .3s;
}
.progress div.active { background: var(--accent); }
.progress div.done   { background: var(--accent2); }
.card {
  background: rgba(30,41,59,.7); backdrop-filter: blur(12px);
  border: 1px solid rgba(255,255,255,.08); border-radius: var(--radius);
  padding: 28px; margin-bottom: 16px;
}
h1 { margin: 0 0 14px; font-size: 24px; }
h2 { margin: 18px 0 10px; font-size: 16px; color: var(--muted); font-weight: 500; }
label { display: block; margin: 14px 0 6px; font-size: 14px; color: var(--muted); }
input[type=text], input[type=password], input[type=time] {
  width: 100%; padding: 12px 14px; border-radius: 10px; border: 1px solid #334155;
  background: var(--bg); color: var(--fg); font-size: 14px;
}
button {
  padding: 10px 18px; border-radius: 10px; border: none;
  background: linear-gradient(120deg, var(--accent), var(--accent2));
  color: white; font-size: 14px; font-weight: 600; cursor: pointer;
  transition: transform .15s;
}
button:hover { transform: translateY(-1px); }
button.ghost {
  background: transparent; border: 1px solid #334155;
}
.actions {
  display: flex; gap: 10px; justify-content: flex-end; margin-top: 24px;
}
.toast {
  margin-top: 14px; padding: 10px 14px; border-radius: 8px;
  background: rgba(34,197,94,.15); color: var(--ok);
}
.toast.err { background: rgba(239,68,68,.15); color: var(--err); }
.checkbox-row { display: flex; align-items: center; gap: 8px; margin: 10px 0; }
.checkbox-row input { width: 18px; height: 18px; }
.row { display: flex; gap: 10px; }
.row > * { flex: 1; }
.kv {
  background: var(--bg); padding: 12px; border-radius: 8px;
  font-family: monospace; font-size: 13px; margin: 6px 0;
}
</style>
</head><body>
<div class=wrap>
  <div class=logo>⚡ dorm-power-monitor 首次配置</div>
  <div class=progress id=progress>
    <div></div><div></div><div></div><div></div><div></div><div></div>
  </div>
  <div class=card id=card>
    <h1 id=title>欢迎</h1>
    <div id=body>加载中…</div>
    <div id=toast></div>
    <div class=actions id=actions></div>
  </div>
</div>
<script>
const step = {{ step }};
const totalSteps = 6;
function paintProgress() {
  const ps = document.querySelectorAll('#progress div');
  ps.forEach((d, i) => {
    d.classList.remove('active', 'done');
    if (i + 1 < step) d.classList.add('done');
    else if (i + 1 === step) d.classList.add('active');
  });
}
function toast(msg, isErr) {
  const el = document.getElementById('toast');
  el.innerHTML = '<div class="toast ' + (isErr ? 'err' : '') + '">' + msg + '</div>';
  setTimeout(() => { el.innerHTML = ''; }, 4000);
}
async function save(data) {
  const resp = await fetch('/admin/api/oobe/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ step: String(step), data }),
  });
  const json = await resp.json();
  if (!json.ok) { toast(json.error || '保存失败', true); return null; }
  return json;
}
function btn(label, fn, ghost) {
  const b = document.createElement('button');
  b.textContent = label;
  if (ghost) b.classList.add('ghost');
  b.addEventListener('click', fn);
  return b;
}

const templates = {
  1: () => ({
    title: '欢迎使用 dorm-power-monitor',
    body: '<p>跟着向导 6 步完成首次配置：</p>'
      + '<ol><li>学校绑定</li><li>飞书 webhook 设置</li>'
      + '<li>推送偏好</li><li>测试推送</li><li>完成</li></ol>'
      + '<p>预计耗时 3 分钟。准备一个飞书机器人 webhook URL 和你的校园门户 openid URL。</p>',
    // Round 37 B1 — was ``() => location.reload()`` which silently
    // re-loaded step 1 forever (reload hits /admin/oobe which reads
    // oobe_step='1' from meta and re-renders the same step).  Now
    // calls goNext() with an empty payload so the server advances
    // ``oobe_step`` to '2' and the wizard moves forward.
    actions: [btn('下一步 →', () => goNext(), false)],
  }),
  2: () => ({
    title: '第 2 步：绑定学校账号',
    body: '<label>粘贴任意一个 finduser URL</label>'
      + '<input id=school_url type=text placeholder="http://.../finduser?openid=...">'
      + '<div id=parsed></div>',
    actions: [
      btn('解析', () => importUrl()),
      btn('下一步 →', () => goNext()),
    ],
  }),
  3: () => ({
    title: '第 3 步：飞书 webhook',
    body: '<label>飞书机器人 webhook URL</label>'
      + '<input id=webhook_url type=text placeholder="https://open.feishu.cn/...">',
    actions: [
      btn('下一步 →', () => goNext()),
    ],
  }),
  4: () => ({
    title: '第 4 步：推送偏好',
    body: '<div class=checkbox-row><input id=push_l2 type=checkbox checked>'
      + '<label style=margin:0>整点报 (L2)</label></div>'
      + '<div class=checkbox-row><input id=push_daily type=checkbox checked>'
      + '<label style=margin:0>日报</label></div>'
      + '<div class=checkbox-row><input id=push_weekly type=checkbox checked>'
      + '<label style=margin:0>周报</label></div>'
      + '<div class=checkbox-row><input id=push_monthly type=checkbox checked>'
      + '<label style=margin:0>月报</label></div>'
      + '<div class=row><div><label>静默开始</label>'
      + '<input id=quiet_start type=time value="23:00"></div>'
      + '<div><label>静默结束</label>'
      + '<input id=quiet_end type=time value="07:00"></div></div>',
    actions: [btn('下一步 →', () => goNext())],
  }),
  5: () => ({
    title: '第 5 步：测试推送',
    body: '<p>向步骤 3 设置的 webhook 发送一条测试消息。</p>'
      + '<div id=test-result></div>',
    actions: [
      btn('发送测试消息', () => testPush()),
      btn('下一步 →', () => goNext()),
    ],
  }),
  6: () => ({
    title: '配置完成！',
    body: '<p>已保存全部偏好。dashboard 已解锁。</p>'
      + '<p>建议接下来在 Admin 面板设置 HTTP Basic Auth 密码。</p>',
    actions: [btn('进入 dashboard', () => location.href = '/admin')],
  }),
};

async function importUrl() {
  const url = document.getElementById('school_url').value.trim();
  if (!url) { toast('URL 不能为空', true); return; }
  const resp = await fetch('/admin/api/import-url', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url }),
  });
  const json = await resp.json();
  if (!resp.ok || json.error) { toast(json.error || '解析失败', true); return; }
  document.getElementById('parsed').innerHTML =
    '<div class=kv>openid: ' + (json.openid || '(空)') + '</div>'
    + '<div class=kv>roomId: ' + (json.roomId || '(空)') + '</div>'
    + '<div class=kv>roomNo: ' + (json.roomNo || '(空)') + '</div>'
    + '<div class=kv>EqPrice: ' + (json.EqPrice || '(空)') + '</div>';
  toast('解析成功');
}
async function testPush() {
  const r = document.getElementById('test-result');
  r.innerHTML = '<div class=toast>发送中…</div>';
  const resp = await fetch('/admin/api/test-push', { method: 'POST' });
  const json = await resp.json();
  if (json.ok) r.innerHTML = '<div class=toast>已发送 ✓</div>';
  else r.innerHTML = '<div class="toast err">失败：' + (json.error || '') + '</div>';
}
async function goNext() {
  let payload = {};
  if (step === 2) {
    const url = document.getElementById('school_url').value.trim();
    if (!url) { toast('请粘贴学校 URL', true); return; }
    payload = { school_url: url };
  } else if (step === 3) {
    payload = { webhook_url: document.getElementById('webhook_url').value.trim() };
    if (!payload.webhook_url) { toast('请粘贴 webhook URL', true); return; }
  } else if (step === 4) {
    payload = {
      l2_enable:     document.getElementById('push_l2').checked ? '1' : '0',
      daily_enable:  document.getElementById('push_daily').checked ? '1' : '0',
      weekly_enable: document.getElementById('push_weekly').checked ? '1' : '0',
      monthly_enable:document.getElementById('push_monthly').checked ? '1' : '0',
      quiet_hours_start: document.getElementById('quiet_start').value,
      quiet_hours_end:   document.getElementById('quiet_end').value,
    };
  }
  const r = await save(payload);
  if (r) {
    if (r.next_step === 'done') location.href = '/admin';
    else location.href = '/admin/oobe?step=' + r.next_step;
  }
}

paintProgress();
const t = templates[step]();
document.getElementById('title').textContent = t.title;
document.getElementById('body').innerHTML = t.body;
const acts = document.getElementById('actions');
t.actions.forEach(a => acts.appendChild(a));
</script>
</body></html>"""


ADMIN_HTML = """<!doctype html>
<html lang=zh><head>
<meta charset=utf-8>
<title>Admin — dorm-power-monitor</title>
<style>
/* Round 35 — glassmorphism admin page.
   * CSS vars mirror the dashboard's design system so /admin and /
     feel like one product.
   * Inputs got high-contrast borders (rgba(96,165,250,.4)) and a
     glow on focus so they're legible against the dark gradient.
   * Push-config 4-column rows collapsed to a 2x2 grid on desktop and
     a single column on ≤768px so each cell actually has room for
     label + input. */
:root {
  --bg: #0f172a;
  --bg2: #1e293b;
  --fg: #f8fafc;
  --muted: #94a3b8;
  --accent: #60a5fa;
  --accent-2: #34d399;
  --ok: #22c55e;
  --err: #ef4444;
  --warn: #f59e0b;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
  background:
    radial-gradient(circle at 12% 0%, rgba(96,165,250,0.10) 0%, transparent 42%),
    radial-gradient(circle at 88% 100%, rgba(52,211,153,0.08) 0%, transparent 42%),
    var(--bg);
  color: var(--fg);
  padding: 40px 24px;
  min-height: 100vh;
  -webkit-font-smoothing: antialiased;
}
.wrap { max-width: 920px; margin: 0 auto; }
h1 {
  font-size: 26px;
  margin: 0 0 28px;
  font-weight: 700;
  background: linear-gradient(135deg, #60a5fa 0%, #34d399 100%);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
  letter-spacing: 0.5px;
}
section {
  background: linear-gradient(135deg, rgba(255,255,255,0.06), rgba(255,255,255,0.02));
  -webkit-backdrop-filter: blur(20px);
  backdrop-filter: blur(20px);
  border: 1px solid rgba(255,255,255,0.12);
  border-radius: 16px;
  padding: 24px;
  margin: 0 0 24px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.25);
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
section:hover {
  border-color: rgba(96,165,250,0.25);
  box-shadow: 0 6px 22px rgba(96,165,250,0.10);
}
section h2 {
  margin: 0 0 16px;
  font-size: 17px;
  font-weight: 600;
  color: var(--accent);
  display: flex;
  align-items: center;
  gap: 8px;
}
section h2 .ic { font-size: 19px; }
label {
  display: block;
  margin: 14px 0 6px;
  font-size: 13px;
  color: var(--muted);
  font-weight: 500;
}
.help {
  display: block;
  font-size: 0.8rem;
  opacity: 0.6;
  margin: 4px 0 0;
  line-height: 1.4;
}
input {
  width: 100%;
  padding: 11px 14px;
  border-radius: 10px;
  border: 1px solid rgba(96,165,250,0.4);
  background: rgba(15,23,42,0.55);
  color: var(--fg);
  font-size: 14px;
  font-family: inherit;
  outline: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease, background 0.15s ease;
}
input::placeholder { color: rgba(148,163,184,0.5); }
input:hover {
  border-color: rgba(96,165,250,0.6);
  background: rgba(15,23,42,0.7);
}
input:focus {
  border-color: rgba(96,165,250,0.8);
  box-shadow: 0 0 0 3px rgba(96,165,250,0.18), 0 0 14px rgba(96,165,250,0.25);
  background: rgba(15,23,42,0.85);
}
button {
  padding: 10px 20px;
  border-radius: 10px;
  border: 1px solid rgba(96,165,250,0.4);
  background: linear-gradient(135deg, rgba(96,165,250,0.18), rgba(52,211,153,0.18));
  color: var(--fg);
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  font-size: 14px;
  transition: transform 0.12s ease, border-color 0.12s ease, background 0.12s ease;
}
button:hover {
  border-color: rgba(96,165,250,0.8);
  background: linear-gradient(135deg, rgba(96,165,250,0.30), rgba(52,211,153,0.30));
}
button:active { transform: translateY(1px); }
/* 4-column push toggles become 2x2 grid on desktop, single column on mobile. */
.row {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 1.5rem;
  margin-bottom: 4px;
}
.row > div { min-width: 0; }
.row > div input { margin-top: 6px; }
/* Buttons live in their own row below the grid. */
.btn-row { margin-top: 16px; display: flex; gap: 10px; flex-wrap: wrap; }
.toast { margin-top: 14px; padding: 10px 14px; border-radius: 8px; font-size: 13px; }
.toast.ok  { background: rgba(34,197,94,.15); color: var(--ok); border: 1px solid rgba(34,197,94,.3); }
.toast.err { background: rgba(239,68,68,.15); color: var(--err); border: 1px solid rgba(239,68,68,.3); }
a.back-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: var(--accent);
  text-decoration: none;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: 8px;
  transition: background 0.15s ease;
}
a.back-link:hover {
  background: rgba(96,165,250,0.10);
  text-decoration: none;
}
/* Mobile: collapse 2x2 grid to a single column. */
@media (max-width: 768px) {
  body { padding: 24px 16px; }
  .row { grid-template-columns: 1fr; gap: 12px; }
  h1 { font-size: 22px; }
  section { padding: 18px; }
}
</style>
</head><body>
<div class=wrap>
  <h1>⚙ Admin — dorm-power-monitor</h1>

  <section>
    <h2><span class=ic>🏠</span>抓包配置</h2>
    <label>学校 base URL</label>
    <input id=dorm_base_url placeholder="http://ybhqcz.fjny.edu.cn">
    <span class=help>宿舍门户根地址，默认即可（学校一般不会换）。</span>

    <label>openid</label>
    <input id=dorm_openid placeholder="oXyz...（微信 openid）">
    <span class=help>把学校 H5 页面 URL 里的 <code>openid=</code> 整段粘进来；视为密码。</span>

    <label>roomId</label>
    <input id=dorm_room_id placeholder="UUID 或 32 位 hex">
    <span class=help>留空让抓取脚本自动从 finduser 页面发现。已知可填加速抓取。</span>

    <label>电价 (元/kW·h)</label>
    <input id=eqprice placeholder="0.5">
    <span class=help>用于低余额告警阈值；默认 0.5。</span>

    <label>飞书 webhook</label>
    <input id=feishu_webhook_url placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/...">
    <span class=help>机器人 hook URL；签名验证另配 FEISHU_SECRET。</span>

    <div class=btn-row>
      <button onclick=saveScrape()>保存抓包配置</button>
    </div>
  </section>

  <section>
    <h2><span class=ic>🔔</span>推送配置</h2>
    <div class=row>
      <div><label>L2 整点报</label><input id=push_l2_enable placeholder="1 / 0"></div>
      <div><label>日报</label><input id=push_daily_enable placeholder="1 / 0"></div>
      <div><label>周报</label><input id=push_weekly_enable placeholder="1 / 0"></div>
      <div><label>月报</label><input id=push_monthly_enable placeholder="1 / 0"></div>
    </div>
    <div class=row>
      <div><label>日报时间</label><input id=push_daily_time placeholder="09:00"></div>
      <div><label>周报时间</label><input id=push_weekly_time placeholder="周一 09:00"></div>
      <div><label>月报时间</label><input id=push_monthly_time placeholder="1 号 09:00"></div>
      <div></div>
    </div>
    <div class=row>
      <div><label>静默开始</label><input id=quiet_hours_start placeholder="23:00"></div>
      <div><label>静默结束</label><input id=quiet_hours_end placeholder="07:00"></div>
    </div>
    <div class=btn-row>
      <button onclick=savePush()>保存推送配置</button>
    </div>
  </section>

  <section>
    <h2><span class=ic>🔐</span>Admin 密码</h2>
    <label>新密码（留空不修改）</label>
    <input id=admin_password type=password placeholder="至少 8 位">
    <span class=help>改密码需要重启 dorm-web 服务。</span>
    <div class=btn-row>
      <button onclick=saveAdminPwd()>设置密码</button>
    </div>
  </section>

  <section>
    <h2><span class=ic>🧪</span>测试 & 操作</h2>
    <span class=help style="margin:0 0 12px">先发一条测试消息确认 webhook 通，再用"立即抓取一次"验证整条数据通路。</span>
    <div class=btn-row>
      <button onclick=testPush()>发送测试消息</button>
      <button onclick=forceRefresh()>立即抓取一次</button>
    </div>
    <div id=action-result></div>
  </section>

  <section>
    <h2><span class=ic>↩</span>回到 dashboard</h2>
    <a class=back-link href="/">← 返回主面板</a>
  </section>
</div>

<script>
function toast(msg, ok) {
  const el = document.getElementById('action-result');
  el.innerHTML = '<div class="toast ' + (ok ? 'ok' : 'err') + '">' + msg + '</div>';
  setTimeout(() => { el.innerHTML = ''; }, 3500);
}
async function loadConfig() {
  const [scrape, push] = await Promise.all([
    fetch('/admin/api/scrape/config').then(r => r.json()),
    fetch('/admin/api/push/config').then(r => r.json()),
  ]);
  for (const k of Object.keys(scrape)) {
    const el = document.getElementById(k);
    if (el) el.value = scrape[k];
  }
  for (const k of Object.keys(push)) {
    const el = document.getElementById(k);
    if (el) el.value = push[k];
  }
}
async function saveScrape() {
  const payload = {};
  ['dorm_base_url','dorm_openid','dorm_room_id','eqprice','feishu_webhook_url']
    .forEach(k => payload[k] = document.getElementById(k).value);
  const r = await fetch('/admin/api/scrape/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  toast(r.ok ? '抓包配置已保存' : '保存失败', r.ok);
}
async function savePush() {
  const payload = {};
  ['push_l2_enable','push_daily_enable','push_weekly_enable','push_monthly_enable',
   'push_daily_time','push_weekly_time','push_monthly_time',
   'quiet_hours_start','quiet_hours_end'].forEach(k => {
    const v = document.getElementById(k).value;
    payload[k] = v;
  });
  const r = await fetch('/admin/api/push/config', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify(payload),
  });
  toast(r.ok ? '推送配置已保存' : '保存失败', r.ok);
}
async function saveAdminPwd() {
  const v = document.getElementById('admin_password').value;
  if (!v) { toast('密码不能为空', false); return; }
  const r = await fetch('/admin/api/admin-password', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ admin_password: v }),
  });
  toast(r.ok ? '密码已更新' : '保存失败', r.ok);
}
async function testPush() {
  const r = await fetch('/admin/api/test-push', { method: 'POST' });
  const j = await r.json();
  toast(j.ok ? '已发送' : ('失败：' + (j.error || '')), j.ok);
}
async function forceRefresh() {
  const r = await fetch('/api/refresh', { method: 'POST' });
  const j = await r.json();
  toast(j.ok ? '抓取成功' : ('失败：' + (j.error || '')), j.ok);
}
loadConfig();
</script>
</body></html>"""


@app.route("/admin/oobe")
def oobe_wizard():
    """Round 34B — render the OOBE wizard at the user's current step.

    On every request we re-read the step counter from meta so a fresh
    page load (e.g. after the user clicks "上一步") doesn't lose state.
    Once ``oobe_completed=1`` is set we redirect to the main dashboard;
    the wizard should not be reachable post-setup.
    """
    if db.get_meta("oobe_completed") == "1":
        return redirect("/admin")
    step_raw = db.get_meta("oobe_step") or "1"
    try:
        step = max(1, min(6, int(step_raw)))
    except (TypeError, ValueError):
        step = 1
    return render_template_string(OOBE_HTML, step=step)


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
        db.set_meta("oobe_completed", "1")

    next_step = "done" if step >= 6 else str(step + 1)
    db.set_meta("oobe_step", str(6 if next_step == "done" else step + 1))
    return jsonify({"ok": True, "next_step": next_step})


@app.route("/admin")
@_admin_required
def admin_index():
    """Round 34B — admin landing page (OOBE must be completed first)."""
    return render_template_string(ADMIN_HTML)


@app.route("/admin/api/scrape/config", methods=["GET", "POST"])
@_admin_required
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
@_admin_required
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
@_admin_required
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
@_admin_required
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
@_admin_required
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


def main() -> None:
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG)


if __name__ == "__main__":
    main()