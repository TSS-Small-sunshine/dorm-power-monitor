#!/usr/bin/env bash
# =============================================================================
# StarWatt 星瓦 —— 裸机一键安装 / 升级（6.3 / N3 / Q19）
# =============================================================================
# 适用：Debian / Ubuntu（amd64 或 arm64）。armv7 请走 Docker（见 docs/DEPLOY.md）。
#
#   sudo ./install.sh                 # 首次安装（默认装到 /opt/dorm-power-monitor）
#   sudo ./install.sh --upgrade       # 升级：拉代码 + 重建依赖与前端，**保留 .env 与数据**
#   sudo ./install.sh --no-frontend   # 不构建前端（只用 API / 之后再补构建）
#
# 三条安全铁律（改这个脚本前先读）
# =============================================================================
# 1. **绝不覆盖已存在的 `.env`**
#    `.env` 里的 FLASK_SECRET_KEY 同时用于加密配置中心里的 secret。
#    重新生成它 = 用户所有凭据永久解不开（数据丢失级事故）。
# 2. **绝不动数据目录**
#    升级只重建代码与依赖；`records.db` 原样保留（零迁移）。
# 3. **随机密码只打印一次**
#    首启由应用建号后立即从 `.env` 抹掉（用后即焚）。
# =============================================================================
set -euo pipefail

APP_NAME="starwatt"
INSTALL_DIR="${INSTALL_DIR:-/opt/dorm-power-monitor}"
DATA_DIR="${DATA_DIR:-/var/lib/dorm-power-monitor}"
SERVICE_USER="${SERVICE_USER:-www-data}"
SERVICE_NAME="dorm-web"
DEFAULT_PORT=5000

UPGRADE=0
BUILD_FRONTEND=1
PORT=""

usage() {
    cat <<EOF
StarWatt 星瓦 · 裸机安装脚本

用法：sudo ./install.sh [选项]

  --upgrade          升级已有安装（保留 .env 与数据）
  --no-frontend      跳过前端构建（不装 Node 也能用 API）
  --dir PATH         安装目录（默认 $INSTALL_DIR）
  --data-dir PATH    数据目录（默认 $DATA_DIR）
  --user NAME        运行服务的系统用户（默认 $SERVICE_USER）
  --port N           监听端口（默认 $DEFAULT_PORT，仅绑 127.0.0.1）
  -h, --help         显示本帮助
EOF
}

log() { printf '\033[1;32m[starwatt]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[starwatt]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[starwatt] 错误：\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 参数解析
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --upgrade) UPGRADE=1 ;;
        --no-frontend) BUILD_FRONTEND=0 ;;
        --dir) INSTALL_DIR="${2:?--dir 需要一个路径}"; shift ;;
        --data-dir) DATA_DIR="${2:?--data-dir 需要一个路径}"; shift ;;
        --user) SERVICE_USER="${2:?--user 需要一个用户名}"; shift ;;
        --port) PORT="${2:?--port 需要一个端口}"; shift ;;
        -h | --help) usage; exit 0 ;;
        *) die "未知参数：$1（用 --help 看用法）" ;;
    esac
    shift
done

PORT="${PORT:-$DEFAULT_PORT}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---------------------------------------------------------------------------
# 随机串 / 随机密码（不依赖 openssl）
# ---------------------------------------------------------------------------
_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

# 满足后端强度策略：≥8 位 + 大写 + 数字 + 特殊字符（starwatt/auth/password.py）
_new_password() { printf '%s' "$(_hex 16 | cut -c1-16)Aa1!"; }

# ---------------------------------------------------------------------------
# 前置检查
# ---------------------------------------------------------------------------
[ "$(id -u)" = "0" ] || die "请用 root 运行（sudo ./install.sh）：要装 systemd 服务"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" >/dev/null 2>&1; then
        if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
            PYTHON_BIN="$(command -v "$candidate")"
            break
        fi
    fi
done
[ -n "$PYTHON_BIN" ] || die "需要 Python ≥ 3.10（apt install python3 python3-venv）"
log "Python：$PYTHON_BIN（$("$PYTHON_BIN" --version 2>&1)）"

if [ "$BUILD_FRONTEND" = "1" ]; then
    if ! command -v node >/dev/null 2>&1; then
        warn "没找到 node —— 无法构建前端。用 --no-frontend 只装 API，"
        warn "或在装好 Node ≥ 20 后重跑本脚本。"
        BUILD_FRONTEND=0
    else
        NODE_MAJOR="$(node -v | sed 's/^v//' | cut -d. -f1)"
        if [ "$NODE_MAJOR" -lt 20 ]; then
            warn "Node $(node -v) 版本过低（需要 ≥ 20），跳过前端构建"
            BUILD_FRONTEND=0
        else
            log "Node：$(node -v)"
        fi
    fi
fi

if ! command -v systemctl >/dev/null 2>&1; then
    die "没有 systemd —— 本脚本按 systemd 服务安装；容器请用 docker compose"
fi

# ---------------------------------------------------------------------------
# 同步代码到安装目录
# ---------------------------------------------------------------------------
if [ "$UPGRADE" = "1" ] && [ -d "$INSTALL_DIR/.git" ]; then
    log "升级：拉取最新代码（保留 .env 与数据）"
    git -C "$INSTALL_DIR" pull --ff-only
