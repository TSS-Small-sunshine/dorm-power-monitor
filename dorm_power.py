"""dorm_power.py — Round 43 documented.

Headless scraper that drives the school's WeChat-embedded H5 portal and
persists every observation to ``records.db`` via ``db`` helpers.  It is
the only module that knows about the school's HTML / JSON endpoints;
``feishu_bot`` and ``web`` consume the resulting rows.

主要功能 / Key responsibilities:
  * Discover the ``roomId`` UUID once per scrape via the ``finduser``
    GET endpoint (Round 25) — when ``DORM_ROOM_ID`` is set in ``.env``
    the discover step is skipped.
  * POST the 5 form-encoded data endpoints (record history, daily usage,
    violations, live meter snapshot, pay history) and normalise their
    JSON.
  * Drive low-balance / offline / violation alerts into Feishu.
  * Persist all rows via ``db`` helpers; cache eqprice in ``meta`` on
    every ``finduser`` scrape.
  * Provide ``run_once()`` for both cron (every 10 min) and the manual
    ``/api/refresh`` Flask route.

数据流 / Data flow:
  cron / /api/refresh -> dorm_power.run_once -> school portal (HTTP)
    -> db helpers -> records.db  (side-effect: Feishu alerts via
    feishu_bot)

依赖 / Dependencies:
  * stdlib: ``json``, ``logging``, ``re``, ``urllib.parse`` …
  * 3rd party: ``requests``
  * Local: ``config``, ``db``, ``feishu_bot``

Round history:
  * R0   - scaffold + finduser + selectRecord
  * R2   - 5 endpoint expansion + EqPrice cache
  * R3   - Feishu alert push wired
  * R25  - roomId dynamic discovery (no env override required)
  * R34A - meta cached values + lazy getters
  * R34D - cleaner error reporting
  * R36   - db.insert 3->2 参数迁移
  * R37   - OOBE B1-B5+B8 业务代码 (FEISHU_APP_ID setup / etc.)
  * R39   - backfill robustness (partial-failure retry)
  * R41   - monthly_projection 算法 (cumulative-meter fix)
  * R42   - eqprice fallback chain on the read side too

The original per-endpoint docstring follows below for reference.

---

Dorm electricity scraper.

The portal is a thin WeChat-embedded H5 page: the only "auth" is the
`openid` query-string parameter.  This script:

  1. (optionally) GETs the shell page once per scrape to discover the
     `roomId` UUID and the human-readable room label.
  2. POSTs a form-encoded body ``roomId=<uuid>`` to the real data API.
     The portal's embedded JS uses jQuery ``$.post``, which defaults to
     ``Content-Type: application/x-www-form-urlencoded``; we mirror that
     encoding (a JSON body is rejected with ``status == -1``).
  3. Persists the result in SQLite and posts a Feishu card.

Round 2 added five more POST endpoints and a few cached state values:

  * ``/dormEmQuery/selectRecord``       — historical backfill (one-shot)
  * ``/dormEmDayElectQuery/...``        — per-day usage (F2, daily)
  * ``/dormEmWgQuery/selectWgElect``    — violation records (F3, hourly)
  * ``/dormEmRunStatus/getEmRunStatus`` — live meter snapshot (F4, every scrape)
  * ``/dormEmPayQuery/getEmPayQuery``   — pay history (F5, daily)
  * ``/dormEmRealRead/finduser`` (GET)  — also caches the ``EqPrice`` value
                                          from the hidden ``<input id="EqPrice">``
                                          the first time it is visited.

All new POSTs go through a single private helper ``_post_form`` so
signing / Referer / UA / Origin / X-Requested-With handling stays in
one place.

Run by cron (see ``deploy/dorm-cron.txt``).  Safe to run manually with
``python dorm_power.py``.

Security notes (read these before changing the file):

* ``DORM_OPENID`` is a secret.  It is treated like a password by the
  portal.  This file is careful to never log it — neither in the URL
  nor in any error message.  The log line after a successful scrape
  prints only the data-API path and the last 4 characters of the
  roomId.
* The full request URL (which contains the openid) is built inside a
  helper and never passed to ``logger``.  Network errors are re-raised
  with a sanitized message that strips the query string.
"""
from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import requests

import config
import db

logger = logging.getLogger("dorm_power")

# WeChat in-app browser User-Agent.  The portal rejects requests that
# don't look like they're coming from inside the WeChat client.
_WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "MicroMessenger/8.0.49(0x18003130) NetType/WIFI Language/zh_CN"
)


# ---------------------------------------------------------------------------
# Round 26a — Session connection pool + retry-with-backoff
# ---------------------------------------------------------------------------
# One module-level ``Session`` is reused across every _fetch_* call so the
# underlying urllib3 connection pool keeps the TCP/TLS handshake alive
# between scrapes.  The default pool size (10) is plenty for a single
# cron tick that fires <10 requests back-to-back.
_SESSION = requests.Session()
_SESSION.headers.update({
    # Tells the school's nginx we're a "decent" HTTP/1.1 client.  Many
    # IIS-fronted portals 502-spam curl/python-requests UAs that look
    # like scripts.
    "User-Agent": ("dorm-power-monitor/1.0 (+https://github.com/) "
                   "python-requests"),
    "Accept": "text/html,application/x-www-form-urlencoded,application/json,*/*",
})


def _find_http_cause(exc: BaseException) -> Optional[requests.HTTPError]:
    """Walk the ``__cause__`` / ``__context__`` chain for an HTTPError."""
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, requests.HTTPError):
            return cur
        cur = cur.__cause__ or cur.__context__
    return None


def _find_transport_cause(exc: BaseException) -> bool:
    """True when ``exc`` (or any chain link) is a retryable transport error.

    We treat ``ConnectionError`` and ``Timeout`` as "yes, retry" and
    HTTPError as "look at the status code" — see retry_school for the
    actual status semantics.
    """
    seen: set[int] = set()
    cur: Optional[BaseException] = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, (requests.ConnectionError, requests.Timeout)):
            return True
        if isinstance(cur, requests.RequestException):
            # Found a request exception that isn't a connection/timeout;
            # don't keep walking (we've reached the deepest transport
            # layer).
            return False
        cur = cur.__cause__ or cur.__context__
    return False


