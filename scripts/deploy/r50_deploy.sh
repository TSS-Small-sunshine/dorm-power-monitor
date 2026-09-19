#!/usr/bin/env bash
# r50_deploy.sh — Round 50 documented.
#
# Server-side deploy script for Round 50 — UI 重设计 (DeepSeek 风格).
#
# Operational difference vs R49:
#   * NO migration.  R50 is a pure UI redesign of web.py INDEX_HTML.
#     db.py, dorm_power.py, feishu_bot.py are UNCHANGED.
#   * NO env knob.  No new variables in .env.example.
#   * dorm-web must be restarted so Flask re-reads the new HTML string
#     (Python doesn't hot-reload the module-level INDEX_HTML constant).
#   * dorm-cron.timer can stay running throughout — cron never holds a
#     long DB lock and writes are unchanged from R49 (INSERT OR REPLACE
#     with UNIQUE(ts) still in place).
#
# Usage:
#   bash r50_deploy.sh
#
# Output:
#   - exit 0 on success (deployment complete + service up + healthz 200)
#   - non-zero on any pre-flight failure (sha256 mismatch, unzip error,
#     service start failure, healthz timeout)
#
# See TECHNICAL.md for the broader deploy story.
#
# Round 50 — UI 重设计 (DeepSeek 探索未知 风格)
# ------------------------------------------------
# Pre-R50 timeline:
#   web.py INDEX_HTML  → MCSM 风格深色 dashboard
#                          sidebar 240px + topbar + card-grid
#                          深色背景 (#1a1a1a) + 静态卡片
#                          5 个 stat card (剩余/小时/抄表/日均/月度预测)
# Pre-R50 user-visible pain:
#   - 视觉密度高, 大量功能块挤在视口里
#   - sidebar 占视口宽度 (~240px) 在小屏幕上挤压主区
#   - 数字字号 24px, 不够醒目
#   - 主题偏深, 用户反馈"看不清楚大字"
#
# Post-R50 timeline:
#   web.py INDEX_HTML  → DeepSeek "探索未知" 风格
#                          顶部 tab nav (`.topnav` + `.nav-tab`)
#                          Hero 区 72px gradient text 巨字
#                          玻璃 card (backdrop-filter blur(24px))
#                          浅蓝渐变背景 (linear-gradient 135deg)
#                          4 个 stat card 玻璃网格 (剩余/月度/小时/日均)
#                          浅色 / 暗色 双主题 + 6 accent 色
# Post-R50 expectation:
#   - 大字 hero 区一眼看到剩余电量
#   - 顶部 tab 一键切换 4 个 section (替代 sidebar)
#   - 玻璃 card 在浅蓝背景上有浮起感
#   - 暗色模式保留, 但默认是亮色 (用户反馈深色看不清)
#   - 所有 R46-R49 修复保留 (datetime T→space, +08:00, UNIQUE ts)
#   - 主题/强调色用户可自选, localStorage 记忆
#   - 移动端底部 nav 保留, 响应式断点 < 768px

set -euo pipefail

# SHA256 placeholder; the orchestrator pastes the real value in after
# r50_build_zip.py produces the artifact.  Leave the literal string
# so a forgotten replace is loud at edit time.
SHA_EXPECTED="REPLACE_WITH_R50_BUILD_SHA256_AT_DEPLOY_TIME"
ZIP="$HOME/dorm-power-monitor.zip"
APP="$HOME/dorm-power-monitor"
BACKUP="$HOME/dorm-power-monitor.r49.bak.$(date -u +%Y%m%dT%H%M%SZ)"
HEALTHZ="http://127.0.0.1:8000/healthz"
SERVICE="dorm-web.service"

echo "=== R50 deploy: UI redesign (DeepSeek style) ==="
echo "ZIP:           $ZIP"
echo "APP:           $APP"
echo "BACKUP:        $BACKUP"
echo "SHA256 expect: $SHA_EXPECTED"
echo

# Sanity check the SHA placeholder was replaced.
if [[ "$SHA_EXPECTED" == "REPLACE_WITH_R50_BUILD_SHA256_AT_DEPLOY_TIME" ]]; then
    echo "FATAL: SHA_EXPECTED is still the placeholder string."
    echo "  Paste the real SHA from r50_build_zip.py output before running."
    exit 1
fi

# ---- step 1: backup current app dir ---------------------------------
echo "[1/7] backup → $BACKUP"
if [[ -d "$APP" ]]; then
    mv "$APP" "$BACKUP"
else
    echo "  no prior app dir to backup (first deploy?)"
    mkdir -p "$(dirname "$APP")"
fi
mkdir -p "$APP"

# ---- step 2: verify zip sha256 ---------------------------------------
echo "[2/7] sha256 verify"
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

# ---- step 3: unzip ---------------------------------------------------
echo "[3/7] unzip → $APP"
unzip -q "$ZIP" -d "$(dirname "$APP")"
echo "  $(find "$APP" -type f | wc -l) files extracted"

# ---- step 4: restore .env --------------------------------------------
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

# ---- step 5: chown ---------------------------------------------------
echo "[5/7] chown dorm:dorm"
if id dorm >/dev/null 2>&1; then
    chown -R dorm:dorm "$APP"
else
    echo "  WARN: 'dorm' user not found, skipping chown"
fi

# Clear stale __pycache__ from the prior install so the new code is
# what's loaded — without this, Python can serve a cached .pyc built
# from the pre-R50 source after the file content changes.
find "$APP" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

# ---- step 6: restart dorm-web ----------------------------------------
# R50 has no migration.  Just restart so Flask re-imports the new
# INDEX_HTML string.  dorm-cron.timer can stay running — cron writes
# at minute granularity and R49's INSERT OR REPLACE + UNIQUE(ts)
# guarantees the new code path stays sane.
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

# ---- step 7: verify --------------------------------------------------
echo "[7/7] verify"
for i in 1 2 3 4 5; do
    HTTP="$(curl -s -o /tmp/r50_healthz.json -w '%{http_code}' "$HEALTHZ" || true)"
    if [[ "$HTTP" == "200" ]]; then
        echo "  healthz 200 OK"
        python3 - <<'PY'
import json, sys
with open("/tmp/r50_healthz.json") as f:
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
        echo "=== R50 deploy SUCCESS ==="
        echo
        echo "Browser: Ctrl+Shift+R to hard-refresh dashboard"
        echo "Expected visual:"
        echo "  - 顶部 tab nav (4 个 tab 居中:概览/历史/违规/电表)"
        echo "  - Hero 区大字 72px 渐变蓝剩余电量"
        echo "  - 玻璃 card 4 个 stat (本月费用 / 1小时 / 日均 / 当前)"
        echo "  - 主 chart 居中 320px 高, 蓝色填充"
        echo "  - 浅蓝渐变背景 (linear-gradient 135deg)"
        echo
        echo "Verify the new HTML is live:"
        echo "  curl -s http://127.0.0.1:8000/ | grep -E 'topnav|nav-tab|hero-value'"
        echo "  -- expect topnav + nav-tab + hero-value in the response"
        echo
        echo "If something looks off (rare — no migration to roll back):"
        echo "  rm -rf $APP && mv $BACKUP $APP && systemctl restart $SERVICE"
        exit 0
    fi
    echo "  attempt $i: HTTP=$HTTP, retrying in 2s..."
    sleep 2
done

echo "FATAL: healthz never returned 200"
echo "  last body:"
cat /tmp/r50_healthz.json 2>/dev/null || echo "  (none)"
exit 1