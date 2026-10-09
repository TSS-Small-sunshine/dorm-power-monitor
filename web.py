"""web.py —— gunicorn 入口（``web:app``）。

职责只有三件（B2 / B3）
=======================

1. 装配日志（Q20）与应用对象（``create_app``）
2. 暴露 ``app`` 给 gunicorn：``gunicorn -c gunicorn.conf.py web:app``
3. 提供 :func:`start_scheduler_once` / :func:`stop_scheduler` 给
   ``gunicorn.conf.py`` 的 ``post_fork`` / ``worker_int`` 钩子调用

**这里不调用 ``app.run()``** —— 开发与生产都用 gunicorn，从根上消除
「Flask dev server 的 reloader 让调度器启动两次」这个风险（B2 §2.4）。

启动守卫（B3 防线 3）
=====================

⚠️ **不能用「模块导入时的 PID」当 master**。gunicorn 26 的真实顺序是
（`arbiter.py` → `workers/base.py`）：

::

    ① master fork 出子进程
    ② 子进程调用 post_fork()      ← 我们的钩子在这里 import web
    ③ 子进程调用 load_wsgi()      ← 应用模块此时才被导入

也就是说 ``preload_app=False`` 时 ``web`` 是在 **worker** 里被导入的，
「导入时 PID」就等于 worker 自己的 PID —— 用它做守卫会**每次启动都误判**。

正确做法：gunicorn 的 ``post_fork(server, worker)`` 会把 **arbiter** 传进来，
``server.pid`` 才是 master 的 PID。钩子把它显式传给
:func:`start_scheduler_once`，于是「在 master 里启动」变成一个可判定的事实：

::

    worker 进程 → os.getpid() != server.pid → 正常启动
    master 进程 → os.getpid() == server.pid → RuntimeError（启动即失败）

幂等守卫是第二层：``post_fork`` 理论上每个 worker 只调一次，但
``--reload`` / 反复调用时必须有副作用为零的保证。
"""
from __future__ import annotations

import logging
import os

from starwatt.logging_setup import get_logger, setup_logging
from starwatt.web.factory import create_app, register_startup_tasks

setup_logging()
logger: logging.Logger = get_logger("web")

#: gunicorn 要找的 WSGI 应用对象（``web:app``）
app = create_app()

#: 调度器单例（模块级，进程内唯一）
_scheduler = None

#: 启动维护结果（幂等守卫；``None`` = 还没跑过）
_startup_report: dict | None = None


def startup_once() -> dict:
    """**首次启动维护**（B4）—— 幂等，可在任意进程安全重复调用。

    1. 建表（幂等）
    2. 删掉已下线的旧键（``admin_password``，L20）
    3. 未写入的配置项按默认值落库
    4. **bootstrap 建号**：``users`` 为空且 ``BOOTSTRAP_ADMIN_PASSWORD`` 存在时
       建管理员并强制首登改密，随后把该行从 ``.env`` 抹掉（用后即焚）
    5. 把 secret 注册给日志脱敏（Q20）

    Returns:
        维护报告（``{"purged", "defaults", "bootstrap"}``）。
    """
    global _startup_report
    if _startup_report is not None:
        return _startup_report

    _startup_report = register_startup_tasks(app)
    return _startup_report


def start_scheduler_once(*, master_pid: int | None = None) -> None:
    """在 **worker 进程内**幂等启动调度器（由 gunicorn ``post_fork`` 调用）。

    Args:
        master_pid: gunicorn **arbiter（master）** 的 PID —— 由
            ``post_fork(server, worker)`` 的 ``server.pid`` 传入。
            传了它才能判定「我是不是 master」；见模块 docstring 的说明
            （**不能**用导入时 PID 代替）。

    Raises:
        RuntimeError: 在 master 进程里被调用（``master_pid`` 与本进程 PID 相同）。
            这是**故意的**：宁可启动失败，也不要「看着在跑、其实没抓」。
    """
    global _scheduler
    if _scheduler is not None:
        return  # 幂等：重复调用无副作用

    if master_pid is not None and os.getpid() == master_pid:
        raise RuntimeError(
            f"调度器不得在 master 进程（pid={master_pid}）启动："
            "请确认 gunicorn 的 preload_app=False，并且不要使用 app.run()。"
        )
    if master_pid is None:
        # 没有 arbiter 信息（手工调用 / 测试 / CLI）→ 放行但留痕
        logger.log(25, "start_scheduler_once 未收到 master_pid，跳过 master 判定")  # NOTICE

    # 进程启动时确保表结构存在（幂等）—— 调度器要读 meta 里的配置
    # （scrape_interval_sec）与状态键；M4 的 app 启动也会调用它。
    from starwatt.db.connection import init as db_init

    db_init()

    # B4：首次启动维护 + bootstrap 建号（幂等；在 worker 进程里跑）
    startup_once()

    from starwatt.scheduler import build_scheduler  # 延迟导入：CLI 路径不需要 APScheduler

    scheduler = build_scheduler()
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "调度器已启动（pid=%s，jobs=%s）",
        os.getpid(),
        [job.id for job in scheduler.get_jobs()],
    )


def stop_scheduler() -> None:
    """优雅停止调度器（SIGINT / SIGTERM / worker 退出）。"""
    global _scheduler
    if _scheduler is None:
        return

    from starwatt.scheduler import shutdown_scheduler

    shutdown_scheduler(_scheduler)
    _scheduler = None


def scheduler_running() -> bool:
    """调度器是否已在**本进程**内启动（``/healthz`` 与测试用）。"""
    return _scheduler is not None and bool(getattr(_scheduler, "running", False))