def retry_school(max_attempts: int = 3,
                 backoff: tuple[float, ...] = (0.5, 1.0, 2.0)) -> Callable:
    """Decorator: retry on transport errors + HTTP 429/5xx, honour Retry-After.

    Plain 4xx (except 429) is **not** retried — those are bugs the
    caller has to fix, not transient failures.  The backoff sequence is
    fixed-ish (0.5s / 1s / 2s) so the total worst-case cost stays under
    5s for one endpoint; we don't use ``tenacity`` for one decorator.

    ``max_attempts`` is the total attempt budget including the first
    try, so the default 3 means "try, retry-once, retry-twice".

    Note on the catch list: ``_fetch_data`` / ``_post_form`` wrap
    ``raise_for_status`` in a sanitised ``RuntimeError`` (so the
    openid never leaks into log lines).  We therefore catch the broad
    ``Exception`` superclass and look at the underlying
    ``__cause__`` / ``.response`` chain to decide whether to retry.
    Other (non-transport) exceptions still bubble up unchanged.
    """
    def deco(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrap(*args, **kwargs):
            last_exc: Optional[BaseException] = None
            for i in range(max_attempts):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    # Non-request exceptions (programming bugs, JSON
                    # parse errors, etc.) must NOT be retried — they
                    # are deterministic and will fail again.  We allow
                    # any exception whose __cause__ is request-related
                    # through, because _fetch_data / _post_form wrap
                    # transport/HTTP failures as sanitised RuntimeErrors.
                    is_request_typed = (
                        isinstance(e, requests.RequestException)
                        or _find_http_cause(e) is not None
                        or _find_transport_cause(e)
                    )
                    if not is_request_typed:
                        raise
                    last_exc = e
                    http_exc = _find_http_cause(e)
                    resp = (getattr(http_exc, "response", None)
                            if http_exc else None) or getattr(
                        e, "response", None
                    )
                    status = getattr(resp, "status_code", None)
                    is_last = i == max_attempts - 1
                    if status == 429:
                        try:
                            ra = float(resp.headers.get("Retry-After",
                                                        backoff[i]))
                        except (TypeError, ValueError, IndexError):
                            ra = backoff[i]
                        if not is_last:
                            time.sleep(min(ra, 5))
                    elif status is None:
                        # Pure transport (Timeout / ConnectionError) or
                        # unknown — always retryable.
                        if not is_last:
                            time.sleep(backoff[i] if i < len(backoff) else backoff[-1])
                    elif 400 <= status < 500:
                        # 4xx (except 429) is a code-level bug — don't
                        # hammer the upstream.
                        raise
                    else:
                        # 5xx — sleep & retry.
                        if not is_last:
                            time.sleep(backoff[i] if i < len(backoff) else backoff[-1])
            # Exhausted retries; re-raise the last error verbatim so the
            # caller (run_once / _backfill_once) can do its own logging.
            assert last_exc is not None
            raise last_exc
        return wrap
    return deco

# The two paths we talk to.  Kept as module constants so log lines can
# reference the path without ever holding the openid.
_FINDUSER_PATH = "/campus/webchat/dormEmRealRead/finduser"
_DATA_PATH = "/campus/webchat/dormEmRealRead/getEmRealRead"

# Round 2 — the five new POST endpoints.  All form-encoded, all reusing
# the finduser Referer.  The single POST helper `_post_form` is the only
# place these URLs are stitched together with the openid.
_PATH_SELECT_RECORD     = "/campus/webchat/dormEmQuery/selectRecord"
_PATH_DAY_ELECT         = "/campus/webchat/dormEmDayElectQuery/getEmDayElectQuery"
_PATH_WG_ELECT          = "/campus/webchat/dormEmWgQuery/selectWgElect"
_PATH_RUN_STATUS        = "/campus/webchat/dormEmRunStatus/getEmRunStatus"
_PATH_PAY_QUERY         = "/campus/webchat/dormEmPayQuery/getEmPayQuery"

# Regexes used to pull hidden inputs and the room label out of the
# thin shell page.  The page is a one-liner with a handful of <input>
# tags, so a single regex per element is cheaper than pulling in an
# HTML parser.
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_INPUT_ATTR_RE = re.compile(
    r"""(?P<key>type|id|value)\s*=\s*["'](?P<val>[^"']*)["']""",
    re.IGNORECASE,
)
# Round 34A — B2 fix: the finduser HTML exposes the room label as
# ``<input type="text" id="roomNo" value="6号楼-1-119">`` (attribute
# value), NOT as an inner text node.  The previous regex required
# ``<...id="roomNo"...>文本</...>`` and so ``room_label`` was always
# ``None``, which made the Feishu card title fall back to the generic
# "宿舍电量监控".  This regex accepts both attribute orders
# (``id`` before ``value`` OR ``value`` before ``id``) so a future
# reordering of the school's HTML can't silently break the title
# again.  Group 1 is the value capture in both alternations; we use
# ``m.group(1) or m.group(2)`` in ``_discover_room``.
_ROOM_NO_RE = re.compile(
    r'<input[^>]*\bid=["\']roomNo["\'][^>]*\bvalue=["\']([^"\']+)["\']'
    r'|'
    r'<input[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bid=["\']roomNo["\']',
    re.IGNORECASE,
)
# Round 34A — B1 fix: the school HTML never carries an ``<input
# id="EqPrice">`` field (verified by web_fetch; the docstring above
# was lying).  EqPrice is now sourced from meta with an env fallback,
# so this constant is no longer needed but is kept as a historical
# breadcrumb in case a future scraper revives the HTML parser.
# _EQPRICE_ID = "eqprice"   # REMOVED — see _read_eqprice()

# Header color thresholds for the "remaining electricity" gauge.  A
# glance at the colored header bar is enough to tell how urgent a
# recharge is.  The bounds are strict less-than; tweak freely.
_REMAIN_RED_BELOW    = 30    # remain  < 30   -> red
_REMAIN_ORANGE_BELOW = 80    # remain  < 80   -> orange
_REMAIN_BLUE_BELOW   = 200   # remain  < 200  -> blue
                             # remain >= 200  -> green

# ---------------------------------------------------------------------------
# Round 2 — meta-key constants and alert timing
# ---------------------------------------------------------------------------
# Every cached/scraped-at timestamp and dedupe key lives in `meta`.  We
# keep them as module-level constants so a typo doesn't silently split
# the keyspace in two.
_META_BACKFILL_DONE        = "backfill_done"            # "1" once historical import has run
_META_EQPRICE              = "eqprice"                  # cached y/kW·h value
_META_LAST_SCRAPE_AT       = "last_scrape_at"            # ISO ts of the last successful scrape
_META_LAST_DAILY_ELECT_AT  = "last_daily_elect_at"      # F2 cadence gate
_META_LAST_VIOLATION_AT    = "last_violation_check_at"  # F3 cadence gate
_META_LAST_PAY_AT          = "last_pay_at"              # F5 cadence gate
_META_LAST_VIOLATION_ALERT = "last_violation_alert_at"  # dedupe for the violation push
_META_LAST_LOWBATT_ALERT   = "last_low_battery_alert_at"
_META_LAST_STALE_ALERT     = "last_stale_alert_at"

# Round 34A — L3 daily / weekly / monthly report cadence.  These are
# separate from the L1 (10-min scrape) and L2 (every-tick summary
# card) cadence; they fire on a once-a-day / once-a-week /
# once-a-month schedule controlled by the * * * * * cron line in
# ``deploy/dorm-cron.txt``.  The ``_l3_due`` / ``_stamp_l3`` /
# ``_read_push_time`` triple makes each L3 push idempotent so a
# missed tick (e.g. the box was off at 09:00) can be safely
# retried — we only stamp *after* a successful push.
_META_LAST_DAILY_REPORT    = "last_daily_report_date"      # "YYYY-MM-DD" (BJ date)
_META_LAST_WEEKLY_REPORT   = "last_weekly_report_iso"      # "YYYY-Www" (BJ ISO week)
_META_LAST_MONTHLY_REPORT  = "last_monthly_report_mo"      # "YYYY-MM"   (BJ month)
_META_PUSH_DAILY_TIME      = "push_daily_time"             # "HH:MM" BJ
_META_PUSH_WEEKLY_TIME     = "push_weekly_time"            # "HH:MM" BJ
_META_PUSH_MONTHLY_TIME    = "push_monthly_time"           # "HH:MM" BJ
_META_PUSH_DAILY_ENABLE    = "push_daily_enable"           # "1" / "0"
_META_PUSH_WEEKLY_ENABLE   = "push_weekly_enable"          # "1" / "0"
_META_PUSH_MONTHLY_ENABLE  = "push_monthly_enable"         # "1" / "0"

# Round 26a — degraded-mode (stale) tracking.  ``last_scrape_status``
# is one of "ok" / "stale" / "failed"; the dashboard reads it via
# /api/live so the Pillow card can show a ⚠ marker when the last
# successful run is too old to be trusted.
_META_LAST_SCRAPE_STATUS   = "last_scrape_status"

# Cadence (in seconds).  F1 (live dormEmRealRead) and F4 (run_status) run
# every tick; F2/F5 are once per day, F3 once per hour.
_F2_INTERVAL_SEC = 24 * 3600
_F3_INTERVAL_SEC = 3600
_F5_INTERVAL_SEC = 24 * 3600

# If `last_scrape_at` is older than this when `run_once()` starts, push
# a "scraper hasn't run in 2h+" warning.  Note: this only fires when
# the scraper itself is running; if the box is fully down the user
# gets no notification (see README "故障排查").
_STALE_SCRAPE_GAP_SEC = 2 * 3600

# Violation-push rate limit.  Even if a new violation appears every 10
# min, we don't push more than once every 30 min.
_VIOLATION_ALERT_COOLDOWN_SEC = 30 * 60

# Strings the school uses to mark the meter as "online".  Anything else
# (and in particular the absence of `runStatus`) is treated as offline.
_ONLINE_LABELS = {"在线", "正常", "通讯正常"}


# ---------------------------------------------------------------------------
# Scraping — helpers
# ---------------------------------------------------------------------------

def _validate_openid() -> str:
    """Return the openid from the environment, raising if it's missing.

    The openid is the only credential the portal accepts.  This
    function only returns a non-empty value; callers MUST never log
    the return value.
    """
    openid = (os.getenv("DORM_OPENID") or "").strip()
    if not openid:
        raise RuntimeError(
            "DORM_OPENID is empty. Set it in .env (or the systemd "
            "EnvironmentFile). The openid is the value of the `openid` "
            "query string on the dorm portal's finduser page."
        )
    return openid


def _build_url(path: str, openid: str) -> str:
    """Join base URL + path + openid as a query string.

    The returned URL contains the openid and is therefore NEVER logged.
    Use ``urlparse(...).path`` if you need a safe-to-log path token.
    """
    base = config.DORM_BASE_URL.rstrip("/")
    return f"{base}{path}?openid={openid}"


def _safe_path(url: str) -> str:
    """Return just the path component of ``url`` (no query string).

    Used in error messages so we can mention where the request failed
    without leaking the openid.
    """
    try:
        return urlparse(url).path or "/"
    except Exception:  # pragma: no cover - urlparse is very forgiving
        return "/"


def _safe_room_tail(room_id: str) -> str:
    """Return the last 4 characters of the roomId for log lines."""
    if not room_id:
        return "????"
    return room_id[-4:]


def _parse_input_attrs(tag: str) -> dict[str, str]:
    """Parse a single ``<input ...>`` tag into a dict of attribute names."""
    attrs: dict[str, str] = {}
    for m in _INPUT_ATTR_RE.finditer(tag):
        attrs[m.group("key").lower()] = m.group("val")
    return attrs


def _discover_room(html: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(room_id, room_label)`` parsed from the shell page.

    ``room_id`` is the UUID the data API expects; ``room_label`` is the
    human-readable label (e.g. ``6号楼-1-119``) and is best-effort.
    """
    room_id: Optional[str] = None
    for tag in _INPUT_TAG_RE.finditer(html):
        attrs = _parse_input_attrs(tag.group(0))
        if attrs.get("type", "").lower() != "hidden":
            continue
        if attrs.get("id", "").lower() == "roomid":
            room_id = attrs.get("value") or None
    room_label: Optional[str] = None
    m = _ROOM_NO_RE.search(html)
    if m:
        # Round 34A — B2 fix: ``_ROOM_NO_RE`` has two alternations
        # (id-then-value OR value-then-id) so the value may land in
        # group 1 or group 2 depending on the attribute order in
        # the source HTML.  Pick the non-empty one.
        raw_label = m.group(1) or m.group(2)
        if raw_label:
            room_label = raw_label.strip() or None
    return room_id, room_label


def _read_eqprice() -> float:
    """Return the configured electricity price in 元/kW·h.

    Round 34A — was previously discovered from the school's HTML
    ``EqPrice`` hidden input (which never actually exists in the
    finduser HTML — see audit B1).  Now sourced from ``meta.eqprice``
    (cached on first successful scrape under ``_META_EQPRICE``)
    with an env fallback (``DORM_EQPRICE``), default 0.5 — matches
    the 福建职业技术学院 dorm rate.

    Order of resolution:
      1. ``db.get_meta("eqprice")`` if cached,
      2. ``os.environ.get("DORM_EQPRICE")``,
      3. hard default ``0.5``.

    Any non-parseable value silently falls back to ``0.5`` so a
    broken cached value can never crash the cron.
    """
    raw = db.get_meta(_META_EQPRICE)
    if raw is None or raw == "":
        raw = os.environ.get("DORM_EQPRICE", "0.5")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.5


# Round 34A — B1 fix: ``_discover_eqprice`` was removed because the
# finduser HTML never carries an ``<input id="EqPrice">`` field.  See
# ``_read_eqprice`` for the new meta/env-fallback source.


@retry_school()
def _fetch_html(session: requests.Session, openid: str) -> str:
    """GET the finduser shell page.  Returns the response body text."""
    url = _build_url(_FINDUSER_PATH, openid)
    path = _safe_path(url)
    try:
        resp = session.get(
            url,
            headers={
                "User-Agent": _WECHAT_UA,
                "Referer": config.DORM_BASE_URL.rstrip("/") + _FINDUSER_PATH,
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        # Re-raise with a sanitized message; the original exception
        # (which contains the full URL with the openid) is preserved
        # on the chain for debugging but never reaches the logger.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status else ""
        raise RuntimeError(
            f"GET {path} failed: {type(exc).__name__}{suffix}"
        ) from exc
    return resp.text


@retry_school()
def _fetch_data(
    session: requests.Session,
    openid: str,
    room_id: str,
) -> dict[str, Any]:
    """POST to the data API.  Returns the parsed JSON body.

    Raises on transport errors, HTTP >= 400, non-JSON body, and on
    ``status == "-1"`` (the portal's failure code).
    """
    url = _build_url(_DATA_PATH, openid)
    referer = _build_url(_FINDUSER_PATH, openid)
    path = _safe_path(url)
    try:
        resp = session.post(
            url,
            data={"roomId": room_id},
            headers={
                "User-Agent": _WECHAT_UA,
                "Referer": referer,
                "Origin": config.DORM_BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status else ""
        raise RuntimeError(
            f"POST {path} failed: {type(exc).__name__}{suffix}"
        ) from exc

    try:
        body: Any = resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"POST {path} returned non-JSON body "
            f"(status={resp.status_code})"
        ) from exc
    if not isinstance(body, dict):
        raise RuntimeError(f"POST {path} returned a non-object JSON body")

    if str(body.get("status")) not in ("0", 0):
        # Log the full parsed JSON body so the next failure is easy to
        # diagnose from journalctl / PowerShell.  ``body`` is the parsed
        # server response and never contains the openid; ``path`` strips
        # the openid off the request URL.
        logger.error(
            "POST %s returned failure payload: %s",
            path,
            body,
        )
        raise RuntimeError(
            f"POST {path} returned failure: {body!r}"
        )
    return body


def _coerce_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Days-remaining helper (Round 21a)
# ---------------------------------------------------------------------------
# Round 21a fix: ``_data_remain`` used to call ``remain / avg(total_eq)``,
# but ``total_eq`` is a *cumulative* meter reading (e.g. 12345.67 kWh since
# install), not a per-day value.  Dividing a small ``remain`` (~30 kWh) by
# the cumulative meter reading always produced ~0, so the Feishu card always
# displayed "0 天".  The two fixes belong together:
#
#   1. ``avg_daily_kwh`` must be the **delta** between consecutive
#      ``total_eq`` rows, not the raw total.
#   2. This helper centralises the division + edge cases so the card
#      formatter can rely on consistent semantics:
#        - negative remain  -> 0  (we're already in the red)
#        - non-positive avg -> None (cannot estimate; show "—")
#        - normal case      -> remain / avg (rounded by caller)
def calc_days_remaining(remain_kwh: Optional[float],
                        avg_daily_kwh: Optional[float]) -> Optional[float]:
    """Estimate how many days the remaining kWh will last.

    Returns:
        * a non-negative float in the normal case
        * ``0`` if ``remain_kwh`` is negative (caller is in deficit)
        * ``None`` if the daily average is missing / non-positive
          (caller cannot make a meaningful estimate; the Feishu card
          should display "—" rather than "∞ 天" to avoid confusing
          the user with infinity)
    """
    if remain_kwh is None:
        return None
    if remain_kwh < 0:
        return 0.0
    if avg_daily_kwh is None or avg_daily_kwh <= 0:
        return None
    return remain_kwh / avg_daily_kwh


# Round 22 — outlier filter for cumulative meter series.
# The cumulative ``total_eq`` column occasionally carries a single
# huge jump (e.g. a meter reset/refactor in the school backend, an
# admin correction, or a re-install) that is NOT a real "one day of
# consumption".  A single such delta used to dominate the average
# (avg_daily > 200 kWh), so days-left rounded to 0 for any sane
# remaining kWh.  Skip any delta above this threshold; it's almost
# certainly an upstream event, not consumption.
_MAX_DAILY_KWH = 50.0


def compute_avg_daily_from_cumulative(
    rows: list[dict],
    max_daily_kwh: float = _MAX_DAILY_KWH,
) -> Optional[float]:
    """Convert a list of ``daily_elec`` rows (cumulative ``total_eq``) into
    the average per-day kWh consumed.

    The portal stores a monotonically-increasing meter reading in
    ``total_eq`` / ``zong_eq``; per-day consumption is the delta between
    consecutive rows.  Rows are sorted by ``dt`` ascending.  Negative
    deltas (the meter reset, a refactor, or a partial day) AND deltas
    above ``max_daily_kwh`` (a one-shot huge jump from a re-install /
    admin correction) are skipped so they don't poison the average.

    Returns ``None`` when there are fewer than 2 valid rows (we need at
    least one delta to compute an average) OR every delta was filtered
    out as an outlier.
    """
    if not rows:
        return None
    # Sort by date ASC and drop rows without a usable cumulative value.
    cleaned: list[tuple[str, float]] = []
    for r in rows:
        dt = r.get("dt")
        if not dt:
            continue
        v = _coerce_float(r.get("total_eq"))
        if v is None:
            v = _coerce_float(r.get("zong_eq"))
        if v is None:
            continue
        cleaned.append((dt, v))
    cleaned.sort(key=lambda t: t[0])
    deltas: list[float] = []
    prev_v: Optional[float] = None
    for _, v in cleaned:
        if prev_v is not None:
            delta = v - prev_v
            # Skip meter resets (delta < 0) and outliers (delta > max).
            if delta < 0:
                pass  # reset — don't update prev either, so we don't
                      # mix pre/post-reset deltas into the average
            elif delta <= max_daily_kwh:
                deltas.append(delta)
        prev_v = v
    if not deltas:
        return None
    return sum(deltas) / len(deltas)


def _coerce_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


# ---------------------------------------------------------------------------
# Round 2 — the single POST helper and the five new fetchers
# ---------------------------------------------------------------------------

@retry_school()
def _post_form(
    session: requests.Session,
    openid: str,
    path: str,
    **fields: Any,
) -> Any:
    """POST form-encoded ``fields`` to ``path`` and return the parsed JSON.

    All five new endpoints (F1-backfill / F2 / F3 / F4 / F5) go through
    this helper.  The existing live data API is intentionally *not*
    wired up here: it has its own response-validation logic in
    ``_fetch_data`` and is the primary code path that the rest of the
    module is built on top of.  Mixing the two would force the new
    endpoints to take on the ``status == "0"`` check they don't use.
    """
    url = _build_url(path, openid)
    referer = _build_url(_FINDUSER_PATH, openid)
    safe_p = _safe_path(url)
    try:
        resp = session.post(
            url,
            data=dict(fields),
            headers={
                "User-Agent": _WECHAT_UA,
                "Referer": referer,
                "Origin": config.DORM_BASE_URL,
                "X-Requested-With": "XMLHttpRequest",
            },
            timeout=30,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        suffix = f" status={status}" if status else ""
        raise RuntimeError(
            f"POST {safe_p} failed: {type(exc).__name__}{suffix}"
        ) from exc

    try:
        return resp.json()
    except ValueError as exc:
        raise RuntimeError(
            f"POST {safe_p} returned non-JSON body "
            f"(status={resp.status_code})"
        ) from exc


def _date_range(days: int) -> tuple[str, str]:
    """Return ``(stime, etime)`` as ``YYYY-MM-DD`` for the last ``days`` days.

    Used by F1-backfill, F2, F3, F5 — all of which accept a date range
    and a page index.  The endpoints are forgiving about time-of-day
    in our testing, so we send day-granularity strings; if the school
    ever tightens this we can promote to ``YYYY-MM-DD HH:MM:SS`` here
    in one place.

    Round 47 — switched to naive local time so the date range matches
    the user's wall clock (CST).  Previously we used ``datetime.now(UTC)``
    which silently shifted the window by 8 h during the morning hours.
    """
    end = datetime.now().date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def _as_list(payload: Any) -> list[dict]:
    """Coerce a payload to a list of dicts; return [] on anything else.

    The school sometimes returns a bare object (e.g. ``{"rows": [...]}")
    for an empty result, or even an error string.  The fetchers all
    expect a list; anything else is treated as zero rows.
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for k in ("rows", "data", "list", "records"):
            v = payload.get(k)
            if isinstance(v, list):
                return [r for r in v if isinstance(r, dict)]
    return []


def _fetch_dormEmQuery(
    session: requests.Session,
    openid: str,
    room_id: str,
    days: int = 30,
) -> list[dict]:
    """F1 (backfill): ``/dormEmQuery/selectRecord`` — last ``days`` of records."""
    stime, etime = _date_range(days)
    payload = _post_form(
        session, openid, _PATH_SELECT_RECORD,
        roomId=room_id, stime=stime, etime=etime, page=1,
    )
    return _as_list(payload)


def _fetch_dormEmDayElectQuery(
    session: requests.Session,
    openid: str,
    room_id: str,
    days: int = 30,
) -> list[dict]:
    """F2: ``/dormEmDayElectQuery/getEmDayElectQuery`` — per-day usage totals."""
    stime, etime = _date_range(days)
    payload = _post_form(
        session, openid, _PATH_DAY_ELECT,
        roomId=room_id, stime=stime, etime=etime, page=1,
    )
    return _as_list(payload)


def _fetch_dormEmWgQuery(
    session: requests.Session,
    openid: str,
    room_id: str,
    days: int = 30,
) -> list[dict]:
    """F3: ``/dormEmWgQuery/selectWgElect`` — power-violation records."""
    stime, etime = _date_range(days)
    payload = _post_form(
        session, openid, _PATH_WG_ELECT,
        roomId=room_id, stime=stime, etime=etime, page=1,
    )
    return _as_list(payload)


def _fetch_dormEmRunStatus(
    session: requests.Session,
    openid: str,
    room_id: str,
) -> dict[str, Any]:
    """F4: ``/dormEmRunStatus/getEmRunStatus`` — single live meter snapshot.

    Always returns a dict (possibly empty if the portal is unreachable
    and a later caller wants to inspect what came back).  We never
    raise a missing-field error here — the row will just be partial.
    """
    payload = _post_form(
        session, openid, _PATH_RUN_STATUS,
        roomId=room_id,
    )
    return payload if isinstance(payload, dict) else {}


# Round 33 — pay history starts from 2026-09-07 (semester / meter
# activation date).  The 30-day rolling window missed 9/7 because
# today is mid-September and 30 days back = mid-August; the
# semester start is a fixed anchor, not a sliding window.
_PAY_SEMESTER_START = "2026-09-07"


def _pay_date_range() -> tuple[str, str]:
    """Return (stime, etime) for F5: 2026-09-07 → today (CST, naive local).

    Round 47 — drop UTC; the F5 endpoint treats ``etime`` as a date
    string and CST is the user's wall clock, so a naive local ``today``
    is the correct boundary.
    """
    return _PAY_SEMESTER_START, datetime.now().date().isoformat()


def _fetch_dormEmPayQuery(
    session: requests.Session,
    openid: str,
    room_id: str,
    days: int = 30,  # kept for signature compatibility; ignored — see _pay_date_range
    fee_type: int = 0,
) -> list[dict]:
    """F5: ``/dormEmPayQuery/getEmPayQuery`` — pay history.

    ``fee_type`` follows the school's JS mapping (verified by Round 33
    via web_fetch of the finduser shell page):
      * ``0``  = 全部 (all — both recharges and refunds)
      * ``1``  = 缴费 (recharge / payment only)
      * ``-1`` = 退费 (refund only)

    We default to ``0`` so the dashboard sees every entry.  The earlier
    default of ``-1`` was a *bug*: -1 is the refund-only filter and
    silently dropped every recharge row, which is why F5 was empty.

    The date range is anchored at the 2026-09-07 semester / meter
    activation date (see ``_pay_date_range``) instead of a rolling
    30-day window — a 30-day window starting today never reaches
    2026-09-07, so recharges on/around 9/7 were missing.
    """
    del days  # explicit: this fetcher ignores its `days` arg
    stime, etime = _pay_date_range()
    payload = _post_form(
        session, openid, _PATH_PAY_QUERY,
        roomId=room_id, stime=stime, etime=etime, feeType=fee_type,
    )
    return _as_list(payload)


def _fetch_eqprice(
    session: requests.Session,
    openid: str,
) -> Optional[float]:
    """Read the cached EqPrice from meta (Round 34A).

    Previously this function hit the school portal's finduser page
    and parsed an ``<input id="EqPrice">`` hidden field.  That field
    does not actually exist in the live HTML (Round 34A audit B1),
    so the fetch always returned ``None`` and the Feishu card never
    showed the "⚡ 单价" line.

    Kept as a stub for backward compatibility with the existing
    one-shot cache call site in :func:`run_once`.  The cached value
    will be ``0.5`` (the default) the first time it runs and then
    whatever the user writes to ``meta.eqprice`` on subsequent
    scrapes — manual cache seeding via the dashboard is the
    supported workflow going forward.
    """
    # B1 fix: do NOT scrape the school — return the meta value
    # (with env / default fallback) so the cron never wastes an
    # HTTP request on a field that doesn't exist.
    return _read_eqprice()


def _parse_meta_ts(raw: Optional[str]) -> Optional[datetime]:
    """Parse a meta-stored ISO timestamp into a naive local datetime.

    Round 23 fix — the cron stores meta timestamps via
    ``_stamp_now`` which calls ``datetime.now(UTC).strftime(...)`` and
    drops the ``+00:00`` suffix, so the string is a *naive* ISO value.
    ``datetime.fromisoformat`` on that returns a naive ``datetime``, and
    subtracting a tz-aware ``datetime.now(UTC)`` from it raises::

        TypeError: can't subtract offset-naive and offset-aware datetimes

    This crash (caught by ``main()`` as a generic failure, with no
    helpful trace) was taking down the cron the moment any meta
    timestamp existed, i.e. from the second run onward.  The fix
    attached ``UTC`` to any naive parse result so the rest of the math
    kept working.

    Round 47 — DB / meta now store NAIVE LOCAL time, so we return the
    parsed datetime as-is.  Subtracting ``datetime.now()`` (also naive)
    no longer raises, and the cadence gate operates on the user's wall
    clock.  Bad / missing input returns ``None`` so callers can fall
    back to "never run" semantics.
    """
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    return ts


def _is_due(meta_key: str, interval_sec: int) -> bool:
    """Return True if the cadence gate is open (never run, or stale enough).

    Round 47 — subtract naive ``datetime.now()`` from the naive
    ``last_ts`` returned by ``_parse_meta_ts``.  Both sides are in the
    user's local clock, so the cadence gate no longer fires off-by-8h.
    """
    last_ts = _parse_meta_ts(db.get_meta(meta_key))
    if last_ts is None:
        return True
    return (datetime.now() - last_ts).total_seconds() >= interval_sec


def _stamp_now(meta_key: str) -> None:
    """Record the current local (CST, naive) time under ``meta_key``."""
    db.set_meta(meta_key, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


def _fallback_body_from_row(row: Optional[Any]) -> Optional[dict[str, Any]]:
    """Re-hydrate a ``body`` dict from a row's ``raw_html`` column.

    Round 26a helper: when the live school API is unreachable, fall
    back to the last good row in SQLite so the cron can keep pushing
    cards.  Returns ``None`` when there's no row yet (cold-start) or
    the stored JSON is broken — the caller is then expected to give
    up rather than ship a half-empty card.
    """
    if not row:
        return None
    raw = row["raw_html"] if hasattr(row, "__getitem__") else None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


# ---------------------------------------------------------------------------
# Round 2 — historical backfill
# ---------------------------------------------------------------------------

def _backfill_once(
    session: requests.Session,
    openid: str,
    room_id: str,
) -> None:
    """One-shot 30-day import for F1-backfill / F2 / F5.

    Guarded by the ``backfill_done`` meta key.  We run the three
    list-returning endpoints once on the first scrape after a deploy
    or a fresh DB.

    Round 39 — partial backfill retry: previously we set ``backfill_done``
    unconditionally after the three try/excepts, even if F2 (the
    daily_elec writer) had raised mid-loop.  That meant a single
    transient TypeError / DB hiccup would leave ``daily_elec`` empty
    forever — the dashboard would then show "¥— 等待学校返回数据"
    until the operator manually cleared the meta flag.  Now we only
    set the flag when **all three** endpoints succeed; any failure
    leaves the flag unset so the next cron tick retries the batch.
    The F1 step is intentionally side-effect-free (F1 rows are no
    longer written; see Round 33c comment) so its failure should NOT
    block the flag — we still treat F1 success as "the probe ran".
    """
    if db.get_meta(_META_BACKFILL_DONE) == "1":
        return

    logger.info("running one-shot historical backfill for room ***%s",
                _safe_room_tail(room_id))

    # F1-backfill: import up to 30 days of the live "remainEq" data.
    # Round 33c — F1 rows are NO LONGER written to daily_elec.
    # F1's useEq is cumulative-since-install, which has incompatible
    # semantics with F2's eebm (cumulative end-of-day).  Mixing them
    # in daily_elec.zong_eq corrupts the daily chart's used_today
    # deltas (e.g. showed 6.70 instead of 30.83 for 2026-09-11).
    # F2 (getEmDayElectQuery) is now the sole source for daily_elec.
    f1_ok = False
    try:
        rows = _fetch_dormEmQuery(session, openid, room_id, days=30)
        if rows:
            logger.info("backfill: skipped %d F1 rows (F2-only daily_elec)",
                        len(rows))
        f1_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: selectRecord failed: %s: %s",
                       type(exc).__name__, exc)

    # F2: writes daily_elec (the table _compute_monthly_projection reads).
    # Round 39 — this is the one we MUST succeed for "本月预计电费" to
    # populate.  An empty daily_elec + backfill_done='1' was the silent
    # failure that produced "¥—" forever.
    f2_ok = False
    try:
        rows = _fetch_dormEmDayElectQuery(session, openid, room_id, days=30)
        if rows:
            db.record_daily_elec(room_id, rows)
            logger.info("backfill: imported %d day-elect rows", len(rows))
        f2_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: getEmDayElectQuery failed: %s: %s",
                       type(exc).__name__, exc)

    # F5: pay history.  We do NOT run this every scrape, but the
    # backfill is a natural place to do the first pull.  Round 33:
    # no `days` arg — _fetch_dormEmPayQuery now uses _pay_date_range()
    # anchored at 2026-09-07 (semester start) → today.
    f5_ok = False
    try:
        rows = _fetch_dormEmPayQuery(session, openid, room_id)
        if rows:
            db.record_pay(room_id, rows)
            logger.info("backfill: imported %d pay rows", len(rows))
        f5_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: getEmPayQuery failed: %s: %s",
                       type(exc).__name__, exc)

    # Round 39 — only set the flag when ALL three sections succeeded.
    # Otherwise leave it unset so the next cron run retries.
    if f1_ok and f2_ok and f5_ok:
        db.set_meta(_META_BACKFILL_DONE, "1")
        logger.info("backfill complete (F1+F2+F5)")
    else:
        logger.warning(
            "backfill partial: F1=%s F2=%s F5=%s — will retry next cron "
            "(backfill_done NOT set)",
            f1_ok, f2_ok, f5_ok,
        )


# ---------------------------------------------------------------------------
# Feishu (Lark) notification
# ---------------------------------------------------------------------------

def _sign(secret: str) -> tuple[str, str]:
    """Compute the Feishu signing secret per the official spec."""
    timestamp = str(int(time.time()))
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    return timestamp, base64.b64encode(hmac_code).decode("utf-8")


def _fmt_num(v: Optional[float], unit: str) -> Optional[str]:
    if v is None:
        return None
    return f"{v:.2f} {unit}"


def _fmt_money(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    return f"¥{v:.2f}"


def _is_top_of_hour() -> bool:
    """Return True when the current local (CST) time is at the top of the hour (minute == 0).

    Round 47 — switched from UTC to naive local time.  The L2 "整点
    播报" card now fires when the user's wall clock hits minute 00,
    not when UTC hits minute 00 (which used to be 8h shifted).  This
    matches the documented L3 / L2 cron behaviour in
    ``docs/TECHNICAL.md``.
    """
    return datetime.now().minute == 0


def _now_beijing() -> datetime:
    """Return current local (CST, naive) time — Round 34A helper, Round 47 simplified.

    Historically this added 8 h to ``datetime.now(UTC)`` to get Asia/Shanghai
    wall clock; the school's cron schedule and the user-facing Feishu card
    timestamps have always been quoted in Beijing time.  Round 47 — the
    entire stack now stores NAIVE LOCAL timestamps (``db.insert``,
    ``_stamp_now``, ``_parse_meta_ts``), so ``_now_beijing`` simply
    returns ``datetime.now()``.  The 8-hour shift that used to live in
    this helper is now baked into every persisted timestamp.
    """
    return datetime.now()


# ---------------------------------------------------------------------------
# Round 34A — L3 daily / weekly / monthly report cadence
# ---------------------------------------------------------------------------
# Three parallel layers of notification:
#
#   L1 — every 10 min:  scrape + low-battery / violation / offline alerts.
#   L2 — every 10 min:  regular summary card (top-of-hour adds ⏰ prefix).
#   L3 — daily / weekly / monthly: long-form text reports, scheduled in
#        Beijing time, fired from a separate * * * * * cron entry so they
#        never collide with the */10 scrape cadence.
#
# L3 push is driven by the per-kind enable flag, the per-kind push time
# (overridable via meta), and the last-stamp meta key (idempotency).
# _l3_due() returns True exactly once per Beijing-day / week / month at
# the configured HH:MM; _stamp_l3() writes the stamp AFTER a successful
# push so a failed push can be retried on the next cron tick.

_DEFAULT_DAILY_TIME   = "09:00"
_DEFAULT_WEEKLY_TIME  = "09:00"
_DEFAULT_MONTHLY_TIME = "09:00"


def _read_push_time(key: str, default_hhmm: str) -> tuple[int, int]:
    """Read HH:MM from ``meta[key]`` and return ``(hour, minute)`` in BJ time.

    Falls back to ``default_hhmm`` (parsed the same way) when the meta
    value is missing, empty, or malformed.  We deliberately accept
    "8:30" (single-digit hour) as well as "08:30" so a user-friendly
    edit on the dashboard can still be parsed.
    """
    raw = db.get_meta(key)
    if raw is None or raw == "":
        raw = default_hhmm
    try:
        parts = str(raw).strip().split(":")
        if len(parts) != 2:
            raise ValueError("not HH:MM")
        h = int(parts[0])
        m = int(parts[1])
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError("hour/minute out of range")
        return h, m
    except (ValueError, AttributeError):
        # Malformed — fall back to default; never raise (cron would die).
        h_s, m_s = default_hhmm.split(":")
        return int(h_s), int(m_s)


def _l3_due(kind: str) -> bool:
    """Return True if the L3 report of ``kind`` is due this minute (Beijing).

    ``kind`` is one of ``"daily"``, ``"weekly"``, ``"monthly"``.

    Logic per kind:
      * daily   — the enable flag is "1", the current BJ minute matches
                  the configured push time, and today's BJ date is not
                  already stamped.
      * weekly  — same as daily, but only on BJ ISO weekday 1 (Monday),
                  and the stamp is the ISO year-week tuple ``YYYY-Www``.
      * monthly — same as daily, but only on BJ day==1, stamp is the
                  ``YYYY-MM`` month key.

    This function reads meta state but does NOT stamp — stamping is the
    caller's responsibility (see ``_stamp_l3``), and only happens after
    a successful push so a retry on the next cron tick can re-fire.
    """
    now_bj = _now_beijing()
    if kind == "daily":
        if db.get_meta(_META_PUSH_DAILY_ENABLE) != "1":
            return False
        hh, mm = _read_push_time(_META_PUSH_DAILY_TIME, _DEFAULT_DAILY_TIME)
        today_bj = now_bj.strftime("%Y-%m-%d")
        if (now_bj.hour == hh and now_bj.minute == mm
                and db.get_meta(_META_LAST_DAILY_REPORT) != today_bj):
            return True
    elif kind == "weekly":
        if db.get_meta(_META_PUSH_WEEKLY_ENABLE) != "1":
            return False
        hh, mm = _read_push_time(_META_PUSH_WEEKLY_TIME, _DEFAULT_WEEKLY_TIME)
        iso = now_bj.isocalendar()
        # weekday 1 == Monday in Python's isocalendar().
        if (iso.weekday == 1 and now_bj.hour == hh and now_bj.minute == mm):
            wk_key = f"{iso.year}-W{iso.week:02d}"
            if db.get_meta(_META_LAST_WEEKLY_REPORT) != wk_key:
                return True
    elif kind == "monthly":
        if db.get_meta(_META_PUSH_MONTHLY_ENABLE) != "1":
            return False
        hh, mm = _read_push_time(_META_PUSH_MONTHLY_TIME, _DEFAULT_MONTHLY_TIME)
        if (now_bj.day == 1 and now_bj.hour == hh and now_bj.minute == mm):
            mo_key = now_bj.strftime("%Y-%m")
            if db.get_meta(_META_LAST_MONTHLY_REPORT) != mo_key:
                return True
    return False


def _stamp_l3(kind: str) -> None:
    """Stamp the meta key that records the last L3 push for ``kind``.

    Must be called *after* a successful ``_post_feishu`` so a failed
    push can retry on the next cron tick.  Silently ignores unknown
    kinds (defensive: a future kind added without touching this fn
    shouldn't crash cron).
    """
    now_bj = _now_beijing()
    if kind == "daily":
        db.set_meta(_META_LAST_DAILY_REPORT, now_bj.strftime("%Y-%m-%d"))
    elif kind == "weekly":
        iso = now_bj.isocalendar()
        db.set_meta(_META_LAST_WEEKLY_REPORT, f"{iso.year}-W{iso.week:02d}")
    elif kind == "monthly":
        db.set_meta(_META_LAST_MONTHLY_REPORT, now_bj.strftime("%Y-%m"))


def compute_daily_summary(
    room_id: str,
    current_remain: Optional[float],
    current_dt: Optional[str],
) -> dict:
    """Compute yesterday + today + this-month totals for the daily report.

    Worker spec: simplify.  Returns a dict that ``_build_l3_card``
    can render directly.  Missing data → 0 / None (never raises).
    """
    daily = db.recent_daily_elec(room_id, days=7) if room_id else []
    # Sort by dt ASC so deltas pair correctly.
    sorted_rows = sorted(
        [r for r in daily if r.get("dt") and r.get("total_eq") is not None],
        key=lambda r: r["dt"],
    )
    today_kwh = 0.0
    yesterday_kwh = 0.0
    if len(sorted_rows) >= 2:
        try:
            today_kwh = float(sorted_rows[-1]["total_eq"]) - float(sorted_rows[-2]["total_eq"])
            if len(sorted_rows) >= 3:
                yesterday_kwh = float(sorted_rows[-2]["total_eq"]) - float(sorted_rows[-3]["total_eq"])
        except (TypeError, ValueError):
            pass
    avg7 = compute_avg_daily_from_cumulative(daily) or 0.0

    # This-month cumulative: latest eebm (or zong_eq) within current BJ month.
    month_total_kwh = 0.0
    now_bj = _now_beijing()
    month_prefix = now_bj.strftime("%Y-%m")
    month_rows = [
        r for r in sorted_rows
        if (r.get("dt") or "").startswith(month_prefix)
    ]
    if month_rows:
        try:
            month_total_kwh = float(month_rows[-1].get("total_eq") or 0)
        except (TypeError, ValueError):
            month_total_kwh = 0.0

    # Today's violations count (for the "违规" line).
    violations_today = 0
    if room_id:
        try:
            violations_today = sum(
                1 for v in db.recent_violations(room_id, days=1)
                if (v.get("dt") or "").startswith(month_prefix)
            )
        except Exception:
            pass

    return {
        "kind": "daily",
        "remain": current_remain,
        "dt": current_dt,
        "today_kwh": today_kwh,
        "yesterday_kwh": yesterday_kwh,
        "avg7_kwh": avg7,
        "month_total_kwh": month_total_kwh,
        "violations_today": violations_today,
    }


def compute_weekly_summary(
    room_id: str,
    current_remain: Optional[float],
    current_dt: Optional[str],
) -> dict:
    """Compute last-week + this-month totals for the weekly report.

    Worker spec: simplify.  Aggregates 7-day delta + 30-day running
    total + total recharges in the same window.
    """
    daily = db.recent_daily_elec(room_id, days=30) if room_id else []
    sorted_rows = sorted(
        [r for r in daily if r.get("dt") and r.get("total_eq") is not None],
        key=lambda r: r["dt"],
    )
    # last-week (last 7 rows) total
    last_week_kwh = 0.0
    if len(sorted_rows) >= 8:
        try:
            last_week_kwh = (
                float(sorted_rows[-1]["total_eq"]) - float(sorted_rows[-8]["total_eq"])
            )
        except (TypeError, ValueError):
            last_week_kwh = 0.0
    elif len(sorted_rows) >= 2:
        try:
            last_week_kwh = float(sorted_rows[-1]["total_eq"]) - float(sorted_rows[0]["total_eq"])
        except (TypeError, ValueError):
            last_week_kwh = 0.0

    # this-month cumulative
    now_bj = _now_beijing()
    month_prefix = now_bj.strftime("%Y-%m")
    month_total_kwh = 0.0
    month_rows = [r for r in sorted_rows if (r.get("dt") or "").startswith(month_prefix)]
    if month_rows:
        try:
            month_total_kwh = float(month_rows[-1].get("total_eq") or 0)
        except (TypeError, ValueError):
            month_total_kwh = 0.0

    # Recharge total in the past 30 days
    recharge_total = 0.0
    if room_id:
        try:
            for p in db.recent_pay(room_id, days=30):
                fee_type = (p.get("fee_type") or "").strip()
                # Only count 缴费 (recharge), not 退费 (refund).
                if "退" in fee_type:
                    continue
                money = p.get("money")
                if money is None:
                    continue
                try:
                    recharge_total += float(money)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    violations_week = 0
    if room_id:
        try:
            violations_week = len(db.recent_violations(room_id, days=7))
        except Exception:
            pass

    return {
        "kind": "weekly",
        "remain": current_remain,
        "dt": current_dt,
        "last_week_kwh": last_week_kwh,
        "month_total_kwh": month_total_kwh,
        "recharge_total": recharge_total,
        "violations_week": violations_week,
    }


def compute_monthly_summary(
    room_id: str,
    current_remain: Optional[float],
    current_dt: Optional[str],
) -> dict:
    """Compute last-month + this-month totals for the monthly report.

    Worker spec: simplify.  Reports last month's 30-day delta, this
    month's running total, and the lifetime violation count.
    """
    daily = db.recent_daily_elec(room_id, days=60) if room_id else []
    sorted_rows = sorted(
        [r for r in daily if r.get("dt") and r.get("total_eq") is not None],
        key=lambda r: r["dt"],
    )
    now_bj = _now_beijing()
    mo_key = now_bj.strftime("%Y-%m")
    last_month_kwh = 0.0
    this_month_kwh = 0.0
    # Last-month delta: sum of (delta within last-month rows).
    last_month_rows = [r for r in sorted_rows if not (r.get("dt") or "").startswith(mo_key)]
    if len(last_month_rows) >= 2:
        try:
            last_month_kwh = float(last_month_rows[-1]["total_eq"]) - float(last_month_rows[0]["total_eq"])
        except (TypeError, ValueError):
            last_month_kwh = 0.0
    this_month_rows = [r for r in sorted_rows if (r.get("dt") or "").startswith(mo_key)]
    if len(this_month_rows) >= 2:
        try:
            this_month_kwh = float(this_month_rows[-1]["total_eq"]) - float(this_month_rows[0]["total_eq"])
        except (TypeError, ValueError):
            this_month_kwh = 0.0
    elif this_month_rows:
        try:
            this_month_kwh = float(this_month_rows[-1]["total_eq"])
        except (TypeError, ValueError):
            this_month_kwh = 0.0

    recharge_total = 0.0
    if room_id:
        try:
            for p in db.recent_pay(room_id, days=60):
                fee_type = (p.get("fee_type") or "").strip()
                if "退" in fee_type:
                    continue
                money = p.get("money")
                if money is None:
                    continue
                try:
                    recharge_total += float(money)
                except (TypeError, ValueError):
                    pass
        except Exception:
            pass

    violations_total = 0
    if room_id:
        try:
            violations_total = len(db.recent_violations(room_id, days=60))
        except Exception:
            pass

    return {
        "kind": "monthly",
        "remain": current_remain,
        "dt": current_dt,
        "last_month_kwh": last_month_kwh,
        "this_month_kwh": this_month_kwh,
        "recharge_total": recharge_total,
        "violations_total": violations_total,
    }


# L3 report header titles / colors — single source of truth so the
# title and the color always agree.
_L3_TITLES: dict[str, str] = {
    "daily":   "📊 每日用电报告",
    "weekly":  "📈 每周用电报告",
    "monthly": "📅 每月用电报告",
}
_L3_TEMPLATES: dict[str, str] = {
    "daily":   "blue",
    "weekly":  "green",
    "monthly": "purple",
}


def _build_l3_card(kind: str, summary: dict, room_label: Optional[str], note: str) -> dict:
    """Build the L3 report card for ``kind`` (``daily`` / ``weekly`` / ``monthly``).

    Re-uses ``_build_card`` so the columns / free-vs-charge / read-time
    rendering stays consistent; overrides the title emoji and the
    header template color, and suppresses the "🔔 ⏰ 整点播报" prefix
    (the L3 card already covers the same minute, the L2 summary would
    just be a duplicate in the user's chat).

    Worker spec: simplify — feed the per-kind summary fields back into
    ``body`` as a flat dict so ``_build_card``'s existing render code
    handles them.  The two-column layout is the same; only the title
    and color change.
    """
    title_prefix = _L3_TITLES.get(kind, "📊 报告")
    header_template = _L3_TEMPLATES.get(kind, "blue")
    body: dict[str, Any] = {
        "remainEq": summary.get("remain"),
        "dt": summary.get("dt"),
        "eqprice": _read_eqprice(),
    }
    # Surface the L3 numbers in the summary line via total_eq (used
    # by _build_card's "累计用电 ... 已用 %" line).  We use the
    # month/week-cumulative value so the user sees a comparable
    # magnitude.
    if kind == "daily":
        body["totalEq"] = summary.get("month_total_kwh")
    elif kind == "weekly":
        body["totalEq"] = summary.get("month_total_kwh")
    elif kind == "monthly":
        body["totalEq"] = summary.get("this_month_kwh") or summary.get("month_total_kwh")

    card = _build_card(
        body=body,
        room_label=room_label,
        note=note,
        stale=False,
        title_prefix=title_prefix,
        header_template=header_template,
        top_of_hour=False,  # L3 is the canonical report for this minute
    )

    # Append a kind-specific narrative block so the L3 cards are not
    # identical to the L2 summary cards.  The narrative is plain
    # lark_md text; we keep it short to avoid spamming the user.
    narrative_lines: list[str] = []
    if kind == "daily":
        tk = summary.get("today_kwh") or 0.0
        yk = summary.get("yesterday_kwh") or 0.0
        avg7 = summary.get("avg7_kwh") or 0.0
        vt = summary.get("violations_today") or 0
        narrative_lines.append(
            f"📅 今日：**{tk:.2f}** kW·h　·　昨日：**{yk:.2f}** kW·h"
        )
        narrative_lines.append(
            f"📈 7 日均值：**{avg7:.2f}** kW·h/天"
        )
        if vt:
            narrative_lines.append(f"⚠ 今日违规：**{vt}** 次")
    elif kind == "weekly":
        lwk = summary.get("last_week_kwh") or 0.0
        mt = summary.get("month_total_kwh") or 0.0
        rc = summary.get("recharge_total") or 0.0
        vw = summary.get("violations_week") or 0
        narrative_lines.append(
            f"📅 上周累计：**{lwk:.2f}** kW·h　·　本月累计：**{mt:.2f}** kW·h"
        )
        narrative_lines.append(
            f"💰 近 30 天充值：**¥{rc:.2f}**"
        )
        if vw:
            narrative_lines.append(f"⚠ 近 7 天违规：**{vw}** 次")
    elif kind == "monthly":
        lm = summary.get("last_month_kwh") or 0.0
        tm = summary.get("this_month_kwh") or 0.0
        rc = summary.get("recharge_total") or 0.0
        vt = summary.get("violations_total") or 0
        narrative_lines.append(
            f"📅 上月累计：**{lm:.2f}** kW·h　·　本月累计：**{tm:.2f}** kW·h"
        )
        narrative_lines.append(
            f"💰 近 60 天充值：**¥{rc:.2f}**"
        )
        if vt:
            narrative_lines.append(f"⚠ 近 60 天违规：**{vt}** 次")

    if narrative_lines:
        narrative_div = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "\n".join(narrative_lines),
            },
        }
        # Insert just before the trailing ``note`` so the narrative
        # sits between the cols and the footer.
        elements: list[dict] = card["card"]["elements"]
        # The note element is the last one; insert before it.
        if elements and elements[-1].get("tag") == "note":
            elements.insert(len(elements) - 1, narrative_div)
        else:
            elements.append(narrative_div)

    return card


def _template_for_remain(remain: Optional[float]) -> str:
    """Pick a Feishu header ``template`` color for the remaining kW·h.

    Cutoffs are module-level so they're easy to tune; see the
    ``_REMAIN_*_BELOW`` constants.  Falls back to ``"blue"`` when
    ``remain`` is missing (data not loaded yet, or the field absent).
    """
    if remain is None:
        return "blue"
    if remain < _REMAIN_RED_BELOW:
        return "red"
    if remain < _REMAIN_ORANGE_BELOW:
        return "orange"
    if remain < _REMAIN_BLUE_BELOW:
        return "blue"
    return "green"


def _build_card(
    body: dict[str, Any],
    room_label: Optional[str],
    note: str,
    stale: bool = False,
    *,
    title_prefix: Optional[str] = None,      # Round 34A — override ⚡ icon (L3 reports)
    header_template: Optional[str] = None,   # Round 34A — override template color (L3 reports)
    top_of_hour: Optional[bool] = None,     # Round 34A — override top-of-hour (L3 suppression)
) -> dict:
    """Build the Feishu interactive card (v2: iconified + 2-column layout).

    The header now carries the headline number as a subtitle, and its
    background color reflects how urgent a recharge is.  The body is
    laid out as: a summary line, two 2-column rows (free/charge and
    used/water), and a read-time footer.  Fields that are missing
    render as an em-dash inside their cell; the read-time row is
    dropped entirely when ``dt`` is missing.  Round 2 adds an extra
    "⚡ 单价" line to the summary when ``body["eqprice"]`` is set.

    Round 26a: when ``stale`` is True (cron fell back to a cached row
    because the school API is down), prepend a yellow "⚠ 数据陈旧"
    banner above the summary so the user knows the numbers aren't
    live.  Title flips from ⚡ to ⚠ to match.
    """
    remain          = _coerce_float(body.get("remainEq"))
    free_eq         = _coerce_float(body.get("freeEq"))
    recharge_eq     = _coerce_float(body.get("rechargeEq"))
    use_eq          = _coerce_float(body.get("useEq"))
    total_eq        = _coerce_float(body.get("totalEq"))
    remain_wq_money = _coerce_float(body.get("remainWqMoney"))
    dt              = _coerce_str(body.get("dt"))
    eqprice         = _coerce_float(body.get("eqprice"))
    # Round 34A — allow L3 dispatchers to suppress the "🔔 ⏰ 整点
    # 播报" prefix (the L3 card already covers the same minute), and
    # also to override the auto-derived "is it minute==0 now?" check
    # when the L3 caller has already established context.
    top_of_hour_eff = _is_top_of_hour() if top_of_hour is None else top_of_hour

    # ---- header ---------------------------------------------------------
    title_room = room_label if room_label else "宿舍电量监控"
    header_subtitle = (
        f"剩余 {remain:.2f} kW·h" if remain is not None else "剩余 — kW·h"
    )
    # Round 34A — L3 cards pass a custom emoji (📊/📈/📅); default to ⚡.
    title_prefix_eff = "⚡" if title_prefix is None else title_prefix
    # Round 34A — L3 cards pass an explicit template color; default to
    # the auto-derived one (green on top-of-hour, else remain gauge).
    header_template_eff = (
        "green" if top_of_hour_eff else _template_for_remain(remain)
    ) if header_template is None else header_template
    if stale:
        title_prefix_eff = "⚠"
        header_template_eff = "yellow"
    header = {
        "title":    {"tag": "plain_text", "content": (
            f"🔔 ⏰ 整点播报 · {title_room}" if top_of_hour_eff
            else f"{title_prefix_eff} {title_room}"
        )},
        "subtitle": {"tag": "plain_text", "content": header_subtitle},
        "template": header_template_eff,
    }

    # ---- summary line ---------------------------------------------------
    summary_lines: list[str] = []
    if total_eq is not None and remain is not None and total_eq > 0:
        used_pct = (total_eq - remain) / total_eq * 100
        prefix = "⏰ " if top_of_hour_eff else ""
        summary_lines.append(
            f"{prefix}"
            f"📊 累计用电 **{total_eq:.2f}** kW·h"
            f"　·　已用 **{used_pct:.1f}%**"
        )
    if eqprice is not None:
        summary_lines.append(f"⚡ 单价：**¥{eqprice:.3f}**/kW·h")
    summary_div: Optional[dict] = None
    if summary_lines:
        summary_div = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "　·　".join(summary_lines),
            },
        }

    # ---- two-column rows ------------------------------------------------
    def _kv_cell(icon: str, label: str, body_text: str) -> dict:
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"{icon} **{label}**\n{body_text}",
            },
        }

    free_text  = f"{free_eq:.2f} kW·h"     if free_eq         is not None else "—"
    charge_text = f"{recharge_eq:.2f} kW·h" if recharge_eq     is not None else "—"
    used_text  = f"{use_eq:.2f} kW·h"      if use_eq          is not None else "—"
    water_text = f"¥{remain_wq_money:.2f}" if remain_wq_money is not None else "—"

    cols_free_charge = {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_kv_cell("🎁", "免费电量", free_text)],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_kv_cell("💰", "充值电量", charge_text)],
            },
        ],
    }
    cols_used_water = {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_kv_cell("🔥", "已用电量", used_text)],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [_kv_cell("💧", "剩余水费", water_text)],
            },
        ],
    }

    # ---- read-time row --------------------------------------------------
    read_time_div: Optional[dict] = None
    if dt is not None:
        read_time_div = {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"🕐 抄表时间：**{dt}**",
            },
        }

    # ---- assemble -------------------------------------------------------
    elements: list[dict] = []
    if stale:
        # Yellow banner at the very top of the card so the user can't
        # miss that the live numbers below are stale.
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "⚠️ **数据陈旧** — 学校接口无响应，"
                    "显示上一次成功抓取的数据。"
                ),
            },
        })
        elements.append({"tag": "hr"})
    if summary_div is not None:
        elements.append(summary_div)
    elements.append({"tag": "hr"})
    elements.append(cols_free_charge)
    elements.append({"tag": "hr"})
    elements.append(cols_used_water)
    elements.append({"tag": "hr"})
    if read_time_div is not None:
        elements.append(read_time_div)
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": note}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": header,
            "elements": elements,
        },
    }


