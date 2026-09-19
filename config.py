"""config.py — Round 43 documented.

Central configuration module — every external dependency the project
needs is read once here, and downstream modules import constants rather
than re-reading ``os.environ`` themselves.  All secrets live in ``.env``
(the systemd unit loads it via ``EnvironmentFile=``); the values in this
file are only fallbacks used when the env var is missing.

主要功能 / Key responsibilities:
  * Load ``.env`` from the project root (not cwd) so cron / systemd /
    manual runs all behave identically.
  * Expose 5 lazy getters (``get_feishu_webhook``, ``get_feishu_secret``,
    ``get_dorm_openid``, ``get_dorm_base_url``, ``get_dorm_room_id``)
    that resolve ``meta``-cached values first, env second, hard-coded
    fallback third — R34A unified contract.
  * Provide typed constants for scrape / alert / dashboard tunables
    (intervals, thresholds, timeouts, retry counts).

数据流 / Data flow:
  .env / systemd EnvironmentFile -> config module -> all runtime modules
  (dorm_power, feishu_bot, web)

依赖 / Dependencies:
  * python-dotenv (load_dotenv)

Round history:
  * R0   - scaffold + dotenv loader
  * R25  - roomId 动态发现 (config.DORM_ROOM_ID 留空作为 fallback)
  * R34A - 5 个 lazy getters (meta -> env -> fallback chain)
  * R34D - db.py cleanup; lazy getters documented

NOTE: Round 43 cleanup — preserved the original 7-line docstring above;
the per-module overview lives here.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env into os.environ, regardless of the current working directory.
# ``python-dotenv``'s default search path is ``os.getcwd()``, which is
# wrong for us: cron runs ``www-data`` with cwd = /var/www, not the
# project root, so the .env file is invisible and every env var falls
# back to empty.  Pinning the path to ``Path(__file__).parent`` makes
# the load work the same whether the script is invoked manually, via
# cron, via a systemd timer, or in any other context.
#
# No-op if the file is absent.  Default override=False means values
# already present in the process environment (e.g. injected by systemd
# ``EnvironmentFile=`` on the server) win over the file; on Windows /
# manual ``python dorm_power.py`` runs there is nothing in os.environ,
# so .env is the source of truth.
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# ---------------------------------------------------------------------------
# Feishu (Lark) webhook
# ---------------------------------------------------------------------------
# Create a custom robot in your Feishu group, copy its webhook URL here.
# If the robot has signature verification enabled, also set FEISHU_SECRET.
FEISHU_WEBHOOK: str = os.getenv("FEISHU_WEBHOOK", "")

# Only required when the Feishu robot has "signature verification" enabled.
# Get it from the same robot config page (Settings -> Signature check).
FEISHU_SECRET: str = os.getenv("FEISHU_SECRET", "")

# ---------------------------------------------------------------------------
# Dorm portal (WeChat openid auth)
# ---------------------------------------------------------------------------
# The portal is a thin H5 page served inside the WeChat in-app browser.
# The only credential is the ``openid`` query-string parameter.  The
# scraper takes care of every other step; you only need to supply this
# one value (plus, optionally, DORM_BASE_URL and DORM_ROOM_ID).
#
# Treat DORM_OPENID like a password — see the "Security" section in
# README.md.  The previous username / password / login-URL settings
# (DORM_USERNAME / DORM_PASSWORD / DORM_LOGIN_URL) have been removed
# because the portal no longer uses a login form; auth is the openid
# alone.
DORM_OPENID: str = os.getenv("DORM_OPENID", "")

# Base URL of the dorm portal.  Defaults to the school's host; override
# this if they ever move it.
DORM_BASE_URL: str = os.getenv(
    "DORM_BASE_URL",
    "http://ybhqcz.fjny.edu.cn",
)

# Optional: the roomId UUID the data API expects.  When empty, the
# scraper GETs the finduser page once per scrape to discover the value
# from the hidden ``<input id="roomId">``.  Set this to skip that extra
# request (handy if the finduser page ever rate-limits you).
DORM_ROOM_ID: str = os.getenv("DORM_ROOM_ID", "")

# How often the cron job is expected to scrape.  Kept for backward
# compatibility with existing .env files; the new Feishu card no longer
# surfaces this number directly.
SCRAPE_INTERVAL_MINUTES: int = int(os.getenv("SCRAPE_INTERVAL_MINUTES", "60"))

# ---------------------------------------------------------------------------
# Local storage
# ---------------------------------------------------------------------------
# SQLite file.  In production point this at a directory the service
# user can write to, e.g. /var/lib/dorm-power-monitor/records.db
DB_PATH: str = os.getenv("DB_PATH", str(Path(__file__).parent / "records.db"))

# ---------------------------------------------------------------------------
# Flask dashboard
# ---------------------------------------------------------------------------
FLASK_HOST: str = os.getenv("FLASK_HOST", "127.0.0.1")
FLASK_PORT: int = int(os.getenv("FLASK_PORT", "5000"))
FLASK_DEBUG: bool = os.getenv("FLASK_DEBUG", "0") == "1"

# ---------------------------------------------------------------------------
# Feishu bot (inbound events) — Round 3
# ---------------------------------------------------------------------------
# All four env vars are empty by default.  The /feishu/event route is wired
# regardless of whether they're set, so the url_verification probe that
# Feishu sends on URL-save always succeeds.  But the bot refuses to send
# replies without FEISHU_APP_ID and FEISHU_APP_SECRET.  See README
# "Feishu bot setup" for how to obtain the values.
FEISHU_APP_ID: str = os.getenv("FEISHU_APP_ID", "").strip()
FEISHU_APP_SECRET: str = os.getenv("FEISHU_APP_SECRET", "").strip()
FEISHU_VERIFICATION_TOKEN: str = os.getenv("FEISHU_VERIFICATION_TOKEN", "").strip()
FEISHU_ENCRYPT_KEY: str = os.getenv("FEISHU_ENCRYPT_KEY", "").strip()


# ---------------------------------------------------------------------------
# Round 34D — lazy getters that prefer meta (WebUI override) over env
# ---------------------------------------------------------------------------
# These functions are imported lazily by ``dorm_power.py`` /
# ``feishu_bot.py`` so the WebUI's "settings" form can override the
# static .env values without a restart.  Backward compatibility: the
# module-level constants above (DORM_BASE_URL, DORM_OPENID,
# FEISHU_WEBHOOK) still read env at import time; the new getters read
# env at call time AND check meta first.
#
# Import is done inside each function (not at module top) so we don't
# create an import cycle — ``db`` already imports ``config``.

def get_dorm_base_url() -> str:
    """Get DORM_BASE_URL — prefer meta override, fallback env."""
    import db
    return db.get_meta("dorm_base_url") or os.environ.get("DORM_BASE_URL", "http://ybhqcz.fjny.edu.cn")


def get_dorm_openid() -> str:
    """Get DORM_OPENID — prefer meta override, fallback env."""
    import db
    return db.get_meta("dorm_openid") or os.environ.get("DORM_OPENID", "")


def get_feishu_webhook() -> str:
    """Get Feishu webhook — prefer meta override, fallback env."""
    import db
    return db.get_meta("feishu_webhook_url") or os.environ.get("FEISHU_WEBHOOK", "")


def get_api_internal_token() -> str:
    """Get internal API token — prefer meta override, fallback env."""
    import db
    return db.get_meta("api_internal_token") or os.environ.get("API_INTERNAL_TOKEN", "")


def get_dorm_room_id() -> str:
    """Get DORM_ROOM_ID — prefer meta override, fallback env.

    Round 35 — the scraper writes ``meta.last_room_id`` after every
    successful scrape (see ``dorm_power.run_once``), so reading
    ``meta.last_room_id`` here makes the admin UI round-trip with the
    scraper.  Falling back to env keeps deployments that never touched
    the admin UI (e.g. raw ``.env`` files) working as before.
    """
    import db
    return db.get_meta("last_room_id") or os.environ.get("DORM_ROOM_ID", "")
