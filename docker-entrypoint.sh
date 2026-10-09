#!/usr/bin/env bash
# =============================================================================
# StarWatt 星瓦 —— 容器入口（6.4 / Q19 / B4）
# =============================================================================
# 只做两件「容器特有」的事，然后 exec 交棒给 gunicorn：
#
# 1. **首启生成随机管理员密码**并打印一次（Q19）
#    用后即焚：应用建号后会把 BOOTSTRAP_ADMIN_PASSWORD 从 .env 抹掉；
#    容器里环境变量抹不掉，但建号只在 users 表为空时发生 —— 幂等。
#
# 2. **持久化 FLASK_SECRET_KEY**（这一步是必须的，不是锦上添花）
#    密钥缺失时 starwatt/auth/secret.py 会自己生成并**尝试写回 .env**；
#    容器里 .env 通常不存在或只读 → 它只能退化成「临时密钥」，于是
#    **每次重启都会换密钥**，后果是：
#      · 配置中心里所有加密的 secret（openid / 飞书 / QQ 凭据）永久解不开
#      · 所有已签发的会话立刻失效
#    所以入口把密钥生成一次、写进**数据卷**（/data/.flask_secret_key），
#    之后每次启动都复用它。
# =============================================================================
set -euo pipefail

#: 数据目录（与 Dockerfile 的 ENV 一致；允许用户覆盖）
DATA_DIR="${DORM_DATA_DIR:-/data}"
DB_FILE="${DB_PATH:-$DATA_DIR/records.db}"
SECRET_FILE="$DATA_DIR/.flask_secret_key"

mkdir -p "$DATA_DIR" 2>/dev/null || true

# --- 随机串生成（只用 /dev/urandom，不依赖 openssl）-------------------------
# ⚠️ 不要用 `tr -dc ... </dev/urandom | head -c N`：head 提前退出会让 tr 收到
#    SIGPIPE，配合 `set -o pipefail` 会直接把脚本打死（经典 bash 坑）。
_hex() {
    head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
}

# 生成满足强度策略的密码：≥8 位、含大写、数字、特殊字符
# （规则见 starwatt/auth/password.py: validate_strength）
_new_password() {
    printf '%s' "$(_hex 16 | cut -c1-16)Aa1!"
}

# --- 1. 会话/加密密钥：缺失则生成一次并落盘到数据卷 -------------------------
if [ -z "${FLASK_SECRET_KEY:-}" ]; then
    if [ -f "$SECRET_FILE" ]; then
        FLASK_SECRET_KEY="$(cat "$SECRET_FILE")"
        echo "[entrypoint] 复用数据卷里的 FLASK_SECRET_KEY"
    else
        FLASK_SECRET_KEY="$(_hex 32)"
        printf '%s' "$FLASK_SECRET_KEY" >"$SECRET_FILE"
        chmod 600 "$SECRET_FILE" 2>/dev/null || true
        echo "[entrypoint] 已生成 FLASK_SECRET_KEY 并保存到 $SECRET_FILE"
        echo "[entrypoint] ⚠️ 这个文件丢了，配置中心里加密的凭据就无法解密 —— 请备份数据卷"
    fi
    export FLASK_SECRET_KEY
fi

# --- 2. 首启：生成一次性管理员密码 ----------------------------------------
FIRST_RUN=0
if [ ! -f "$DB_FILE" ] && [ -z "${BOOTSTRAP_ADMIN_PASSWORD:-}" ]; then
    BOOTSTRAP_ADMIN_PASSWORD="$(_new_password)"
    export BOOTSTRAP_ADMIN_PASSWORD
    FIRST_RUN=1
fi

if [ "$FIRST_RUN" = "1" ]; then
    cat <<EOF

============================================================
  StarWatt 星瓦 · 首次启动
------------------------------------------------------------
  管理员账号：admin
  初始密码  ：${BOOTSTRAP_ADMIN_PASSWORD}

  ⚠️ 这串密码只在这里打印一次，请立刻记下。
     首次登录会被强制修改密码。
     也可以用环境变量 BOOTSTRAP_ADMIN_PASSWORD 自己指定。
============================================================

EOF
fi

exec "$@"