def _build_offline_card(run_status: dict[str, Any], note: str) -> dict:
    """Build the red "电表离线" card for an offline meter.

    Pushed in addition to the regular summary card.  The body shows the
    meter's last reported update time so the user has a single
    "as-of" data point to know how stale the readings are.
    """
    update_dt = _coerce_str(run_status.get("updateDt")) or "—"
    stop_reason = _coerce_str(run_status.get("stopReason")) or "—"
    work_status = _coerce_str(run_status.get("workStatus")) or "—"
    run_status_label = _coerce_str(run_status.get("runStatus")) or "—"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": "⚠ 电表离线"},
                "subtitle": {"tag": "plain_text", "content": f"最后在线 {update_dt}"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"🚨 电表状态：**{run_status_label}**\n"
                            f"📡 通讯状态：**{work_status}**\n"
                            f"⏸ 停机原因：**{stop_reason}**\n"
                            f"🕐 最后上报：**{update_dt}**"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text", "content": note}],
                },
            ],
        },
    }


def _post_feishu(card: dict, *, raise_on_error: bool = False) -> None:
    """Post the card to Feishu.  Falls back to stdout if no webhook is set.

    Round 37 — ``raise_on_error`` flag controls whether a failed webhook
    POST bubbles up as an exception or is swallowed (the original cron
    behavior, preserved by default).

    Background:
      * The cron tick fires this on every alert; a transient 4xx/5xx
        from the Feishu side should NOT crash the scraper — we just log
        and move on (raise_on_error=False default).
      * But the OOBE / Admin "send test message" button needs to surface
        the failure to the user, otherwise the wizard silently reports
        "成功" every time (see B2 / B3).  When called from
        ``/admin/api/test-push``, we set raise_on_error=True so the
        caller can jsonify the error string and toast it back to the UI.
    """
    if not config.FEISHU_WEBHOOK:
        logger.warning("FEISHU_WEBHOOK is empty; printing card to stdout instead.")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return

    payload: dict = dict(card)
    if config.FEISHU_SECRET:
        ts, sign = _sign(config.FEISHU_SECRET)
        payload["timestamp"] = ts
        payload["sign"] = sign

    try:
        # Round 26a: route the webhook through the shared Session so the
        # connection pool can reuse the Feishu-server TLS handshake;
        # the bare ``requests.post`` here used to spin a fresh urllib3
        # pool per cron tick.
        resp = _SESSION.post(config.FEISHU_WEBHOOK, json=payload, timeout=15)
        resp.raise_for_status()
        logger.info("Feishu notification sent: %s", resp.text)
    except requests.RequestException as exc:
        logger.error("Feishu post failed: %s", exc)
        if raise_on_error:
            # Round 37 — re-raise so the caller (typically the OOBE /
            # Admin test-push endpoint) can surface the error to the
            # user.  Preserve the original traceback for debugging.
            raise


