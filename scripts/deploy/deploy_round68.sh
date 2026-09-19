#!/usr/bin/env bash
# deploy_round68.sh — Round 68 deploy automation
#
# Replaces R51c baseline with the full R60-R67 deliverable stack on
# the production server (Ubuntu 24.04+ at iZn4acqrskey97uuyo31m1Z,
# nginx → systemd dorm-power-monitor-web → Flask web.py on :5000).
#
# Operational difference vs R51c:
#   * MUST run as root (or with sudo) — touches systemd, /opt paths,
#     and chowns the deploy dir.
#   * MUST preserve .env — R63 added FLASK_SECRET_KEY which the
#     operator pre-fills; losing it invalidates all live sessions.
#   * MUST install new pip deps (pydantic + bcrypt) — both are R61 / R63
#     additions, not in R51c's requirements.txt.
#   * MUST run `python -c 'import db; db.init()'` — idempotent, creates
#     the 3 new tables (users / login_attempts / audit_log) plus the
#     6 legacy tables. Safe to re-run on every deploy.
#   * MUST restart dorm-power-monitor-web — Flask re-reads INDEX_HTML,
#     routes, and the auth blueprints on each launch.
#   * dorm-cron.timer can stay running — cron writes at minute
#     granularity and any dup it creates during the deploy is reaped
#     by R49's UNIQUE(ts) constraint + INSERT OR REPLACE.
#
# Usage:
#   1. Upload the zip + sha256 to the server:
#        scp dorm-power-monitor-r68.zip dorm-power-monitor-r68.zip.sha256 \
#            user@<server>:/tmp/
#   2. Edit the ZIP_URL + SHA_URL placeholders below (or move the zip
#      to /tmp/dorm-r68.zip and the sha to /tmp/dorm-r68.zip.sha256).
#   3. Run as root:
#        sudo bash deploy_round68.sh
#
# Output:
#   - exit 0 on success (deploy complete + deps installed + db.init OK
#     + service active + /api/auth/csrf returned 200)
#   - non-zero on any pre-flight failure (sha256 mismatch, unzip error,
#     pip install failure, db.init failure, service start failure,
#     HTTP timeout).
#
# Rollback (3 lines):
#   sudo systemctl stop dorm-power-monitor-web
#   sudo rsync -av "$CURRENT_BACKUP/" /opt/dorm-power-monitor/
#   sudo systemctl start dorm-power-monitor-web
#
# See docs/TECHNICAL.md for the broader deploy story and
# scripts/deploy/r68_env_migration.md for the .env upgrade playbook.

set -euo pipefail

# ============================================================================
# Configuration — EDIT THESE BEFORE RUNNING
# ============================================================================
PROJECT_DIR="/opt/dorm-power-monitor"
BACKUP_DIR="/opt/dorm-power-monitor-backups"

# Where the zip lives on the server.  Two common patterns:
#   (a) operator scp'd it to /tmp/dorm-r68.zip
#   (b) operator uploaded to an internal artifact store
# Either way, ZIP_PATH must be readable by root and SHA_PATH must be the
# matching .sha256 sidecar produced by build_round68.py.
ZIP_PATH="/tmp/dorm-r68.zip"
SHA_PATH="/tmp/dorm-r68.zip.sha256"

# If you uploaded to a URL instead of scp-ing, uncomment and set:
# ZIP_URL="https://artifacts.internal/dorm-power-monitor-r68.zip"
# SHA_URL="https://artifacts.internal/dorm-power-monitor-r68.zip.sha256"
ZIP_URL=""
SHA_URL=""

# systemd unit name for the Flask web app.
SERVICE="dorm-power-monitor-web.service"

# Cron unit (leave running throughout — R49's UNIQUE(ts) handles dup writes).
CRON_TIMER="dorm-power-monitor-cron.timer"

# Health check endpoint.  R68 exposes a CSRF probe at /api/auth/csrf
# that returns 200 even without auth, so we can verify the new code
# is up before declaring success.
HEALTHZ="http://127.0.0.1:5000/api/auth/csrf"

# Owner for the project dir.  On Debian/Ubuntu nginx + Flask convention
# it's www-data.  If you run a dedicated dorm user, change this.
APP_USER="www-data"
APP_GROUP="www-data"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
CURRENT_BACKUP="$BACKUP_DIR/dorm-power-monitor-r51c-$TIMESTAMP"
LOG="/var/log/dorm-power-monitor-deploy-r68.log"

# ============================================================================
# Pre-flight checks
# ============================================================================
echo "=== R68 deploy: R51c → R60-R67 bundle ==="
echo "PROJECT_DIR:  $PROJECT_DIR"
echo "BACKUP_DIR:   $BACKUP_DIR"
echo "BACKUP name:  $CURRENT_BACKUP"
echo "SERVICE:      $SERVICE"
echo "HEALTHZ:      $HEALTHZ"
echo "ZIP_PATH:     $ZIP_PATH"
echo "SHA_PATH:     $SHA_PATH"
echo "Log:          $LOG"
echo