elif [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
    log "同步代码：$SRC_DIR → $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    # 只同步运行需要的东西；不动 .env / 数据 / 虚拟环境
    for item in starwatt web.py gunicorn.conf.py docker-entrypoint.sh \
        requirements.txt pyproject.toml frontend static deploy scripts; do
        if [ -e "$SRC_DIR/$item" ]; then
            cp -a "$SRC_DIR/$item" "$INSTALL_DIR/"
        fi
    done
fi
[ -f "$INSTALL_DIR/requirements.txt" ] || die "$INSTALL_DIR 里没有 requirements.txt"

# ---------------------------------------------------------------------------
# Python 依赖（虚拟环境）
# ---------------------------------------------------------------------------
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    log "创建虚拟环境：$INSTALL_DIR/.venv"
    "$PYTHON_BIN" -m venv "$INSTALL_DIR/.venv" ||
        die "创建 venv 失败（Debian/Ubuntu 需要 apt install python3-venv）"
fi
log "安装 Python 依赖（pillow / pycryptodome 需要 gcc 时会现场编译）"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# ---------------------------------------------------------------------------
# 前端产物（落到 static/）
# ---------------------------------------------------------------------------
if [ "$BUILD_FRONTEND" = "1" ]; then
    log "构建前端（npm ci && npm run build → static/）"
    (cd "$INSTALL_DIR/frontend" && npm ci --include=dev --no-fund --no-audit && npm run build)
else
    if [ ! -f "$INSTALL_DIR/static/index.html" ]; then
        warn "static/index.html 不存在 —— 现在只有 API 可用（没有网页界面）"
        warn "补构建：cd $INSTALL_DIR/frontend && npm ci && npm run build"
    fi
fi

# ---------------------------------------------------------------------------
# .env（铁律 1：已存在就**绝不**覆盖）
# ---------------------------------------------------------------------------
ENV_FILE="$INSTALL_DIR/.env"
NEW_PASSWORD=""
if [ -f "$ENV_FILE" ]; then
    log "沿用已存在的 .env（不会覆盖你的凭据与会话密钥）"
else
    log "生成 .env"
    NEW_PASSWORD="$(_new_password)"
    umask 077
    cat >"$ENV_FILE" <<EOF
# StarWatt 星瓦 —— 启动配置（Q15：这里只放「DB 打开前必需」的 4 项）
# 其余全部配置项在网页的「管理 → 配置中心」里改。
DB_PATH=$DATA_DIR/records.db
DORM_DATA_DIR=$DATA_DIR
FLASK_PORT=$PORT
FLASK_SECRET_KEY=$(_hex 32)

# 一次性首启密码：应用建号后会**立即把这一行删掉**（用后即焚）
BOOTSTRAP_ADMIN_PASSWORD=$NEW_PASSWORD
EOF
    chmod 600 "$ENV_FILE"
fi

# ---------------------------------------------------------------------------
# 数据目录（铁律 2：已存在的数据原样保留）
# ---------------------------------------------------------------------------
mkdir -p "$DATA_DIR"
if [ -f "$SRC_DIR/records.db" ] && [ ! -f "$DATA_DIR/records.db" ]; then
    log "发现现有 records.db —— 迁移到数据目录（零迁移，schema 未变）"
    cp -a "$SRC_DIR/records.db" "$DATA_DIR/records.db"
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$DATA_DIR" "$INSTALL_DIR" 2>/dev/null ||
    warn "chown 失败（用户 $SERVICE_USER 不存在？）—— 请手动确认目录属主"

# ---------------------------------------------------------------------------
# systemd 服务（按实际路径生成，而不是照抄仓库里的模板）
# ---------------------------------------------------------------------------
UNIT_SRC="$INSTALL_DIR/deploy/dorm-web.service"
[ -f "$UNIT_SRC" ] || die "缺少 $UNIT_SRC（部署单元模板）"
UNIT_DST="/etc/systemd/system/$SERVICE_NAME.service"
log "生成 systemd 单元：$UNIT_DST"

sed \
    -e "s#/opt/dorm-power-monitor#$INSTALL_DIR#g" \
    -e "s#/var/lib/dorm-power-monitor#$DATA_DIR#g" \
    -e "s#^User=.*#User=$SERVICE_USER#" \
    -e "s#^Group=.*#Group=$SERVICE_USER#" \
    "$UNIT_SRC" >"$UNIT_DST"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null
if [ "$UPGRADE" = "1" ]; then
    systemctl restart "$SERVICE_NAME"
else
    systemctl restart "$SERVICE_NAME" 2>/dev/null || systemctl start "$SERVICE_NAME"
fi

# ---------------------------------------------------------------------------
# 结果
# ---------------------------------------------------------------------------
sleep 2
if systemctl is-active --quiet "$SERVICE_NAME"; then
    log "服务已启动：$SERVICE_NAME"
else
    warn "服务没能起来，看日志：journalctl -u $SERVICE_NAME -n 50 --no-pager"
fi

echo
echo "============================================================"
echo " StarWatt 星瓦 安装完成"
echo "------------------------------------------------------------"
echo " 网页        ：http://127.0.0.1:$PORT/   （生产请用 nginx 反代 + HTTPS）"
echo " 配置文件    ：$ENV_FILE"
echo " 数据目录    ：$DATA_DIR"
echo " 服务        ：systemctl status $SERVICE_NAME"
echo " 日志        ：journalctl -u $SERVICE_NAME -f"
echo
if [ -n "$NEW_PASSWORD" ]; then
    echo " 管理员账号  ：admin"
    echo " 初始密码    ：$NEW_PASSWORD"
    echo
    echo " ⚠️ 这串密码只打印这一次；服务启动后它会被从 .env 里删掉（用后即焚）。"
    echo "    首次登录会被强制修改密码。请立刻记下。"
else
    echo " 管理员账号  ：沿用原有（.env 未被改动）"
fi
echo "============================================================"
echo