# ---------------------------------------------------------------------------
# Round 2 — alert pushers
# ---------------------------------------------------------------------------

def _is_offline(run_status_row: Optional[dict]) -> bool:
    """Heuristic: the meter is offline when ``runStatus`` is missing
    or doesn't match any of the online labels.  Treat an absent row
    (no F4 fetch has ever succeeded) as offline so the user gets
    notified instead of staring at a silent dashboard."""
    if not run_status_row:
        return True
    label = _coerce_str(run_status_row.get("run_status"))
    if not label:
        return True
    return label not in _ONLINE_LABELS


def _check_low_battery_alert(
    session: requests.Session,
    openid: str,
    room_id: str,
    current_remain: Optional[float],
) -> None:
    """Push a low-battery card the first time ``remain`` drops below
    the red threshold during a continuous low-battery period.

    The rule is: push only on the *transition* from non-low to low.
    The current scrape's `current_remain` is the just-inserted row;
    `rows[-2]` is the most-recent prior record.  If the prior record
    was also low, we're still in the same low-battery period — stay
    quiet.  Otherwise, this is the first scrape that crossed the line,
    and we push.
    """
    if current_remain is None or current_remain >= _REMAIN_RED_BELOW:
        return

    rows = db.query(hours=24)  # last day of records; 10-min cron -> ~144 rows
    if len(rows) < 2:
        # First-ever low reading — nothing to compare against, so push.
        _push_low_battery_card(current_remain)
        return

    prior_remain_raw = rows[-2]["remain"]
    prior_remain: Optional[float] = None
    if prior_remain_raw is not None:
        try:
            prior_remain = float(prior_remain_raw)
        except (TypeError, ValueError):
            prior_remain = None

    if prior_remain is not None and prior_remain < _REMAIN_RED_BELOW:
        # Already in low-battery period; the previous transition's
        # card is still relevant.  Do not storm the user.
        return

    _push_low_battery_card(current_remain)


