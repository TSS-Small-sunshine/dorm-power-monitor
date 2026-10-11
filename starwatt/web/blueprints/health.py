"""``/healthz`` —— 探活端点（容器 HEALTHCHECK 与 systemd 用）。

铁律：**探活端点自己不能变成故障源**
====================================

* 不碰数据库（``meta`` 表还没建时也要能回 200）
* 不碰网络（不查学校接口、不推飞书）
* 不要求认证（Docker / systemd / nginx 都要能匿名探）

所以它只回答「进程活着吗 + 版本是什么 + 现在几点」。

:/healthz/deep —— 就绪端点
=========================

``/healthz`` 只能证明**进程活着**：即使数据库路径不可写、调度器没起来、
半年没抓到数据，它也照样回 200（审计 F-15）。所以另开一个深一点的就绪
端点给「真人排障 / 外部监控」用 —— 路径取 ``/healthz/deep`` 而不是
``/readyz``，理由是它自然落在 ``/healthz`` 这个已经进
``RESERVED_PREFIXES`` 的前缀下（不会被 SPA 兜底吞掉），且和探活端点
放在同一个蓝图、同一个文件里，语义上就是「同一个东西问得更细」。

与 ``/healthz`` 的**边界**：

* 仍然**不查网络**，仍然**不要求认证**（匿名可探）
* 只做极轻量的检查（一次连接 + 一次回滚写探针 + 两个 state 读），
  不做全表扫描 —— 它可能被外部监控按秒级频率调用
* 任一必需项失败 → **503**；全绿 → **200**。响应体始终是 JSON
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify

from starwatt import version_string
from starwatt.config_registry import get_str
from starwatt.timeutil import stamp

bp = Blueprint("health", __name__)

__all__ = ["CHECKS", "REQUIRED_CHECKS", "SCHEDULER_PROBE_KEY", "bp"]

logger = logging.getLogger("starwatt.web")

#: 版本串（与 ``starwatt.__version__`` 同源）
APP_VERSION = version_string()

#: ``app.extensions`` 里存「调度器是否在跑」探针的键。
#:
#: ``web.py``（顶层装配点，允许依赖 starwatt 包）在模块末尾写入
#: ``app.extensions[SCHEDULER_PROBE_KEY] = web.scheduler_running``；
#: 本模块只读它 —— ``starwatt/**`` 不能回引顶层 ``web`` 模块
#: （``scripts/ast_guard.py`` R4）。
SCHEDULER_PROBE_KEY = "starwatt.scheduler_running"

#: ``_check_scheduler`` 里用到的 state 键（与 registry 的 ``state(...)`` 同名）
_SCRAPE_STATE_AT = "last_scrape_at"
_SCRAPE_STATE_STATUS = "last_scrape_status"

#: 写探针用的临时表 —— 只在**事务内**存在（建完立刻写 + 读 + 回滚）
_DB_PROBE_TABLE = "_healthz_probe"


def _probe_db_writable() -> None:
    """DB 可写探测：零残留，且真的能发现「数据库文件写不进去」。

    做法与理由：

    1. ``BEGIN IMMEDIATE`` 先拿**写锁** —— 文件只读 / 目录不可写时这一步就报错
    2. ``CREATE TEMP TABLE`` + ``INSERT`` + ``SELECT`` 验证写读通路
    3. ``ROLLBACK`` 收尾 —— 连 SQLite 文件头都没动过（事务里的页改动从未
       提交），所以是零残留；不用关掉/重开 ``journal_mode`` 去造垃圾数据

    Raises:
        Exception: 任何一步失败都直接向上抛，由调用方翻译成 ``db=False``。
    """
    from starwatt.db.connection import connect

    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                f"CREATE TEMP TABLE {_DB_PROBE_TABLE} (k TEXT PRIMARY KEY)"
            )
            conn.execute(
                f"INSERT INTO {_DB_PROBE_TABLE} (k) VALUES (?)", ("probe",)
            )
            row = conn.execute(
                f"SELECT k FROM {_DB_PROBE_TABLE} WHERE k = ?", ("probe",)
            ).fetchone()
            if row is None:
                raise RuntimeError("写探针读回为空")
        finally:
            conn.execute("ROLLBACK")


def _check_db() -> dict:
    """必需项：数据库可写。"""
    try:
        _probe_db_writable()
    except Exception as exc:  # noqa: BLE001 —— 就绪检查要吞掉任何异常
        logger.warning("健康检查：数据库写探针失败（%s）", exc)
        return {"ok": False, "error": str(exc)}
    return {"ok": True}


def _check_scheduler() -> dict:
    """必需项：调度器在**本进程**内跑着。

    ⚠️ 接线方式：**不能**在这里 ``import web`` —— ``scripts/ast_guard.py`` 的
    R4 规则禁止 ``starwatt/**`` 回引顶层旧模块 ``web``（本模块属于
    starwatt 包）。所以状态由 ``web.py`` 在模块末尾把自己的
    :func:`web.scheduler_running` 注册进 ``app.extensions``，这里只读那个
    接线点 —— 依赖方向仍然是「顶层 → 包」，没有被倒过来。

    接线点缺失（app 不是由 ``web.py`` 装配的，例如直接调
    ``factory.create_app()``）**算失败**：那种实例里没有任何东西会启动
    调度器，抓取永远不会发生，报 503 才是诚实的。
    """
    from flask import current_app

    probe = current_app.extensions.get(SCHEDULER_PROBE_KEY)
    if probe is None:
        return {
            "ok": False,
            "running": False,
            "error": f"未接入调度器状态（app.extensions[{SCHEDULER_PROBE_KEY!r}] 缺失）",
        }
    try:
        running = bool(probe())
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "running": False, "error": str(exc)}
    return {"ok": running, "running": running}


def _check_scrape() -> dict:
    """非必需项：上次抓取时间与状态（state 里现成的，读两个键而已）。

    从未抓过 / 状态不是 ``ok`` 只作**通报**，不让端点变 503 —— 抓取失败是
    业务问题，不该判定「服务未就绪」。是否算「陈旧」交给调用方看时间戳。
    """
    try:
        last_at = get_str(_SCRAPE_STATE_AT, "") or None
        status = get_str(_SCRAPE_STATE_STATUS, "") or None
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    return {"ok": status == "ok", "last_scrape_at": last_at, "last_scrape_status": status}


def _check_version() -> dict:
    """必需项：只读一个模块常量，成本为零，但能证明本模块被正确加载。"""
    return {"ok": bool(APP_VERSION)}


#: 检查项注册表：``名字 -> 检查函数``。
#:
#: 测试（以及未来新增检查）按名字**整体替换**取值，不用为了触发失败去
#: 真的弄坏数据库。
CHECKS: dict[str, object] = {
    "db": _check_db,
    "scheduler": _check_scheduler,
    "version": _check_version,
    "scrape": _check_scrape,
}

#: 必需项 —— 这些不 ok 就回 503；其余只出现在响应体里
REQUIRED_CHECKS = ("db", "scheduler", "version")


@bp.get("/healthz")
def healthz():
    """无认证、无 IO 的探活响应。"""
    return jsonify(status="ok", version=APP_VERSION, time=stamp())


@bp.get("/healthz/deep")
def healthz_deep():
    """就绪端点：数据库可写 + 调度器在跑 + 上次抓取情况。

    全绿 → 200；必需项有失败 → 503（响应体里逐项列出，便于直接定位）。
    """
    results: dict[str, object] = {}
    failures: list[str] = []
    for name, check in CHECKS.items():
        try:
            results[name] = check()  # type: ignore[operator]
        except Exception as exc:  # noqa: BLE001 —— 检查函数自己崩了也算失败
            results[name] = {"ok": False, "error": str(exc)}
        if name in REQUIRED_CHECKS and not results[name].get("ok"):
            failures.append(name)

    healthy = not failures
    body = {
        "status": "ok" if healthy else "unavailable",
        "version": APP_VERSION,
        "time": stamp(),
        "checks": results,
        "failures": failures,
    }
    return jsonify(body), (200 if healthy else 503)
