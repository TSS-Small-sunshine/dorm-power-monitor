#!/usr/bin/env bash
# r42_deploy.sh — Round 43 documented.
#
# Server-side deploy script for Round 42 — same 7-step dance as the
# r41_deploy.sh template (backup / SHA256 verify / unzip / restore
# .env / chown / restart / healthz probe).
#
# Usage:
#   bash r42_deploy.sh
#
# Output:
#   - exit 0 on success (deployment complete + /healthz returns 200)
#   - non-zero on any pre-flight failure
#
# See TECHNICAL.md for the broader deploy story.
#
# Original Round-specific narrative follows.

# Round 42 deploy — fix web._read_eqprice missing fallback chain.
#
# Standard 7-step dance:
#   1. backup
#   2. SHA256 verify
#   3. unzip (overwrite in-place)
#   4. restore .env
#   5. chown to dorm:dorm
#   6. restart systemd unit
#   7. healthz probe
#
# Run on the server as the deploy user.  Pre-flight:
#   * zip is at $HOME/dorm-power-monitor.zip
#   * systemd unit name = dorm-web.service
#   * nginx already reverse-proxies / → 127.0.0.1:8000
#
# Post-deploy expected: dashboard "¥—" → real ¥ value (the 0.5 default
# eqprice now flows through to _compute_monthly_projection).  User's
# manual `INSERT meta(eqprice='0.5')` becomes redundant; safe to
# delete it with `sqlite3 records.db "DELETE FROM meta WHERE
# key='eqprice'"` but keeping it is harmless (meta still wins).

set -euo pipefail

SHA_EXPECTED="7af026f9eb68ae3b69dab7e1733c62ae14626b38426a0b11af73a29c6ae70833"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r41.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"

echo "=== R42 deploy ==="
echo "ZIP:           $ZIP"
echo "APP:           $APP"
echo "BACKUP:        $BACKUP"
echo "SHA256 expect: $SHA_EXPECTED"
echo

# ---- step 1: backup current app dir ----------------------------------------
if [[ -d "$APP" ]]; then
    echo "[1/7] backup → $BACKUP"
    mv "$APP" "$BACKUP"
else
    echo "[1/7] no prior app dir to backup (first deploy?)"
    mkdir -p "$(dirname "$APP")"
fi
mkdir -p "$APP"

# ---- step 2: verify zip sha256 ---------------------------------------------
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

# ---- step 3: unzip ---------------------------------------------------------
echo "[3/7] unzip → $APP"
unzip -q "$ZIP" -d "$(dirname "$APP")"
# unzip creates dorm-power-monitor/dorm-power-monitor/... — flatten:
if [[ -d "$(dirname "$APP")/dorm-power-monitor" ]]; then
    # the zip top dir is "dorm-power-monitor/" so the layout is
    # $(dirname $APP)/dorm-power-monitor/<files>
    # which is what $APP already is, so no-op if it lands correctly.
    :
fi
echo "  $(find "$APP" -type f | wc -l) files extracted"

# ---- step 4: restore .env --------------------------------------------------
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

# ---- step 5: chown ---------------------------------------------------------
echo "[5/7] chown dorm:dorm"
if id dorm >/dev/null 2>&1; then
    chown -R dorm:dorm "$APP"
else
    echo "  WARN: 'dorm' user not found, skipping chown"
fi

# ---- step 6: restart systemd unit ------------------------------------------
echo "[6/7] systemctl restart $SERVICE"
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

# ---- step 7: healthz probe -------------------------------------------------
echo "[7/7] healthz probe"
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r42_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  healthz 200 OK"
        cat /tmp/r42_healthz.json
        echo
        echo "=== R42 deploy SUCCESS ==="
        echo
        echo "Optional cleanup: the user's manual 'INSERT meta eqprice=0.5'"
        echo "is now redundant — web._read_eqprice() falls back to 0.5"
        echo "automatically.  Safe to delete with:"
        echo "  sqlite3 $APP/records.db \"DELETE FROM meta WHERE key='eqprice'\""
        echo "Keeping it is harmless (meta still takes priority)."
        exit 0
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

echo "FATAL: healthz never returned 200"
echo "  last body:"
cat /tmp/r42_healthz.json 2>/dev/null || echo "  (none)"
exit 1