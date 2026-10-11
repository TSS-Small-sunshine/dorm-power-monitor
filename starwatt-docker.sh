#!/usr/bin/env bash
# =============================================================================
# StarWatt 星瓦 —— Docker 助手（安装 / 升级 / 状态 / 日志 / 取初始密码 / 备份 / 恢复）
# =============================================================================
# 为什么要有这个脚本
# =================
#
# Docker 路径原本只有「三条命令 + 两句叮嘱」写在文档里，能跑，但四件事反复坑人：
#
# 1. **初始密码只打印一次**，用户没截到图就没了 —— 只能去翻 `docker compose logs`
#    再 grep 中文，容器重启过之后日志还可能被轮转掉。
# 2. **升级要手改 compose 文件里的 tag** —— 而 compose 是 `curl` 下来的，
#    用户改完下次又 curl 一次就覆盖回去了。所以 tag 必须落在 `.env` 里
#    （Compose 会自动读 `.env` 做变量替换），升级只动一行。
# 3. **备份要 WAL 感知**：`cp records.db` 在旧服务还在跑时会漏掉未 checkpoint
#    的事务（AUDIT_REPORT 的流程缺陷 #8）。而这个镜像里**没有 sqlite3 命令行**，
#    所以备份走容器内的 Python `sqlite3` 模块（stdlib，一定有）。
# 4. **升级前应该自动备份**，而不是靠人记得。
#
# 用法
# ====
#
#   ./starwatt-docker.sh install          # 首次安装（生成 .env + 起服务 + 打印初始密码）
#   ./starwatt-docker.sh upgrade [tag]    # 升级（默认 latest；先自动备份）
#   ./starwatt-docker.sh status           # 容器状态 + 版本 + 数据卷位置
#   ./starwatt-docker.sh logs [-f]        # 看日志
#   ./starwatt-docker.sh password         # 取初始密码（首登后会自动删除，见下）
#   ./starwatt-docker.sh backup [文件]     # 备份（库 + 密钥，tar.gz）
#   ./starwatt-docker.sh restore <文件>    # 恢复（会二次确认）
#   ./starwatt-docker.sh restart | down
#
# 三条铁律（与 install.sh 同源，改这个脚本前先读）
# ==============================================
# 1. **绝不覆盖已存在的 `.env`** —— 里面的 `FLASK_SECRET_KEY` 决定配置中心里
#    9 项密文能不能解开，重新生成 = 用户所有凭据永久解不开（数据丢失级事故）。
# 2. **备份必须走 `sqlite3` 的在线备份 API**（WAL 感知），不许 `cp` 数据库文件。
# 3. **恢复必须二次确认**，且先把旧库挪成 `.bak` 而不是直接覆盖。
# =============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
SERVICE="starwatt"
IMAGE_REPO="ghcr.io/tss-small-sunshine/starwatt"
DEFAULT_TAG="2.0.3-starcore"
DEFAULT_PORT=5000
#: 容器内跑着的用户 uid（Dockerfile 里 useradd --uid 10001）
CONTAINER_UID=10001
#: 一次性初始密码落在数据卷里的文件名（首登建号后由应用删除）
PASSWORD_FILE=".initial-admin-password"

log() { printf '\033[1;32m[starwatt]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[starwatt]\033[0m %s\n' "$*" >&2; }
die() { printf '\033[1;31m[starwatt] 错误：\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
    cat <<EOF
StarWatt 星瓦 · Docker 助手

用法：./starwatt-docker.sh <命令> [参数]

  install              首次安装：生成 .env、拉镜像/本地构建、起服务、打印初始密码
  upgrade [tag]        升级到指定 tag（默认 latest）。**升级前自动备份**
  status               容器状态 + /healthz 版本 + 数据卷在宿主机的真实路径
  logs [-f]            看日志（-f 跟随）
  password             取一次性初始密码（首登改密后该文件会被应用删除）
  backup [文件]        备份数据库与密钥为 tar.gz（WAL 感知，在线安全）
  restore <文件>       从备份恢复（需要输入 yes 二次确认）
  restart              重启服务
  down                 停止并移除容器（**不动数据卷**）
  help                 显示本帮助

环境变量：FLASK_PORT（默认 $DEFAULT_PORT）、BIND_ADDR（默认 127.0.0.1）、STARWATT_TAG
EOF
}

# ---------------------------------------------------------------------------
# Docker / Compose 探测
# ---------------------------------------------------------------------------
DC=()
detect_docker() {
    command -v docker >/dev/null 2>&1 || die "没找到 docker（先装 Docker Engine：https://docs.docker.com/engine/install/）"
    if docker compose version >/dev/null 2>&1; then
        DC=(docker compose)
    elif command -v docker-compose >/dev/null 2>&1; then
        DC=(docker-compose)
    else
        die "没找到 docker compose（Docker Compose v2 随 Docker Engine 一起装）"
    fi
}

