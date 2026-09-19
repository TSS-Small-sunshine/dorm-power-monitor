#!/usr/bin/env bash
# r49_deploy.sh — Round 49 documented.
#
# Server-side deploy script for Round 49 — records 表去重 + UNIQUE 约束.
#
# Operational difference vs R47/R48:
#   * MUST stop dorm-web before running the dedupe migration so a
#     concurrent writer can't insert into a half-migrated table.
#     dorm-cron.timer can stay running — cron writes new rows at
#     minute granularity so any dup it'll create will be reaped by the
#     dedupe SQL anyway, and the cron never holds a long DB lock.
#   * The migration script is bundled into the zip and lives at
#     /opt/dorm-power-monitor/scripts/deploy/r49_dedupe_records.py.
#   * Backup is taken BEFORE the migration so the operator can roll
#     back to the pre-dedupe state if anything looks wrong.
#
# Usage:
#   bash r49_deploy.sh
#
# Output:
#   - exit 0 on success (deployment complete + migration complete +
#     dorm-web back up + healthz returned 200)
#   - non-zero on any pre-flight failure (sha256 mismatch, unzip
#     error, migration error, healthz timeout)
#
# See TECHNICAL.md for the broader deploy story.
#
# Round 49 — records 去重 + UNIQUE 约束
# --------------------------------------
# Pre-R49 timeline:
#   db.py _SCHEMA  → records(ts TEXT NOT NULL, read_time TEXT, remain REAL)
#                    CREATE INDEX idx_records_ts ON records(ts)
#   db.insert()    → plain INSERT INTO records (ts, read_time, remain) VALUES (?,?,?)
#   production     → 863 total / 686 unique ts / 177 duplicates
# Pre-R49 user-visible bug:
#   Dashboard line chart draws a "锯齿" — same ts appears twice with
#   different remain values (e.g. 180 vs 171), producing apparent
#   vertical jumps that aren't real.  Caused by cron overlap + a
#   race in db.insert() that allowed two writes for the same ts.
#
# Post-R49 timeline:
#   db.py _SCHEMA  → records(ts TEXT NOT NULL UNIQUE, read_time TEXT, remain REAL)
#                    (fresh installs; existing installs rebuilt via _migrate_records_unique)
#   db.insert()    → INSERT OR REPLACE INTO records (ts, read_time, remain) VALUES (?,?,?)
#                    so any future same-ts write overwrites the row
#                    instead of stacking a duplicate.
#   r49_dedupe_records.py
#                  → historical cleanup: for each duplicated ts,
#                    delete all but the smallest-remain row
#                    (电表 remain 单调递减 — 越小 = 越新), then rebuild
#                    the table so the UNIQUE constraint holds end-to-end.
# Post-R49 expectation:
#   records table holds one row per unique ts (total == COUNT(DISTINCT ts)).
#   Dashboard line chart is monotonically non-increasing without sawtooth.
#   Browser hard-refresh (Ctrl+Shift+R) picks up the rebuilt chart from
#   the now-clean dataset.

set -euo pipefail

# SHA256 placeholder; the orchestrator pastes the real value in after
# r49_build_zip.py produces the artifact.  Leave the literal string
# so a forgotten replace is loud at edit time.
SHA_EXPECTED="REPLACE_WITH_R49_BUILD_SHA256_AT_DEPLOY_TIME"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r48.bak.$(date -u +%Y%m%dT%H%M%SZ)"
DB_BACKUP="$HOME/dorm.db.r48-pre49.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"
MIGRATION="/opt/dorm-power-monitor/scripts/deploy/r49_dedupe_records.py"

# The migration script defaults to the production DB path.  If the
# operator has overridden DB_PATH via systemd unit, pass it through.
DB_PATH_ENV=""
if systemctl show "$SERVICE" 2>/dev/null | grep -q "DB_PATH="; then
    DB_PATH_ENV="$(systemctl show "$SERVICE" | grep -oE 'DB_PATH=[^ ]+' | head -n1 | cut -d= -f2-)"
fi

echo "=== R49 deploy: records dedupe + UNIQUE ==="
echo "ZIP:           $ZIP"
echo "APP:           $APP"
echo "BACKUP:        $BACKUP"
echo "DB_BACKUP:     $DB_BACKUP"
echo "SHA256 expect: $SHA_EXPECTED"
echo

# Sanity check the SHA placeholder was replaced.
if [[ "$SHA_EXPECTED" == "REPLACE_WITH_R49_BUILD_SHA256_AT_DEPLOY_TIME" ]]; then
    echo "FATAL: SHA_EXPECTED is still the placeholder string."
    echo "  Paste the real SHA from r49_build_zip.py output before running."
    exit 1
fi

# ---- step 1: backup current app dir + DB -------------------------------
echo "[1/8] backup → $BACKUP"
if [[ -d "$APP" ]]; then
    mv "$APP" "$BACKUP"
else
    echo "  no prior app dir to backup (first deploy?)"
    mkdir -p "$(dirname "$APP")"
fi
mkdir -p "$APP"