def _push_low_battery_card(remain: float) -> None:
    """Build and post the low-battery card.  Records the dedupe meta."""
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": "🚨 剩余电量过低"},
                "subtitle": {"tag": "plain_text",
                             "content": f"剩余 {remain:.2f} kW·h"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"⚡ 剩余电量已跌破 **{_REMAIN_RED_BELOW} kW·h** 阈值。"
                            f"建议尽快充值，避免突然断电。"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text",
                                  "content": "由 dorm-power-monitor 推送"}],
                },
            ],
        },
    }
    _post_feishu(card)
    _stamp_now(_META_LAST_LOWBATT_ALERT)


def _check_violation_alert(
    room_id: str,
    just_fetched: list[dict],
) -> None:
    """Push a red card if F3 returned any new violation rows.

    Dedupe: rate-limit by ``_META_LAST_VIOLATION_ALERT`` (30 min) and
    only count violations newer than the last alert push.
    """
    if not just_fetched:
        return
    last_alert_ts = _parse_meta_ts(db.get_meta(_META_LAST_VIOLATION_ALERT))
    cooldown_active = (
        last_alert_ts is not None
        # Round 47 — naive local compare; both sides are in CST now.
        and (datetime.now() - last_alert_ts).total_seconds()
            < _VIOLATION_ALERT_COOLDOWN_SEC
    )
    if cooldown_active:
        return

    new_rows: list[dict] = []
    for r in just_fetched:
        dt = _coerce_str(r.get("dt"))
        if not dt:
            continue
        try:
            dt_parsed = datetime.fromisoformat(dt)
            # Round 47 — portal returns naive ISO strings that the
            # school generates in local Beijing time; ``last_alert_ts``
            # is also naive local (written by ``_stamp_now``).  Both
            # sides are now in CST, so the tz tag is no longer needed
            # and the fair-compare guarantee holds without an explicit
            # ``replace``.
        except ValueError:
            new_rows.append(r)  # can't compare -> assume new
            continue
        if last_alert_ts is None or dt_parsed > last_alert_ts:
            new_rows.append(r)
    if not new_rows:
        return

    lines = []
    for r in new_rows[:8]:  # cap the card body
        dt = _coerce_str(r.get("dt")) or "—"
        reason = _coerce_str(r.get("wg_reason") or r.get("wgReasonLabel")) or "—"
        # Round 29 fix: wg_power is the instantaneous power level
        # at the moment of the violation event (kW), NOT an energy
        # reading.  kW·h belongs to energy values (remain / total_eq /
        # use_eq / recharge_eq).
        wg_power = _coerce_float(r.get("wg_power") or r.get("wgPower"))
        wp = f"{wg_power:.2f} kW" if wg_power is not None else "—"
        lines.append(f"• {dt}　·　{reason}　·　{_fmt_wg_power(wg_power)}")

    body_text = (
        f"⚠ 检测到 **{len(new_rows)}** 条新的违规记录：\n\n"
        + "\n".join(lines)
    )
    if len(new_rows) > 8:
        body_text += f"\n\n…另有 {len(new_rows) - 8} 条未显示。"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": "🚨 用电违规"},
                "subtitle": {"tag": "plain_text",
                             "content": f"{len(new_rows)} 条新记录"},
                "template": "red",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": body_text},
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text",
                                  "content": "由 dorm-power-monitor 推送"}],
                },
            ],
        },
    }
    _post_feishu(card)
    _stamp_now(_META_LAST_VIOLATION_ALERT)