#: 一律在脚本所在目录执行 —— `.env` 与 `docker-compose.yml` 都在那里，
#: 用户从别的目录调用也不会读到错的 .env
compose() { (cd "$SCRIPT_DIR" && "${DC[@]}" "$@"); }

# ---------------------------------------------------------------------------
# .env 读写（铁律 1：只新增，绝不覆盖已有的键）
# ---------------------------------------------------------------------------
_hex() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

env_get() {
    [ -f "$ENV_FILE" ] || return 1
    sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

#: 已存在则替换该行，不存在则追加 —— 不会产生重复键
env_set() {
    local key="$1" value="$2"
    if [ -f "$ENV_FILE" ] && grep -q "^${key}=" "$ENV_FILE"; then
        local tmp
        tmp="$(mktemp)"
        sed "s|^${key}=.*|${key}=${value}|" "$ENV_FILE" >"$tmp"
        cat "$tmp" >"$ENV_FILE"
        rm -f "$tmp"
    else
        printf '%s=%s\n' "$key" "$value" >>"$ENV_FILE"
    fi
}

ensure_env() {
    if [ -f "$ENV_FILE" ]; then
        log "沿用已存在的 .env（不会覆盖你的会话密钥）"
        return 0
    fi
    log "生成 .env"
    umask 077
    cat >"$ENV_FILE" <<EOF
# StarWatt 星瓦 —— Docker 启动配置
# ============================================================
# 这里只放「起容器需要」的东西；其余 94 项配置全部在网页
# 「管理 → 配置中心」里改，改完不用重启。
#
# ⚠️ FLASK_SECRET_KEY 同时用于加密配置中心里的 9 项凭据。
#    它丢了 = 那些凭据永久解不开。备份请用：
#        ./starwatt-docker.sh backup
# ============================================================

# 会话签名 + 配置加密密钥（随机生成；想换必须重填所有凭据）
FLASK_SECRET_KEY=$(_hex 32)

# 监听端口（只绑 BIND_ADDR，默认本机回环）
FLASK_PORT=${FLASK_PORT:-$DEFAULT_PORT}
BIND_ADDR=${BIND_ADDR:-127.0.0.1}

# 镜像版本：升级只改这一行，或直接跑 ./starwatt-docker.sh upgrade
STARWATT_TAG=${STARWATT_TAG:-$DEFAULT_TAG}
EOF
    chmod 600 "$ENV_FILE"
}

# ---------------------------------------------------------------------------
# 健康检查
# ---------------------------------------------------------------------------
host_port() { env_get FLASK_PORT || echo "$DEFAULT_PORT"; }
host_addr() { env_get BIND_ADDR || echo "127.0.0.1"; }

wait_healthy() {
    local port addr attempt
    port="$(host_port)"
    addr="$(host_addr)"
    # BIND_ADDR=0.0.0.0 时不能直接 curl 0.0.0.0（部分环境会失败），换回环
    [ "$addr" = "0.0.0.0" ] && addr="127.0.0.1"

    log "等待服务就绪（最多 60 秒）…"
    for attempt in $(seq 1 30); do
        if curl -fsS "http://${addr}:${port}/healthz" >/dev/null 2>&1; then
            log "服务已就绪：http://${addr}:${port}/"
            return 0
        fi
        sleep 2
    done

    warn "60 秒内没探活成功。看日志：./starwatt-docker.sh logs"
    compose ps || true
    return 1
}

show_version() {
    local port addr
    port="$(host_port)"
    addr="$(host_addr)"
    [ "$addr" = "0.0.0.0" ] && addr="127.0.0.1"
    curl -fsS "http://${addr}:${port}/healthz" 2>/dev/null || echo "(拿不到 /healthz)"
    echo
}

print_password() {
    local password
    # 首选数据卷里的文件（entrypoint 写的，可重复读）
    password="$(compose exec -T "$SERVICE" cat "/data/$PASSWORD_FILE" 2>/dev/null | tr -d '\r\n' || true)"
    if [ -n "$password" ]; then
        echo "  管理员账号  ：admin"
        echo "  初始密码    ：$password"
        return 0
    fi
    # 退路：从日志里捞（首启那次打印过）
    password="$(compose logs "$SERVICE" 2>/dev/null | sed -n 's/.*初始密码 *：//p' | tail -n 1 | tr -d '\r' || true)"
    if [ -n "$password" ]; then
        echo "  管理员账号  ：admin"
        echo "  初始密码    ：$password"
        echo
        echo "  （该值来自日志；如果已经登录过一次，它就不再有效了）"
        return 0
    fi
    return 1
}

# ---------------------------------------------------------------------------
# 命令：install
# ---------------------------------------------------------------------------
cmd_install() {
    detect_docker
    ensure_env
    log "拉取镜像（拉不到就本地构建 —— compose 里同时写了 image 与 build）"
    compose pull || warn "拉取失败，改为本地构建"
    compose up -d
    wait_healthy || true

    echo
    echo "============================================================"
    echo " StarWatt 星瓦 安装完成"
    echo "------------------------------------------------------------"
    echo " 网页        ：http://$(host_addr):$(host_port)/"
    echo " 配置文件    ：$ENV_FILE"
    echo
    if print_password; then
        echo
        echo " ⚠️ 这串密码**首次登录后会被强制修改**，改完密码文件即被删除。"
        echo "    忘了就再跑一次：./starwatt-docker.sh password"
    else
        echo " 初始密码    ：没能取到，请看日志：./starwatt-docker.sh logs"
    fi
    echo
    echo " 下一步      ：浏览器打开 → 登录 → 强制改密 → 配置中心填 openid"
    echo " 备份        ：./starwatt-docker.sh backup"
    echo "============================================================"
    echo
}

# ---------------------------------------------------------------------------
# 命令：upgrade
# ---------------------------------------------------------------------------
cmd_upgrade() {
    detect_docker
    local target="${1:-latest}"
    [ -f "$ENV_FILE" ] || die "还没装过（没有 .env）。先跑 ./starwatt-docker.sh install"

    log "升级目标：$target"
    # 先备份 —— 升级失败时这是唯一的退路（铁律 2：WAL 感知）
    if compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE"; then
        log "升级前自动备份"
        cmd_backup "" || warn "备份失败 —— 继续升级，但请自行确认已有可用备份"
    else
        warn "服务当前没在运行，跳过自动备份"
    fi

    env_set STARWATT_TAG "$target"
    log "已把 STARWATT_TAG=$target 写进 .env"
    compose pull
    compose up -d
    wait_healthy || true
    log "升级后版本："
    show_version
    echo " 回滚：./starwatt-docker.sh upgrade <旧 tag>（数据不用动）"
}

# ---------------------------------------------------------------------------
# 命令：status
# ---------------------------------------------------------------------------
cmd_status() {
    detect_docker
    compose ps
    echo
    log "版本："
    show_version
    log "数据卷（宿主机上的真实路径 —— 手工备份要带上它）"
    local vol found=0
    while read -r vol; do
        [ -n "$vol" ] || continue
        docker volume inspect "$vol" --format '  {{.Name}} → {{.Mountpoint}}'
        found=1
    done <<EOF
$(docker volume ls --filter "name=starwatt" --format '{{.Name}}' 2>/dev/null || true)
EOF
    [ "$found" = "1" ] || echo "  （没找到卷 —— 服务还没起过？）"
}

# ---------------------------------------------------------------------------
# 命令：logs / password / restart / down
# ---------------------------------------------------------------------------
cmd_logs() {
    detect_docker
    if [ "${1:-}" = "-f" ] || [ "${1:-}" = "--follow" ]; then
        compose logs -f --tail=200 "$SERVICE"
    else
        compose logs --tail=200 "$SERVICE"
    fi
}

cmd_password() {
    detect_docker
    echo "============================================================"
    if print_password; then
        echo
        echo " ⚠️ 登录后会被强制改密，改完这串就失效了。"
    else
        echo " 没找到一次性密码。可能的原因："
        echo "   · 已经登录过并改过密码（改完密码文件会被删除）"
        echo "   · 安装时用 BOOTSTRAP_ADMIN_PASSWORD 自己指定了密码"
        echo "   · 容器日志已经轮转掉了"
        echo
        echo " 忘记密码请用官方方式新建一个管理员（没有、也不会有网页重置入口）："
        echo "   docker compose exec $SERVICE python - <<'PY'"
        echo "   from starwatt.db.connection import init"
        echo "   from starwatt.auth import service as auth"
        echo "   init()"
        echo "   auth.create_user('admin2', '换成你的强密码Aa1!', role='admin')"
        echo "   PY"
    fi
    echo "============================================================"
}

cmd_restart() {
    detect_docker
    compose restart "$SERVICE"
    wait_healthy || true
}

cmd_down() {
    detect_docker
    compose down
    log "已停止。**数据卷没有删除**（重新 install/upgrade 即可回来）"
}

# ---------------------------------------------------------------------------
# 命令：backup（铁律 2：绝不用 cp 备份 WAL 库）
# ---------------------------------------------------------------------------
cmd_backup() {
    detect_docker
    local out="${1:-}"
    [ -n "$out" ] || out="$SCRIPT_DIR/starwatt-backup-$(date +%Y%m%d-%H%M%S).tar.gz"

    compose ps --status running --services 2>/dev/null | grep -qx "$SERVICE" ||
        die "容器没在运行 —— 在线备份需要它跑着。先 ./starwatt-docker.sh install（或 restart）"

    local stage
    stage="$(mktemp -d)"

    log "在容器内做 WAL 感知的在线备份（Python sqlite3 的 backup API）"
    # ⚠️ 不用 cp：库是 WAL 模式，旧服务在写时 cp 出来的副本可能少最后几个事务。
    # ⚠️ 也不用宿主机的 sqlite3 命令行：镜像里没有它，而宿主有没有不一定 ——
    #    容器里的 Python 一定有 stdlib 的 sqlite3，所以整件事都在容器内做完。
    # 最后一行回显两侧行数：**备份没验过等于没备份**。
    local verify
    verify="$(compose exec -T "$SERVICE" python - <<'PY' | tr -d '\r'
import sqlite3


def rows(path):
    return sqlite3.connect(path).execute("select count(*) from records").fetchone()[0]


src = sqlite3.connect("/data/records.db")
dst = sqlite3.connect("/tmp/starwatt-backup.db")
with dst:
    src.backup(dst)
dst.close()
src.close()
print(f"rows src={rows('/data/records.db')} backup={rows('/tmp/starwatt-backup.db')}")
PY
)"

    compose cp "$SERVICE:/tmp/starwatt-backup.db" "$stage/records.db"
    compose exec -T "$SERVICE" cat /data/.flask_secret_key >"$stage/flask_secret_key" 2>/dev/null ||
        warn "没取到 .flask_secret_key（用 FLASK_SECRET_KEY 环境变量时正常）"
    [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$stage/env.backup"

    tar czf "$out" -C "$stage" .
    rm -rf "$stage"

    log "备份完成：$out"
    echo "  $verify"
    case "$verify" in
        *"src="*"backup="*)
            if [ "${verify##*src=}" = "${verify##*backup=}" ]; then
                echo "  ✅ 源库与备份行数一致"
            else
                warn "⚠️ 行数不一致，请重做备份并核对"
            fi
            ;;
    esac
    echo
    echo " 恢复：./starwatt-docker.sh restore $(basename "$out")"
}

