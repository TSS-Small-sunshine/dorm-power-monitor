"""进程内调度（Q14）+ ``post_fork`` 启动时机（审计 B3）。

为什么不再用 host cron
======================

旧方案是**双 cron + flock**（``*/10`` 抓取 + ``* * * * *`` tick）。换成容器分发后，
宿主 crontab 既别扭又难移植，所以 Q14 决定改成**进程内调度**：

::

    gunicorn(1 worker) + Flask + APScheduler     ← 一个容器搞定，用户零配置

B3 三道防线（缺一不可）
=======================

=========  ==========================================================
防线        位置
=========  ==========================================================
防线 1      ``gunicorn.conf.py``：``workers=1`` + ``preload_app=False``
防线 2      ``gunicorn.conf.py`` 的 ``post_fork`` 里启动（本模块被它调用）
防线 3      ``web.py`` 的 master PID 守卫 + 幂等守卫（误配 → 启动即崩）
=========  ==========================================================

三种失败场景与对策
==================

================================================  ==============================
场景                                               后果（没有防线时）
================================================  ==============================
模块导入即 ``start()`` + ``--preload``             master 启动 → fork 后线程丢失
                                                   → **调度器完全不工作**
多 worker                                          每个 worker 各启动一次
                                                   → **重复抓取 N 倍**
``app.run(debug=True)`` 的 reloader                父子进程各启动一次 → 重复抓取
================================================  ==============================

📌 关于 ``misfire_grace_time``：它只覆盖「上一次还在跑、本次已到点」的合并，
**不补跑停机期间错过的任务**。容器停 3 小时再启动，不会立刻补 18 次抓取
—— 这正是我们想要的（避免重启瞬间打爆学校接口）。

📌 为什么用 :class:`~apscheduler.triggers.interval.IntervalTrigger` 而不是
文档示例里的 ``CronTrigger(minute="*/10")``：``scrape_interval_sec`` 是
**WebUI 可改的配置项**（Q15），写死 cron 表达式会让那个配置项变成摆设。
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from starwatt.config_registry import get_int
from starwatt.timeutil import CST

logger = logging.getLogger("starwatt.scheduler")

__all__ = [
    "DEFAULT_INTERVAL_SEC",
    "JOB_L3_GATE",
    "JOB_SCRAPE",
    "MISFIRE_GRACE_SEC",
    "build_scheduler",
    "l3_gate_job",
    "scrape_job",
    "shutdown_scheduler",
]

#: 抓取任务 id（日志与测试引用）
JOB_SCRAPE = "scrape"

#: L3 报表门控任务 id（每分钟检查日报 / 周报 / 月报是否到点）
JOB_L3_GATE = "l3_gate"

#: ``scrape_interval_sec`` 读不到时的兜底（注册表默认值也是 600）
DEFAULT_INTERVAL_SEC = 600

#: 允许的「迟到」秒数 —— 只用于合并重入，不用于补跑停机期
MISFIRE_GRACE_SEC = 300

#: 调度器允许的最小间隔（秒）—— 防止配置写错把学校接口打爆
MIN_INTERVAL_SEC = 60


def scrape_job() -> None:
    """定时抓取任务。

    **线程级兜底**（RK4）：APScheduler 的 job 抛出的异常只会进它自己的日志，
    若再叠加一个未捕获异常，调度器线程可能被拖垮。这里把整轮抓取包起来，
    失败只记日志、绝不外抛。

    两类失败分开记：

    * :class:`ScrapeError` —— 「无法开始」（未配置 openid / 房间号解析失败），
      是**配置问题**：记 ``WARNING``，不要用 traceback 吓人
    * 其它异常 —— 未预期的 bug：记 ``ERROR`` + traceback
    """
    from starwatt.scraper.client import ScrapeError
    from starwatt.scraper.service import run_once  # 延迟导入：避免包导入期副作用

    try:
        report = run_once()
    except ScrapeError as exc:
        logger.warning("定时抓取无法开始：%s", exc)
        return
    except Exception:  # noqa: BLE001 —— 见上：调度线程必须活着
        logger.exception("定时抓取崩溃（已兜住，调度器继续运行）")
        return

    if report.ok:
        logger.info("定时抓取完成：%s", report.summary())
    else:
        logger.warning("定时抓取未完成：%s", report.summary())


def _interval_seconds() -> int:
    """抓取间隔（秒）—— 读注册表配置，**读不到就回落默认值**。

    为什么必须容错：``build_scheduler()`` 可能在 ``meta`` 表还没建好时被调用
    （首次启动、OOBE 尚未完成）。此时若让 ``sqlite3.OperationalError``
    冒出去，整个 worker 会起不来 —— 而「调度器间隔用了默认值」只是个
    小得多的损失（用户改完配置重启即生效）。
    """
    try:
        value = get_int("scrape_interval_sec", DEFAULT_INTERVAL_SEC)
    except Exception:  # noqa: BLE001 —— 见上：不能因为读不到配置就起不来
        logger.warning(
            "读取 scrape_interval_sec 失败，回落默认 %ds", DEFAULT_INTERVAL_SEC
        )
        return DEFAULT_INTERVAL_SEC
    return max(MIN_INTERVAL_SEC, value)


def l3_gate_job() -> None:
    """每分钟检查一次 L3 报表（日报 / 周报 / 月报）是否到点。

    与旧方案的 ``* * * * *`` cron tick 等价 —— 但**判定逻辑在
    :func:`starwatt.notify.policies.l3_due`**：它比对配置的 ``HH:MM`` 与
    当天/当周/当月的幂等键，所以「每分钟都跑」不会重复推送。

    线程级兜底同 :func:`scrape_job`：通知层不可用或某个报表失败，
    都不能把调度线程弄死。
    """
    try:
        from starwatt.notify import policies
    except Exception:  # noqa: BLE001 —— 通知层依赖缺失时静默降级
        logger.debug("通知层不可用，跳过 L3 报表门控")
        return

    for kind in ("daily", "weekly", "monthly"):
        try:
            if policies.push_l3_if_due(kind):
                logger.info("L3 报表已推送：%s", kind)
        except Exception:  # noqa: BLE001 —— 单个报表失败不影响其它两个
            logger.exception("L3 报表 %s 推送失败（已兜住）", kind)


def build_scheduler() -> BackgroundScheduler:
    """构造调度器（**不启动**）。

    启动由 ``web.py:start_scheduler_once()`` 负责 —— 必须在 worker 进程内
    （B3 防线 2/3），不能在模块导入时启动。
    """
    interval = _interval_seconds()
    scheduler = BackgroundScheduler(
        timezone=CST,  # Q10：与 timeutil 同一时区，不受服务器 TZ 影响
        job_defaults={
            "max_instances": 1,  # 防重入（替代旧 flock）
            "coalesce": True,  # 积压合并成一次
            "misfire_grace_time": MISFIRE_GRACE_SEC,
        },
    )
    scheduler.add_job(
        scrape_job,
        IntervalTrigger(seconds=interval, timezone=CST),
        id=JOB_SCRAPE,
        name="学校接口抓取",
        replace_existing=True,
    )
    # L3 报表门控：每分钟一次（判定在 policies.l3_due，不会重复推）
    scheduler.add_job(
        l3_gate_job,
        CronTrigger(minute="*", timezone=CST),
        id=JOB_L3_GATE,
        name="L3 报表门控",
        replace_existing=True,
    )
    logger.info("调度器已装配：job=%s 间隔=%ds，job=%s 每分钟", JOB_SCRAPE, interval, JOB_L3_GATE)
    return scheduler


def shutdown_scheduler(scheduler: BackgroundScheduler | None) -> None:
    """优雅停止（SIGINT / SIGTERM）；``None`` 或未启动时是 no-op。"""
    if scheduler is None:
        return
    try:
        if scheduler.running:
            scheduler.shutdown(wait=True)
            logger.info("调度器已停止")
    except Exception:  # noqa: BLE001 —— 关闭失败不该阻止进程退出
        logger.warning("调度器停止时出错（忽略）", exc_info=True)
