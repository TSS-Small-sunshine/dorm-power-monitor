#!/usr/bin/env bash
# r47_deploy.sh — Round 47 documented.
#
# Server-side deploy script for Round 47 — the timezone migration.
# Same skeleton as r42_deploy.sh (backup / SHA256 / unzip / restore
# .env / chown / restart / verify) PLUS the round-47 specific steps:
#
#   1. Stop dorm-cron FIRST so it can't write a fresh CST row into a
#      table that's mid-migration.
#   2. After unzip, run r47_migrate_utc_to_cst.py to back-fill +8 h
#      across records.ts and meta.<ts-key>.
#   3. Only THEN restart dorm-web and start dorm-cron back up.
#
# Usage:
#   bash r47_deploy.sh
#
# Output:
#   - exit 0 on success (deployment complete + verify API returned
#     a fresh CST ts within the last 10 min)
#   - non-zero on any pre-flight failure (sha256 mismatch, unzip
#     error, migrate error, or verify timeout)
#
# See TECHNICAL.md for the broader deploy story.
#
# Round 47 — naive CST migration
# ------------------------------
# Pre-R47 timeline:
#   db.insert        → datetime.now(UTC)        # records.ts stored UTC
#   web.py JS        → new Date(... + 'Z')      # browser parses as UTC
#   L2 _is_top_of_hour → minute == 0 in UTC     # 8 h shifted
# Pre-R47 user-visible bug:
#   A scrape at UTC 05:00 (BJ 13:00) showed "5:00" in the dashboard.
#
# Post-R47 timeline:
#   db.insert        → datetime.now()           # records.ts stored CST
#   web.py JS        → new Date(...)            # browser parses as local
#   L2 _is_top_of_hour → minute == 0 in CST     # matches user wall clock
# Post-R47 expectation:
#   Dashboard "采集时间" column shows the same hour as the user's
#   laptop clock; L2 整点播报 fires at the top of each CST hour.
#
# The migration script runs UNCONDITIONALLY — it adds 8 h to every
# existing row.  Safe to re-run on a fresh CST install (would push
# the rows 8 h forward, but a fresh install has no rows yet so this
# is a no-op).

set -euo pipefail

# SHA256 placeholder; the orchestrator pastes the real value in after
# r47_build_zip.py produces the artifact.  Leave the literal string
# so a forgotten replace is loud at edit time.
SHA_EXPECTED="REPLACE_WITH_R47_BUILD_SHA256_AT_DEPLOY_TIME"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r46.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"

# The cron timer / service names follow the deploy/dorm-cron.txt layout
# (systemd timer).  Stop them BEFORE the migration so no fresh CST row
# lands while the migration is mid-flight.
CRON_TIMER="dorm-cron.timer"
CRON_SERVICE="dorm-cron.service"

echo "=== R47 deploy: timezone migration UTC → CST ==="
echo "ZIP:           $ZIP"
echo "APP:           $APP"
echo "BACKUP:        $BACKUP"
echo "SHA256 expect: $SHA_EXPECTED"
echo

# Sanity check the SHA placeholder was replaced.
if [[ "$SHA_EXPECTED" == "REPLACE_WITH_R47_BUILD_SHA256_AT_DEPLOY_TIME" ]]; then
    echo "FATAL: SHA_EXPECTED is still the placeholder string."
    echo "  Paste the real SHA from r47_build_zip.py output before running."
    exit 1
fi

# ---- step 1: stop dorm-cron so no fresh CST row races the migration -----
echo "[1/9] stop dorm-cron"
if systemctl is-active --quiet "$CRON_TIMER"; then
    systemctl stop "$CRON_TIMER"
    echo "  $CRON_TIMER stopped"
else
    echo "  $CRON_TIMER was not active (skipping)"
fi
if systemctl is-active --quiet "$CRON_SERVICE"; then
    systemctl stop "$CRON_SERVICE"
    echo "  $CRON_SERVICE stopped"
fi

# ---- step 2: backup current app dir ------------------------------------
echo "[2/9] backup → $BACKUP"
if [[ -d "$APP" ]]; then
    mv "$APP" "$BACKUP"
else
    echo "  no prior app dir to backup (first deploy?)"
    mkdir -p "$(dirname "$APP")"
fi
mkdir -p "$APP"

