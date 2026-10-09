# syntax=docker/dockerfile:1.7
# =============================================================================
# StarWatt 星瓦 —— 多阶段镜像（Q3 / Q4 / B1）
# =============================================================================
# 三条设计约束（都来自需求文档，不是随手加的）
#
# 1. **全架构可用**：amd64 / arm64 / **armv7**（Q3）。
#    依赖里只剩 2 个普通 C 扩展（pycryptodome / pillow），在目标架构上
#    用 gcc 编译即可 —— 没有任何 Rust 依赖（B1：pydantic / bcrypt 已剔除）。
# 2. **镜像里预编译**：用户 `docker pull` 即用，不需要在树莓派上装工具链（Q4）。
# 3. **前端在镜像内构建**：`static/` 产物不进仓库（见 .gitignore），
#    所以镜像必须自己跑一次 `npm run build` —— 否则 `docker run` 出来是空壳。
#
# 阶段划分
# ========
#   frontend → 出 static/（index.html + assets/*）
#   builder  → 在目标架构上编译依赖，装进 /install
#   runtime  → 只带运行时库 + 依赖 + 代码 + 前端产物；非 root 运行
# =============================================================================

# -----------------------------------------------------------------------------
# 阶段 1：前端构建
# -----------------------------------------------------------------------------
FROM node:22-alpine AS frontend

WORKDIR /app
# 先只拷清单，让 npm ci 这一层能被缓存（改代码不必重装依赖）
COPY frontend/package.json frontend/package-lock.json ./frontend/
RUN cd frontend && npm ci --include=dev

# 再拷源码（.dockerignore 已排除 node_modules）
COPY frontend/ ./frontend/
# 产物落到 /app/static（vite.config.ts 的 outDir = ../static）
RUN cd frontend && npm run build


# -----------------------------------------------------------------------------
# 阶段 2：Python 依赖（C 扩展在目标架构上编译）
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_ROOT_USER_ACTION=ignore

# pycryptodome 需要 gcc；pillow 需要 libjpeg / zlib 的头文件（Q4 的依赖表）
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libc6-dev \
        libjpeg-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
# --prefix 装到独立目录，运行阶段整包拷走，编译工具不进最终镜像
RUN pip install --prefix=/install -r requirements.txt


# -----------------------------------------------------------------------------
# 阶段 3：运行时
# -----------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# PYTHONUNBUFFERED：容器日志要立刻可见（否则 journalctl / docker logs 会吞）
# GUNICORN_BIND：⚠️ 必须 0.0.0.0 —— gunicorn.conf.py 默认绑 127.0.0.1，
#                在容器里那样等于外部完全访问不到
# DORM_DATA_DIR / DB_PATH：数据统一放 /data，便于卷挂载
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUNICORN_BIND=0.0.0.0:5000 \
    FLASK_PORT=5000 \
    DORM_DATA_DIR=/data \
    DB_PATH=/data/records.db

# 运行时只需要**动态库**（不要 -dev）：pillow 用 libjpeg/zlib，探活用 curl
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
        curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local

WORKDIR /app
COPY . .
COPY --from=frontend /app/static ./static

# 非 root 运行（Q17 容器加固）；/data 是卷，属主必须对
RUN useradd --system --create-home --uid 10001 --shell /usr/sbin/nologin starwatt \
    && mkdir -p /data \
    && chown -R starwatt:starwatt /data /app \
    && chmod +x /app/docker-entrypoint.sh

USER starwatt

VOLUME ["/data"]
EXPOSE 5000

# 探活端点不碰数据库、不碰网络（见 starwatt/web/blueprints/health.py）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${FLASK_PORT}/healthz" || exit 1

# entrypoint 负责「首启生成随机密码 + 持久化会话密钥」，然后 exec CMD
ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "gunicorn.conf.py", "web:app"]