# Must be root (touches /etc/systemd, /opt paths, chown).
if [[ $EUID -ne 0 ]]; then
    echo "FATAL: this script must run as root (or via sudo)."
    echo "  Reason: systemctl restart, chown -R, mkdir /opt."
    exit 1
fi

# The zip + sha must exist (or we have to download from ZIP_URL).
if [[ -z "$ZIP_URL" && ! -f "$ZIP_PATH" ]]; then
    echo "FATAL: zip not found at $ZIP_PATH and ZIP_URL is empty."
    echo "  Either scp the zip into place or set ZIP_URL."
    exit 1
fi
if [[ -z "$SHA_URL" && ! -f "$SHA_PATH" ]]; then
    echo "FATAL: sha256 sidecar not found at $SHA_PATH and SHA_URL is empty."
    echo "  Either scp the .sha256 into place or set SHA_URL."
    exit 1
fi

mkdir -p "$(dirname "$LOG")"
exec > >(tee -a "$LOG") 2>&1
echo
echo "--- deploy started $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"

# ============================================================================
# Step 1: stop the web service so no concurrent writer hits the DB
# ============================================================================
echo
echo "[1/8] stop $SERVICE"
if systemctl is-active --quiet "$SERVICE"; then
    systemctl stop "$SERVICE"
    sleep 2
    echo "  $SERVICE stopped"
else
    echo "  $SERVICE was not running — proceeding"
fi

# ============================================================================
# Step 2: backup the current install
# ============================================================================
echo
echo "[2/8] backup → $CURRENT_BACKUP"
mkdir -p "$BACKUP_DIR"
if [[ -d "$PROJECT_DIR" ]]; then
    cp -a "$PROJECT_DIR" "$CURRENT_BACKUP"
    # Belt-and-braces: also copy .env separately because the cp -a
    # above already grabbed it, but if the operator pre-renamed it
    # (e.g. .env.live) the dedicated copy below is the canonical
    # rollback artefact.
    if [[ -f "$PROJECT_DIR/.env" ]]; then
        cp "$PROJECT_DIR/.env" "$CURRENT_BACKUP/.env.backup"
    fi
    echo "  backup complete ($(du -sh "$CURRENT_BACKUP" 2>/dev/null | awk '{print $1}'))"
else
    echo "  WARN: $PROJECT_DIR did not exist — fresh install?"
    mkdir -p "$PROJECT_DIR"
fi

# ============================================================================
# Step 3: download + sha256 verify the zip
# ============================================================================
echo
echo "[3/8] fetch + sha256 verify"
if [[ -n "$ZIP_URL" ]]; then
    echo "  downloading $ZIP_URL → $ZIP_PATH"
    curl -fsSL -o "$ZIP_PATH" "$ZIP_URL"
    if [[ -n "$SHA_URL" ]]; then
        curl -fsSL -o "$SHA_PATH" "$SHA_URL"
    fi
fi
if [[ ! -f "$ZIP_PATH" ]]; then
    echo "FATAL: zip still missing after fetch attempt"
    exit 1
fi

SHA_EXPECTED="$(awk '{print $1}' "$SHA_PATH")"
SHA_ACTUAL="$(sha256sum "$ZIP_PATH" | awk '{print $1}')"
echo "  expected sha256: $SHA_EXPECTED"
echo "  actual sha256:   $SHA_ACTUAL"
if [[ "$SHA_EXPECTED" != "$SHA_ACTUAL" ]]; then
    echo "FATAL: zip sha256 mismatch — refusing to deploy."
    echo "  The zip is corrupted or not the one we built."
    exit 1
fi
echo "  OK"

# ============================================================================
# Step 4: unzip the new bundle into a staging dir, then rsync in place
# ============================================================================
echo
echo "[4/8] unzip + rsync into $PROJECT_DIR"
STAGING="$(mktemp -d -t dorm-r68-XXXXXX)"
trap 'rm -rf "$STAGING"' EXIT

unzip -q "$ZIP_PATH" -d "$STAGING"
EXTRACTED="$(find "$STAGING" -type f | wc -l)"
echo "  extracted $EXTRACTED files from zip"

# Preserve .env by stashing it before the rsync.
ENV_PRESERVE=""
if [[ -f "$PROJECT_DIR/.env" ]]; then
    ENV_PRESERVE="$(mktemp -t .env.r68.preserve.XXXXXX)"
    cp "$PROJECT_DIR/.env" "$ENV_PRESERVE"
    echo "  preserved existing .env ($(wc -c < "$ENV_PRESERVE") bytes)"
fi

# rsync with excludes that the operator never wants clobbered:
#   .env              — operator-managed, contains FLASK_SECRET_KEY
#   __pycache__       — will be rebuilt on first launch
#   *.db              — production SQLite, lives at $DB_PATH
#   .venv             — rebuilt deps live in this exact dir; rsync would
#                       clobber pip-installed site-packages
#   _tmp_*            — operator scratch files from previous sessions
#   records.db        — explicit name match (covers ./records.db default)
rsync -av --update \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.db' \
    --exclude='records.db' \
    --exclude='.venv' \
    --exclude='_tmp_*' \
    "$STAGING/dorm-power-monitor/" "$PROJECT_DIR/"

