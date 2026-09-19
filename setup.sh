#!/usr/bin/env bash
# =============================================================================
# 宿舍电量监控 一键安装脚本 (Ubuntu 24.04+ / Debian 12+)
# =============================================================================
# 用法：
#   chmod +x setup.sh
#   ./setup.sh
#
# 该脚本会：
#   1. 检查 Python 3.10+
#   2. 创建 .venv 虚拟环境
#   3. 安装 requirements.txt 里的依赖
#   4. 复制 .env.example → .env（如不存在）
#   5. 初始化 SQLite 数据库
#
# 之后请手动：
#   - 编辑 .env 填入 DORM_OPENID / FEISHU_WEBHOOK 等凭据
#   - 启动 Web：./.venv/bin/python web.py
#   - （可选）配置 systemd + nginx + cron（看 README）
# =============================================================================
set -euo pipefail

echo "🚀 宿舍电量监控 一键安装"
echo "=========================="
echo ""

# ---- 1. Python 检查 --------------------------------------------------------
if ! command -v python3 >/dev/null 2>&1; then
    echo "❌ 未找到 python3。请先安装："
    echo "   sudo apt update && sudo apt install -y python3 python3-venv python3-pip"
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
echo "✅ 检测到 Python ${PY_VERSION}"

NEED_MAJOR=3
NEED_MINOR=10
if [ "${PY_VERSION%.*}" -lt "${NEED_MAJOR}" ] || \
   { [ "${PY_VERSION%.*}" -eq "${NEED_MAJOR}" ] && [ "${PY_VERSION#*.}" -lt "${NEED_MINOR}" ]; }; then
    echo "❌ 需要 Python ${NEED_MAJOR}.${NEED_MINOR}+，当前是 ${PY_VERSION}"
    exit 1
fi

# ---- 2. venv ---------------------------------------------------------------
if [ ! -d ".venv" ]; then
    echo ""
    echo "📦 创建 Python 虚拟环境 .venv ..."
    python3 -m venv .venv
else
    echo ""
    echo "📦 .venv 已存在，跳过创建"
fi

# ---- 3. 依赖 ---------------------------------------------------------------
echo ""
echo "📦 升级 pip 并安装依赖 ..."
./.venv/bin/pip install --upgrade pip --quiet
./.venv/bin/pip install -r requirements.txt --quiet
echo "✅ 依赖安装完成"

# ---- 4. .env ---------------------------------------------------------------
echo ""
if [ ! -f ".env" ]; then
    echo "⚙️  复制 .env.example → .env（请编辑后填入真实凭据）"
    cp .env.example .env
    chmod 600 .env
    echo "   已创建 .env，权限 600（仅当前用户可读）"
else
    echo "⚙️  .env 已存在，跳过复制（请确保已填好 DORM_OPENID 等）"
fi

# ---- 5. DB -----------------------------------------------------------------
echo ""
echo "🗄️  初始化 SQLite 数据库 ..."
./.venv/bin/python -c "from db import init; init()"
echo "✅ 数据库初始化完成"

# ---- 收尾 -----------------------------------------------------------------
echo ""
echo "=========================="
echo "✅ 安装完成！"
echo ""
echo "下一步："
echo "  1. 编辑 .env 填入配置："
echo "       nano .env"
echo "     必填项：DORM_OPENID、FEISHU_WEBHOOK"
echo ""
echo "  2. 测试运行（前台）："
echo "       ./.venv/bin/python web.py"
echo "     浏览器访问 http://localhost:5000/"
echo ""
echo "  3. 测试抓取（手动一次）："
echo "       ./.venv/bin/python dorm_power.py"
echo ""
echo "  4. （可选）生产部署 — 看 README："
echo "       - systemd:   deploy/dorm-web.service"
echo "       - nginx:     nginx/dorm.conf"
echo "       - cron:      deploy/dorm-cron.txt"
echo "       - Feishu机器人：README '🤖 Feishu 机器人部署指南'"
echo ""
echo "📚 文档：README.md"
echo "🐛 问题反馈：项目 Issue 页面"
echo ""