# ---------------------------------------------------------------------------
# 命令：restore（铁律 3：二次确认 + 旧库先挪成 .bak）
# ---------------------------------------------------------------------------
cmd_restore() {
    detect_docker
    local archive="${1:-}"
    [ -n "$archive" ] || die "用法：./starwatt-docker.sh restore <备份文件.tar.gz>"
    [ -f "$archive" ] || die "找不到文件：$archive"

    warn "恢复会用备份覆盖当前数据库（当前库会先被挪成 records.db.bak）。"
    printf '确认继续？输入 yes：'
    read -r answer
    [ "$answer" = "yes" ] || die "已取消"

    local stage
    stage="$(mktemp -d)"
    tar xzf "$archive" -C "$stage"
    [ -f "$stage/records.db" ] || die "备份里没有 records.db，不是本脚本产出的备份？"

    log "停止服务"
    compose stop "$SERVICE" >/dev/null

    log "写回数据卷（用 root 一次性容器，因为容器内应用用户不能改卷属主）"
    compose run --rm --no-deps --user root --entrypoint sh \
        -v "$stage:/restore:ro" "$SERVICE" -c "
            set -e
            if [ -f /data/records.db ]; then mv /data/records.db /data/records.db.bak; fi
            cp /restore/records.db /data/records.db
            chown $CONTAINER_UID:$CONTAINER_UID /data/records.db
            if [ -f /restore/flask_secret_key ]; then
                cp /restore/flask_secret_key /data/.flask_secret_key
                chown $CONTAINER_UID:$CONTAINER_UID /data/.flask_secret_key
                chmod 600 /data/.flask_secret_key
            fi
            echo restored
        "
    rm -rf "$stage"

    log "重新启动"
    compose up -d
    wait_healthy || true
    echo " 旧库保留在数据卷里的 records.db.bak（确认无误后可自行删除）"
}

# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
main() {
    local command="${1:-help}"
    shift || true
    case "$command" in
        install) cmd_install "$@" ;;
        upgrade) cmd_upgrade "$@" ;;
        status) cmd_status "$@" ;;
        logs) cmd_logs "$@" ;;
        password) cmd_password "$@" ;;
        backup) cmd_backup "$@" ;;
        restore) cmd_restore "$@" ;;
        restart) cmd_restart "$@" ;;
        down) cmd_down "$@" ;;
        help | -h | --help) usage ;;
        *) die "未知命令：$command（用 ./starwatt-docker.sh help 看用法）" ;;
    esac
}

main "$@"
