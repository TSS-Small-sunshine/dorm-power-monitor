#!/usr/bin/env bash
# r48_deploy.sh — Round 48 documented.
#
# Server-side deploy script for Round 48 — the frontend JS timezone
# parse fix.  This is intentionally MINIMAL compared to r47_deploy.sh
# because:
#
#   * No DB migration needed — the JS parse change is invisible to the
#     backend.  records.ts / meta.<ts-key> are still written by
#     db.insert() in the R47 naive-CST format; we only changed the
#     browser-side parser to *force* CST via the "+08:00" suffix.
#   * No cron stop/start — the cron writes naive-CST rows in the same
#     format as before, so the existing cadence / L1 / L2 paths are
#     unaffected.
#   * Single service restart (dorm-web only) — the JS bundle lives
#     inside the web service; the cron is pure-Python and doesn't
#     touch the browser.
#
# Usage:
#   bash r48_deploy.sh
#
# Output:
#   - exit 0 on success (deployment complete + verify API returned a
#     fresh CST ts within the last 10 min)
#   - non-zero on any pre-flight failure (sha256 mismatch, unzip
#     error, or verify timeout)
#
# See TECHNICAL.md for the broader deploy story.
#
# Round 48 — frontend JS timezone parse fix
# -----------------------------------------
# Pre-R48 timeline:
#   web.py JS (setPill)  → new Date(tsRaw.replace(' ', 'T'))
#                            # parses against BROWSER's local TZ
#                            # CST host: 13:28 CST → age ≈ 60s   (pill OK)
#                            # UTC host: 13:28 UTC → age ≈ 8h    (pill 离线)
#   web.py JS (validate) → new Date(s) > new Date(e)              # same bug
# Pre-R48 user-visible bug:
#   Dashboard pill shows red "离线" on any non-CST host (UTC container,
#   foreign user laptop) even when the latest records.ts row is <60s old.
#   Screenshot evidence: records table latest ts = "2026-09-17 13:28:01"
#   (CST) but pill = "离线".
#
# Post-R48 timeline:
#   web.py JS (setPill)  → new Date(tsRaw.replace(' ', 'T') + '+08:00')
#                            # JS unconditionally reads the "+08:00" suffix
#                            # pill age is computed against CST wall clock
#                            # independent of container / browser TZ env var
#   web.py JS (validate) → new Date(s + '+08:00') > new Date(e + '+08:00')
#                            # same — start/end compare in CST
# Post-R48 expectation:
#   Dashboard pill should turn green "在线" the moment the operator
#   hard-refreshes (Ctrl+Shift+R) so the new JS bundle replaces the
#   cached one.  records.ts continues to be written in the R47
#   naive-CST format; no DB migration needed.

set -euo pipefail

# SHA256 placeholder; the orchestrator pastes the real value in after
# r48_build_zip.py produces the artifact.  Leave the literal string
# so a forgotten replace is loud at edit time.
SHA_EXPECTED="REPLACE_WITH_R48_BUILD_SHA256_AT_DEPLOY_TIME"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r47.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"

echo "=== R48 deploy: frontend JS timezone parse fix ==="
echo "ZIP:           $ZIP"
echo "APP:           $APP"
echo "BACKUP:        $BACKUP"
echo "SHA256 expect: $SHA_EXPECTED"
echo

# Sanity check the SHA placeholder was replaced.
if [[ "$SHA_EXPECTED" == "REPLACE_WITH_R48_BUILD_SHA256_AT_DEPLOY_TIME" ]]; then
    echo "FATAL: SHA_EXPECTED is still the placeholder string."
    echo "  Paste the real SHA from r48_build_zip.py output before running."
    exit 1
fi

# ---- step 1: backup current app dir ------------------------------------
echo "[1/7] backup → $BACKUP"
if [[ -d "$APP" ]]; then
    mv "$APP" "$BACKUP"
else
    echo "  no prior app dir to backup (first deploy?)"
    mkdir -p "$(dirname "$APP")"
fi
mkdir -p "$APP"

# ---- step 2: verify zip sha256 -----------------------------------------
echo "[2/7] sha256 verify"
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

# ---- step 3: unzip -----------------------------------------------------
echo "[3/7] unzip → $APP"
unzip -q "$ZIP" -d "$(dirname "$APP")"
echo "  $(find "$APP" -type f | wc -l) files extracted"

# ---- step 4: restore .env ----------------------------------------------
echo "[4/7] restore .env"
if [[ -f "$BACKUP/.env" ]]; then
    cp "$BACKUP/.env" "$APP/.env"
    echo "  .env restored from backup"
elif [[ -f "$APP/.env.example" && ! -f "$APP/.env" ]]; then
    cp "$APP/.env.example" "$APP/.env"
    echo "  WARN: no prior .env — copied .env.example (operator must edit)"
else
    echo "  .env left as-is"
fi

# ---- step 5: chown -----------------------------------------------------
echo "[5/7] chown dorm:dorm"
if id dorm >/dev/null 2>&1; then
    chown -R dorm:dorm "$APP"
else
    echo "  WARN: 'dorm' user not found, skipping chown"
fi

# Clear stale __pycache__ from the prior install so the new code is
# what's loaded — without this, Python can serve a cached .pyc built
# from the pre-R48 source after the file content changes.
find "$APP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---- step 6: restart dorm-web ------------------------------------------
echo "[6/7] restart $SERVICE"
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
# NOTE — we deliberately do NOT touch dorm-cron.timer here:
#   * The cron writes naive-CST rows in the R47 format, which the new
#     JS still parses correctly (the "+08:00" suffix is appended by
#     the parser, not stored).
#   * Cadence / L1 / L2 math is unchanged.
#   * Skipping the restart avoids a brief scrape gap.

# ---- step 7: verify ----------------------------------------------------
echo "[7/7] verify"
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r48_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  healthz 200 OK"
        python3 - <<'PY'
import json, sys
with open("/tmp/r48_healthz.json") as f:
    d = json.load(f)
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
        echo "=== R48 deploy SUCCESS ==="
        echo
        echo "Browser: Ctrl+Shift+R to hard-refresh dashboard"
        echo "Expected: 状态 pill should now show green \"在线\" if the latest"
        echo "          records.ts row is within 2 h.  Pre-R48 the pill was"
        echo "          stuck on red \"离线\" on any non-CST host."
        echo
        echo "If the pill still shows \"离线\":"
        echo "  1. Confirm the JS bundle was actually updated — open"
        echo "     devtools, look for \"+08:00\" in the setPill source."
        echo "  2. Clear browser cache (Ctrl+Shift+Delete) and reload."
        echo "  3. If the host TZ env var is unset, export TZ=Asia/Shanghai"
        echo "     before restarting dorm-web so server-side logs agree."
        exit 0
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

echo "FATAL: healthz never returned 200"
echo "  last body:"
cat /tmp/r48_healthz.json 2>/dev/null || echo "  (none)"
exit 1