def _fmt_wg_power(v: Optional[float]) -> str:
    """Tiny helper used only by the violation card builder.

    Round 29 fix: ``wg_power`` from ``/dormEmWgQuery/selectWgElect``
    is the instantaneous power level (kW) recorded at the moment the
    meter flagged a violation.  This is a **power** value, not an
    energy reading, so the unit is ``kW`` (never ``kW·h``).
    """
    if v is None:
        return "—"
    return f"{v:.2f} kW"


def _check_stale_scrape() -> None:
    """Push a warning if the previous successful scrape is too old.

    This is intentionally a *best-effort* check: it only runs when the
    scraper itself is being executed, so if the box is fully down
    (cron dead, network unreachable at the OS level) the user will
    not get notified.  See README "故障排查" for the full discussion.
    """
    last_ts = _parse_meta_ts(db.get_meta(_META_LAST_SCRAPE_AT))
    if last_ts is None:
        # No prior successful run, or the stored value is bad.
        # Either way: nothing to compare against, don't push.
        return
    # Round 47 — naive local compare; both ``now`` and ``last_ts``
    # are in CST so the gap math is on the user's wall clock.
    gap = (datetime.now() - last_ts).total_seconds()
    if gap < _STALE_SCRAPE_GAP_SEC:
        return

    last_alert_ts = _parse_meta_ts(db.get_meta(_META_LAST_STALE_ALERT))
    if last_alert_ts is not None and last_alert_ts > last_ts:
        # We've already pushed an alert for this stale period.
        return

    # Round 24 fix — the previous code referenced an undefined ``last_raw``
    # here, which raised NameError as soon as the stale path was taken
    # (i.e. every run after the second one).  Use ``last_ts`` (a naive
    # local datetime parsed from ``last_scrape_at``) and format it the
    # same way ``_stamp_now`` writes meta timestamps so the card subtitle
    # matches what the dashboard shows.  Round 47 — ``last_ts`` is now
    # a naive CST value (no tz tag); the trailing ``CST`` label below
    # was ``UTC`` for the tz-aware case.
    last_raw_str = last_ts.strftime("%Y-%m-%d %H:%M:%S")
    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title":    {"tag": "plain_text", "content": "⚠ 抓取器长时间无响应"},
                "subtitle": {"tag": "plain_text",
                             "content": f"上次成功抓取 {last_raw_str} CST"},
                "template": "orange",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"🛠 已超过 **{int(gap // 60)} 分钟** 未成功抓取。"
                            f"请检查 cron / 网络 / 抓取器日志。"
                        ),
                    },
                },
                {
                    "tag": "note",
                    "elements": [{"tag": "plain_text",
                                  "content": "由 dorm-power-monitor 推送"}],
                },
            ],
        },
    }
    _post_feishu(card)
    _stamp_now(_META_LAST_STALE_ALERT)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_once(fetch_only: bool = False) -> Optional[tuple[dict, str]]:
    """One full scrape cycle: discover roomId, fetch data, store, notify.

    Raises on any error.  The top-level ``main()`` translates the
    exception into a sanitized log line and a non-zero exit code so
    cron sees the failure without the openid leaking into the log.

    Round 2 layering: the original F1 (dormEmRealRead) + F4 (run_status)
    run on every tick.  F2 (day-elect) / F3 (violations) / F5 (pay) are
    gated by meta timestamps so the cron can keep a 10-min cadence
    without the dashboard scraping the school for the same data
    repeatedly.  The one-shot historical backfill runs the first time
    we discover a roomId.

    Round 26b: ``fetch_only=True`` skips every Feishu notification
    (low-battery / violation / summary / offline cards) and returns
    ``(body, scrape_status)`` instead of ``None``.  Used by
    :func:`fetch_once` so the WebUI can pull fresh data on demand
    without re-pushing the cron summary card every time someone reloads
    the dashboard.
    """
    db.init()
    openid = _validate_openid()

    # Round 26a: pass the module-level Session into every helper so
    # urllib3 reuses the TCP/TLS connection to the school's nginx.
    session = _SESSION

    # Round 26a — degraded-mode flag.  This scrape is considered
    # "stale" if the *primary* data fetch fails AND we still have a
    # previous good body to push.  In that case the dashboard gets a
    # ⚠ card and ``/api/live`` reports ``stale: true`` so the Pillow
    # render can decorate the header.  We start optimistic ("ok") and
    # downgrade only when the primary fetch raises.
    scrape_status = "ok"

    if config.get_dorm_room_id().strip():
        room_id = config.get_dorm_room_id().strip()
        room_label: Optional[str] = None
    else:
        html = _fetch_html(session, openid)
        room_id, room_label = _discover_room(html)
        if not room_id:
            raise RuntimeError(
                "could not find <input id=\"roomId\"> in the finduser page; "
                "set DORM_ROOM_ID in .env to override the discovery step."
            )

    # Publish the roomId so the Flask dashboard can read it back
    # without re-running the discovery step.  Stored in `meta` under
    # `last_room_id`; the dashboard never sees the openid or any
    # credential, just the opaque UUID the scraper is currently using.
    db.set_meta("last_room_id", room_id)

    # Stale-scrape guard runs BEFORE any work — if the previous run
    # was >2h ago and we haven't already alerted for this gap, push a
    # warning.  This requires the cron to be alive, which is the
    # documented limitation.
    #
    # Round 24 — wrap the call in try/except so a regression in the
    # stale-card builder (e.g. another undefined name) can no longer
    # abort the whole scrape cycle.  The guard is best-effort, so a
    # warning in the log is the right blast radius.
    try:
        _check_stale_scrape()
    except Exception as exc:  # noqa: BLE001
        logger.warning("stale-scrape guard failed: %s: %s",
                       type(exc).__name__, exc)

    # Round 26a — primary fetch wrapped so a stale fallback can kick
    # in.  ``retry_school`` already retried 3x with exponential
    # backoff; if it still raised, the school is genuinely down — we
    # don't want the cron to go silent.  Pull the most recent row's
    # JSON body out of SQLite and push a ⚠ "数据陈旧" card.
    body: dict[str, Any]
    try:
        body = _fetch_data(session, openid, room_id)
    except Exception as exc:  # noqa: BLE001
        scrape_status = "stale"
        logger.warning("primary fetch failed (%s: %s); falling back to "
                       "last good row", type(exc).__name__, exc)
        last_row = db.latest()
        body = _fallback_body_from_row(last_row)
        if not body:
            # No historical row to fall back to — give up rather than
            # push an empty card.
            raise

    # Persist using the existing db schema: `remainEq` -> remain,
    # `dt` -> read_time.  Round 34D dropped the `raw_html` column
    # so the JSON blob is no longer stored — the dashboard re-reads
    # the persisted fields directly from SQLite instead.
    remain = _coerce_float(body.get("remainEq"))
    read_time = _coerce_str(body.get("dt"))
    db.insert(remain, read_time)

    # ---- EqPrice cache (Round 34A — B1 fix) ------------------------------
    # Was previously a one-shot HTML scrape of the finduser page's
    # ``<input id="EqPrice">`` hidden field — but that field never
    # actually exists in the live HTML, so the fetch always returned
    # None and ``body["eqprice"]`` stayed unset (the "⚡ 单价" line
    # was missing from every card).  Now we read meta -> env -> 0.5
    # default via ``_read_eqprice()`` and seed the cache the first
    # time we get a non-default value (i.e. env override).
    cached_eqprice_raw = db.get_meta(_META_EQPRICE)
    if cached_eqprice_raw is None:
        price = _read_eqprice()
        # Persist whatever the env (or default) says so the dashboard
        # can display / edit it as the canonical rate.  Skip the
        # write when the price is exactly the hard default (0.5) so
        # a future audit can distinguish "operator-set" from "no
        # override".
        if price != 0.5:
            db.set_meta(_META_EQPRICE, f"{price:.4f}")
            cached_eqprice_raw = f"{price:.4f}"
    body["eqprice"] = _read_eqprice()

    # ---- One-shot backfill (F1-selectRecord / F2 / F5) ------------------
    try:
        _backfill_once(session, openid, room_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("backfill: unexpected error: %s: %s",
                       type(exc).__name__, exc)

    # ---- F4: live meter status (every scrape) ---------------------------
    run_status_row: Optional[dict] = None
    try:
        rs = _fetch_dormEmRunStatus(session, openid, room_id)
        if rs:
            db.upsert_run_status(room_id, rs)
            run_status_row = db.get_run_status(room_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("getEmRunStatus failed: %s: %s",
                       type(exc).__name__, exc)
    # F4 also feeds the offline-card decision below.

    # ---- F2: day-elect (once per day) ----------------------------------
    if _is_due(_META_LAST_DAILY_ELECT_AT, _F2_INTERVAL_SEC):
        try:
            rows = _fetch_dormEmDayElectQuery(session, openid, room_id, days=30)
            db.record_daily_elec(room_id, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("getEmDayElectQuery failed: %s: %s",
                           type(exc).__name__, exc)
        _stamp_now(_META_LAST_DAILY_ELECT_AT)

    # ---- F3: violations (once per hour) + alert push --------------------
    just_violations: list[dict] = []
    if _is_due(_META_LAST_VIOLATION_AT, _F3_INTERVAL_SEC):
        try:
            just_violations = _fetch_dormEmWgQuery(
                session, openid, room_id, days=30
            )
            db.record_violation(room_id, just_violations)
        except Exception as exc:  # noqa: BLE001
            logger.warning("selectWgElect failed: %s: %s",
                           type(exc).__name__, exc)
        _stamp_now(_META_LAST_VIOLATION_AT)
        try:
            _check_violation_alert(room_id, just_violations)
        except Exception as exc:  # noqa: BLE001
            logger.warning("violation alert push failed: %s: %s",
                           type(exc).__name__, exc)

    # ---- F5: pay history (once per day) ---------------------------------
    # Round 33: dropped the `days=30` kwarg — _fetch_dormEmPayQuery
    # now anchors at the 2026-09-07 semester start (see _pay_date_range)
    # so recharges around 9/7 (e.g. first-week meter activation top-up)
    # land in the table.
    if _is_due(_META_LAST_PAY_AT, _F5_INTERVAL_SEC):
        try:
            rows = _fetch_dormEmPayQuery(session, openid, room_id)
            db.record_pay(room_id, rows)
        except Exception as exc:  # noqa: BLE001
            logger.warning("getEmPayQuery failed: %s: %s",
                           type(exc).__name__, exc)
        _stamp_now(_META_LAST_PAY_AT)

    # ---- Low-battery threshold alert ------------------------------------
    # Round 26b: skipped under fetch_only=True so the WebUI doesn't
    # double-push the same alert on every page reload.
    if not fetch_only:
        try:
            _check_low_battery_alert(session, openid, room_id, remain)
        except Exception as exc:  # noqa: BLE001
            logger.warning("low-battery alert push failed: %s: %s",
                           type(exc).__name__, exc)

    # ---- Build & post the regular summary card --------------------------
    note = (
        f"由 dorm-power-monitor 在 "
        f"{time.strftime('%Y-%m-%d %H:%M:%S')} 推送 · "
        f"roomId ***{_safe_room_tail(room_id)}"
    )

    # Round 34A — L3 daily / weekly / monthly dispatch.  These long-form
    # text reports run on a separate cadence from the L2 summary card
    # (cron * * * * *) and would otherwise race with it: at 09:00 BJ
    # on a Monday the */10 scrape cron also fires (UTC 01:00 == BJ 09:00
    # at certain offsets).  We compute each kind's summary first, post
    # the L3 card, stamp meta so it doesn't re-fire, and then suppress
    # the L2 "🔔 ⏰ 整点播报" prefix so the user doesn't get two cards
    # for the same minute (the L3 card already covers it).
    skip_l2_top_of_hour = False
    if not fetch_only:
        for kind in ("daily", "weekly", "monthly"):
            try:
                if not _l3_due(kind):
                    continue
                # Kind-specific summary — failures are non-fatal.
                if kind == "daily":
                    summary = compute_daily_summary(
                        room_id, remain, read_time,
                    )
                elif kind == "weekly":
                    summary = compute_weekly_summary(
                        room_id, remain, read_time,
                    )
                elif kind == "monthly":
                    summary = compute_monthly_summary(
                        room_id, remain, read_time,
                    )
                else:
                    continue
                card = _build_l3_card(kind, summary, room_label, note)
                _post_feishu(card)
                _stamp_l3(kind)
                skip_l2_top_of_hour = True
                logger.info("L3 %s report pushed", kind)
            except Exception as exc:  # noqa: BLE001
                # A single broken L3 push must NOT take down the L2
                # card — log + move on.
                logger.warning("L3 %s report failed: %s: %s",
                               kind, type(exc).__name__, exc)

    # Round 26b: skip the cron summary card under fetch_only mode.
    if not fetch_only:
        _post_feishu(_build_card(
            body, room_label, note,
            stale=(scrape_status == "stale"),
            top_of_hour=not skip_l2_top_of_hour,
        ))

    # ---- Offline-meter card (separate red push) -------------------------
    if not fetch_only and _is_offline(run_status_row):
        try:
            _post_feishu(_build_offline_card(run_status_row or {}, note))
        except Exception as exc:  # noqa: BLE001
            logger.warning("offline-card push failed: %s: %s",
                           type(exc).__name__, exc)

    # ---- Mark this scrape as successful --------------------------------
    _stamp_now(_META_LAST_SCRAPE_AT)
    db.set_meta(_META_LAST_SCRAPE_STATUS, scrape_status)

    fields_present = sum(1 for v in body.values() if v not in (None, ""))
    logger.info(
        "scrape complete: path=%s room_tail=%s fields=%d fetch_only=%s",
        _DATA_PATH,
        _safe_room_tail(room_id),
        fields_present,
        fetch_only,
    )

    # Round 26b: return (body, scrape_status) when called from
    # /api/refresh so the WebUI knows what just landed in the DB.
    return (body, scrape_status) if fetch_only else None


def fetch_once() -> dict:
    """Single scrape cycle, no notifications.

    Round 26b — thin wrapper around :func:`run_once` for the WebUI's
    ``POST /api/refresh`` endpoint.  Triggers an immediate scrape of
    the school dashboard, persists it to the local SQLite database,
    but skips every Feishu push (low-battery / violation / summary /
    offline cards) so a user reloading the page doesn't trigger a
    flood of duplicate messages.

    Returns a dict with ``body`` (full F1 payload), ``scrape_status``
    (``"ok"`` / ``"stale"`` / ``"failed"``) and ``dt`` (the school's
    read timestamp, suitable for "最后更新" display).

    Raises whatever :func:`run_once` raises.  The caller is expected to
    catch and translate into a 5xx response — never let the openid or
    raw HTML leak into the HTTP body.
    """
    body, scrape_status = run_once(fetch_only=True)
    return {
        "body": body,
        "scrape_status": scrape_status,
        "dt": body.get("dt"),
    }


def main(argv: list[str] | None = None) -> None:
    """Entry point.

    Usage:
        ``python dorm_power.py``              — one full scrape cycle
                                              (scrape + L1/L2 push + L3 dispatch)
        ``python dorm_power.py --tick``       — L3 cadence gate only; used by the
                                              ``* * * * *`` cron entry so the
                                              daily/weekly/monthly pushes do not
                                              race with the */10 scrape cron.
                                              No scrape, no L1/L2 alerts.
        ``python dorm_power.py --help``         — prints the above usage banner.

    Both modes share the same logging setup and the same
    sanitized-error handling: the openid never appears in a log line.
    """
    argv = argv if argv is not None else sys.argv[1:]
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if "--help" in argv or "-h" in argv:
        print(main.__doc__ or "dorm_power.py: see source for usage")
        return

    if "--tick" in argv:
        # L3 cadence gate only — no scrape, no L1/L2 push.  This is
        # the path the * * * * * cron entry hits every minute; the
        # actual push happens only when ``_l3_due(kind)`` returns True
        # for one of {daily, weekly, monthly}.  All three gates are
        # checked in a single tick so a misconfigured cron (e.g. every
        # 5 min) still fires the report on its scheduled minute.
        db.init()
        room_id = db.get_meta("last_room_id")
        remain = _coerce_float(db.get_meta(_META_EQPRICE))  # any cheap seed
        dt_str = _now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        for kind in ("daily", "weekly", "monthly"):
            try:
                if not _l3_due(kind):
                    continue
                if kind == "daily":
                    summary = compute_daily_summary(room_id or "", remain, dt_str)
                elif kind == "weekly":
                    summary = compute_weekly_summary(room_id or "", remain, dt_str)
                elif kind == "monthly":
                    summary = compute_monthly_summary(room_id or "", remain, dt_str)
                else:
                    continue
                note = (
                    f"由 dorm-power-monitor 在 "
                    f"{_now_beijing().strftime('%Y-%m-%d %H:%M:%S')} BJ 推送 · "
                    f"kind={kind}"
                )
                room_label_meta = db.get_meta("room_label")
                _post_feishu(_build_l3_card(kind, summary, room_label_meta, note))
                _stamp_l3(kind)
                logger.info("L3 %s report pushed (--tick mode)", kind)
            except Exception as exc:  # noqa: BLE001
                logger.warning("L3 %s report failed (--tick): %s: %s",
                               kind, type(exc).__name__, exc)
        return

    # Default: full scrape cycle (L1/L2 + L3 dispatch).
    try:
        run_once()
    except Exception as exc:
        # Sanitized top-level error: class name + message only.  The
        # full chain (which may contain the openid) is dropped here so
        # cron logs and journald never see the secret.
        logger.error("scrape failed: %s: %s", type(exc).__name__, exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
