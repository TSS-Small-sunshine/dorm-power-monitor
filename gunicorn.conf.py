"""gunicorn 配置（B2）—— 生产与裸机共用，开发也用同一份。

用法
====

生产 / 裸机::

    gunicorn -c gunicorn.conf.py web:app

开发（热重载）::

    gunicorn -c gunicorn.conf.py --reload web:app

**不要再使用 ``python web.py``** —— ``app.run()`` 已被移除（B2 §2.4），
dev/prod 差异因此彻底消失。

两条红线（B3 防线 1）
====================

``workers = 1``
    调度器是**进程内单例**。多 worker 会让每个 worker 各抓一次
    → 学校接口被重复请求 N 倍，DB 也会互相抢写。

``preload_app = False``
    开了 preload，模块（含 ``web.py``）会在 **master** 里导入；fork 之后
    调度器的后台线程**不会**跟着复制到 worker → 抓取静默停摆。
    配合 ``web.py`` 的 master PID 守卫，误配会变成「启动即崩」。

钩子
====

==================  ============================================
``post_fork``       在 worker 内启动调度器（防线 2，只启动一次）
``worker_int``      SIGINT → 优雅停止调度器（Q17 稳定性）
``worker_abort``    超时被强杀 → 同样尽力优雅停止
==================  ============================================
"""
import os

#: 监听地址：容器里通常需要 0.0.0.0（由反代 / 端口映射暴露）
bind = os.environ.get("GUNICORN_BIND") or "127.0.0.1:{}".format(
    os.environ.get("FLASK_PORT", "5000")
)

# 🔴 红线 1：APScheduler 必须单 worker
workers = 1

# 🔴 红线 2：必须 False（否则调度器在 master 启动后丢失）
preload_app = False

worker_class = "sync"
timeout = 120  # 抓取最慢约 30s，留足余量
graceful_timeout = 30
keepalive = 5

# 日志交给 starwatt.logging_setup 统一处理（Q20：级别 / 类别 / 脱敏）
accesslog = "-"
errorlog = "-"
loglevel = "info"


def post_fork(server, worker):
    """防线 2：在 worker 进程内启动调度器（见 starwatt/scheduler.py）。"""
    from web import start_scheduler_once

    start_scheduler_once()


def worker_int(worker):
    """SIGINT —— 优雅停止调度器。"""
    from web import stop_scheduler

    stop_scheduler()


def worker_abort(worker):
    """worker 超时被强杀 —— 同样尽力优雅停止。"""
    from web import stop_scheduler

    stop_scheduler()