# Restore .env.
if [[ -n "$ENV_PRESERVE" ]]; then
    cp "$ENV_PRESERVE" "$PROJECT_DIR/.env"
    rm -f "$ENV_PRESERVE"
    chmod 600 "$PROJECT_DIR/.env"
    chown "$APP_USER:$APP_GROUP" "$PROJECT_DIR/.env"
    echo "  .env restored + chown $APP_USER:$APP_GROUP + chmod 600"
fi

# Clear stale __pycache__ from the prior install so the new code is
# what's loaded — without this, Python can serve a .pyc built from the
# pre-R68 source after the file content changes.
find "$PROJECT_DIR" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ============================================================================
# Step 5: install new pip deps (pydantic >= 2.0 + bcrypt >= 4.0)
# ============================================================================
echo
echo "[5/8] pip install -r requirements.txt (pydantic + bcrypt new in R61/R63)"
if [[ ! -d "$PROJECT_DIR/.venv" ]]; then
    echo "FATAL: $PROJECT_DIR/.venv does not exist — re-run setup.sh first."
    exit 1
fi
sudo -u "$APP_USER" bash -c "
    cd '$PROJECT_DIR'
    source .venv/bin/activate
    pip install --upgrade pip --quiet
    pip install -r requirements.txt --quiet
"
echo "  deps installed"

# ============================================================================
# Step 6: idempotent DB schema init
# ============================================================================
echo
echo "[6/8] db.init() — idempotent, creates 6 legacy + 3 new tables"
sudo -u "$APP_USER" bash -c "
    cd '$PROJECT_DIR'
    source .venv/bin/activate
    python -c 'import db; db.init()'
"
echo "  DB schema OK"

# ============================================================================
# Step 7: chown + restart
# ============================================================================
echo
echo "[7/8] chown + restart $SERVICE"
chown -R "$APP_USER:$APP_GROUP" "$PROJECT_DIR"

if systemctl is-active --quiet "$SERVICE"; then
    systemctl restart "$SERVICE"
else
    systemctl start "$SERVICE"
fi
sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    echo "FATAL: $SERVICE failed to start — check journalctl -u $SERVICE"
    echo "  No rollback attempted — operator must investigate."
    exit 1
fi
echo "  $SERVICE is active"

# ============================================================================
# Step 8: verify the new build is live
# ============================================================================
echo
echo "[8/8] verify $HEALTHZ returns 200"
SUCCESS=0
for i in 1 2 3 4 5 6 7 8; do
    HTTP="$(curl -s -o /tmp/r68_csrf.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  attempt $i: HTTP 200 — R68 is live"
        SUCCESS=1
        break
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

if [[ $SUCCESS -eq 0 ]]; then
    echo
    echo "FATAL: $HEALTHZ never returned 200 after 8 attempts."
    echo "  Last body:"
    cat /tmp/r68_csrf.json 2>/dev/null || echo "  (none)"
    echo
    echo "  Tail of service log:"
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    echo
    echo "  No rollback attempted — operator must investigate."
    exit 1
fi

# ============================================================================
# Done
# ============================================================================
echo
echo "=== R68 deploy SUCCESS ==="
echo
echo "Old R51c backup: $CURRENT_BACKUP"
echo "Logs:            $LOG"
echo
echo "Browser steps:"
echo "  1. Open  http://school.tssplus.top/  (or your server_name)"
echo "  2. You'll be redirected to /oobe (the R65 first-run wizard)"
echo "  3. Walk through 6 steps: username → password → accent → health"
echo "  4. After 'Done' you'll land on /admin"
echo
echo "Sanity checks:"
echo "  * Hard-refresh dashboard (Ctrl+Shift+R) to pick up new CSS"
echo "  * curl -fsSL http://127.0.0.1:5000/api/auth/csrf    # expect 200"
echo "  * curl -fsSL http://127.0.0.1:5000/oobe            # expect 200 + OOBE step 1"
echo "  * sqlite3 /var/lib/dorm-power-monitor/records.db '.tables'"
echo "    -- expect: audit_log finance login_attempts meta meter records run_status users violation"
echo
echo "Rollback (3 lines if anything looks off):"
echo "  sudo systemctl stop $SERVICE"
echo "  sudo rsync -av $CURRENT_BACKUP/ $PROJECT_DIR/"
echo "  sudo systemctl start $SERVICE"
echo
echo "OOBE note:"
echo "  The wizard session lives in Flask's server memory, so a restart"
echo "  clears any in-progress OOBE state. If the operator needs to"
echo "  re-trigger OOBE manually: sqlite3 ... \"DELETE FROM meta WHERE key='oobe_completed';\""
echo "  -- then visit /oobe again."
echo
echo "Env-var upgrade playbook: scripts/deploy/r68_env_migration.md"
echo
echo "--- deploy finished $(date -u +%Y-%m-%dT%H:%M:%SZ) ---"
exit 0