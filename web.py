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

``_MASTER_PID`` 是**模块导入时**的 PID。gunicorn 的 ``post_fork`` 在
**worker 进程**里执行，此时 PID 已经变了；如果没变，说明模块是在 master
里导入的（``preload_app=True`` 或有人用 ``app.run()``），此时启动调度器会
「fork 后线程丢失 → 调度器静默不工作」。守卫把这种误配变成**启动即失败**，
而不是静默出错。

::

    preload_app=False → 导入发生在 worker → PID 必然不同 → 正常启动
    preload_app=True  → 导入发生在 master → PID 相同    → RuntimeError

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

#: 模块导入时的 PID —— 见模块 docstring 的「启动守卫」
_MASTER_PID = os.getpid()

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


def start_scheduler_once() -> None:
    """在 **worker 进程内**幂等启动调度器（由 gunicorn ``post_fork`` 调用）。

    Raises:
        RuntimeError: 在 master 进程里被调用（``preload_app`` 误配成 ``True``，
            或有人改回 ``app.run()``）。这是**故意的**：宁可启动失败，
            也不要「看着在跑、其实没抓」。
    """
    global _scheduler
    if _scheduler is not None:
        return  # 幂等：重复调用无副作用

    if os.getpid() == _MASTER_PID:
        raise RuntimeError(
            "调度器不得在 master 进程启动：请保持 gunicorn 的 preload_app=False，"
            "并且不要使用 app.run()。"
        )

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