# DB backup — copy the live DB before the migration touches it.  The
# cron may write one more row between this copy and the migration,
# but cron overlap dups are exactly what the migration cleans up.
echo "[1/8] DB backup → $DB_BACKUP"
# Detect DB path from systemd override, else default.
DB_LIVE="/var/lib/dorm-power-monitor/dorm.db"
if [[ -n "$DB_PATH_ENV" ]]; then
    DB_LIVE="$DB_PATH_ENV"
fi
if [[ -f "$DB_LIVE" ]]; then
    cp "$DB_LIVE" "$DB_BACKUP"
    echo "  DB copied ($(stat -c%s "$DB_LIVE" 2>/dev/null || stat -f%z "$DB_LIVE") bytes)"
else
    echo "  WARN: no live DB at $DB_LIVE — fresh install?"
fi

# ---- step 2: verify zip sha256 -----------------------------------------
echo "[2/8] sha256 verify"
SHA_ACTUAL="$(sha256sum "$ZIP" | awk '{print $1}')"
if [[ "$SHA_ACTUAL" != "$SHA_EXPECTED" ]]; then
    echo "FATAL: zip sha256 mismatch"
    echo "  expected: $SHA_EXPECTED"
    echo "  actual:   $SHA_ACTUAL"
    if [[ -d "$BACKUP" ]]; then
        rm -rf "$APP"
        mv "$BACKUP" "$APP"
        echo "  restored backup, aborting"
    fi
    exit 1
fi
echo "  OK ($SHA_ACTUAL)"

# ---- step 3: unzip -----------------------------------------------------
echo "[3/8] unzip → $APP"
unzip -q "$ZIP" -d "$(dirname "$APP")"
echo "  $(find "$APP" -type f | wc -l) files extracted"

# ---- step 4: restore .env ----------------------------------------------
echo "[4/8] restore .env"
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
echo "[5/8] chown dorm:dorm"
if id dorm >/dev/null 2>&1; then
    chown -R dorm:dorm "$APP"
else
    echo "  WARN: 'dorm' user not found, skipping chown"
fi

# Clear stale __pycache__ from the prior install so the new code is
# what's loaded — without this, Python can serve a cached .pyc built
# from the pre-R49 source after the file content changes.
find "$APP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---- step 6: stop dorm-web + run migration -----------------------------
# dorm-cron.timer can stay running: cron writes at minute granularity
# and any dup it creates during the migration is reaped by the dedupe
# SQL anyway.  dorm-web MUST stop because it holds a long-lived Flask
# request connection that could write into a half-migrated table.
echo "[6/8] stop $SERVICE & run migration"
if systemctl is-active --quiet "$SERVICE"; then
    systemctl stop "$SERVICE"
    sleep 2
fi
echo "  $SERVICE stopped"

if [[ -f "$DB_LIVE" ]]; then
    if [[ -n "$DB_PATH_ENV" ]]; then
        DB_PATH="$DB_LIVE" python3 "$MIGRATION" "$DB_LIVE"
    else
        python3 "$MIGRATION"
    fi
    MIGRATE_RC=$?
    if [[ $MIGRATE_RC -ne 0 ]]; then
        echo "FATAL: migration exited $MIGRATE_RC — restoring DB backup"
        cp "$DB_BACKUP" "$DB_LIVE"
        echo "  DB restored from $DB_BACKUP"
        # Don't restart dorm-web here — operator must investigate.
        exit $MIGRATE_RC
    fi
else
    echo "  no live DB — skipping migration (fresh install)"
fi

# ---- step 7: restart dorm-web ------------------------------------------
echo "[7/8] restart $SERVICE"
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

# ---- step 8: verify ----------------------------------------------------
echo "[8/8] verify"
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r49_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  healthz 200 OK"
        python3 - <<'PY'
import json, sys
with open("/tmp/r49_healthz.json") as f:
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
        echo "=== R49 deploy SUCCESS ==="
        echo
        echo "Browser: Ctrl+Shift+R to hard-refresh dashboard"
        echo "Expected: line chart should now be monotonically"
        echo "          non-increasing (no sawtooth at the same ts)."
        echo
        echo "Verify the UNIQUE constraint landed:"
        echo "  sqlite3 /var/lib/dorm-power-monitor/dorm.db"
        echo "  .schema records"
        echo "  -- expect 'ts TEXT NOT NULL UNIQUE'"
        echo
        echo "Verify dedupe succeeded:"
        echo "  SELECT COUNT(*) AS total, COUNT(DISTINCT ts) AS unique_ts FROM records;"
        echo "  -- expect total == unique_ts (no duplicates remain)"
        echo
        echo "If something looks off:"
        echo "  1. Restore DB: cp $DB_BACKUP /var/lib/dorm-power-monitor/dorm.db"
        echo "  2. Roll back app: rm -rf $APP && mv $BACKUP $APP"
        echo "  3. systemctl restart $SERVICE"
        exit 0
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

echo "FATAL: healthz never returned 200"
echo "  last body:"
cat /tmp/r49_healthz.json 2>/dev/null || echo "  (none)"
exit 1