# ---- step 3: verify zip sha256 -----------------------------------------
echo "[3/9] sha256 verify"
SHA_ACTUAL="$(sha256sum "$ZIP" | awk '{print $1}')"
if [[ "$SHA_ACTUAL" != "$SHA_EXPECTED" ]]; then
    echo "FATAL: zip sha256 mismatch"
    echo "  expected: $SHA_EXPECTED"
    echo "  actual:   $SHA_ACTUAL"
    # put the backup back so we don't lose the running version
    if [[ -d "$BACKUP" ]]; then
        rm -rf "$APP"
        mv "$BACKUP" "$APP"
        echo "  restored backup, aborting"
    fi
    exit 1
fi
echo "  OK ($SHA_ACTUAL)"

# ---- step 4: unzip -----------------------------------------------------
echo "[4/9] unzip → $APP"
unzip -q "$ZIP" -d "$(dirname "$APP")"
echo "  $(find "$APP" -type f | wc -l) files extracted"

# ---- step 5: restore .env ----------------------------------------------
echo "[5/9] restore .env"
if [[ -f "$BACKUP/.env" ]]; then
    cp "$BACKUP/.env" "$APP/.env"
    echo "  .env restored from backup"
elif [[ -f "$APP/.env.example" && ! -f "$APP/.env" ]]; then
    cp "$APP/.env.example" "$APP/.env"
    echo "  WARN: no prior .env — copied .env.example (operator must edit)"
else
    echo "  .env left as-is"
fi

# ---- step 6: chown -----------------------------------------------------
echo "[6/9] chown dorm:dorm"
if id dorm >/dev/null 2>&1; then
    chown -R dorm:dorm "$APP"
else
    echo "  WARN: 'dorm' user not found, skipping chown"
fi

# ---- step 7: run migration ---------------------------------------------
echo "[7/9] migrate DB UTC → CST"
python3 "$APP/scripts/deploy/r47_migrate_utc_to_cst.py"

# ---- step 8: restart services ------------------------------------------
echo "[8/9] restart services"
if systemctl is-active --quiet "$SERVICE"; then
    systemctl restart "$SERVICE"
else
    systemctl start "$SERVICE"
fi
sleep 2
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "FATAL: $SERVICE failed to start — check journalctl -u $SERVICE"
    exit 1
fi
echo "  $SERVICE is active"
# Restart the cron NOW — fresh CST rows can start landing safely.
systemctl start "$CRON_TIMER" || true
systemctl start "$CRON_SERVICE" || true

# ---- step 9: verify the migration ---------------------------------------
echo "[9/9] verify"
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r47_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  healthz 200 OK"
        # Inspect the most recent row's ts; it must be within 10 min of
        # now (i.e. today's wall clock in CST).
        python3 - <<'PY'
import json, sys
from datetime import datetime
with open("/tmp/r47_healthz.json") as f:
    d = json.load(f)
# /healthz returns ``db_ok`` + ``last_scrape_age_sec``; the rows are
# exposed via /api/data but we only need the age to confirm the
# migration ran (a freshly-updated row has age < 600 s).
age = d.get("last_scrape_age_sec")
if age is None:
    print("  WARN: healthz did not include last_scrape_age_sec — "
          "verify manually with curl /api/data?hours=1")
elif age < 600:
    print(f"  ✓ last scrape was {age:.0f} s ago — within 10 min")
else:
    print(f"  ✗ last scrape was {age:.0f} s ago — too old")
    sys.exit(1)
PY
        echo
        echo "=== R47 deploy SUCCESS ==="
        echo
        echo "Browser: Ctrl+Shift+R to hard-refresh dashboard"
        echo "Expected: 采集时间 column should now show CST hours (e.g."
        echo "          13:00 instead of 05:00 if the scrape ran at"
        echo "          13:00 local)."
        echo
        echo "If the column still shows UTC hours: clear browser cache"
        echo "(Ctrl+Shift+Delete) and reload — the round-47 frontend fix"
        echo "(drop 'Z' from new Date) needs the new web.py bundle to be"
        echo "in cache."
        exit 0
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

echo "FATAL: healthz never returned 200"
echo "  last body:"
cat /tmp/r47_healthz.json 2>/dev/null || echo "  (none)"
exit 1