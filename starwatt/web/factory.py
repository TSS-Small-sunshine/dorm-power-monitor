"""Flask 应用工厂 —— ``create_app()``（M2 最小实现，M4 逐步补齐蓝图）。

约定
====

* ``create_app()`` **不启动调度器**、**不初始化数据库** —— 它只装配 WSGI
  应用对象。调度器由 ``web.py:start_scheduler_once()`` 在 worker 内启动
  （B3 防线 2/3）；数据库由 ``db.init()`` 在进程启动时建（M4 的 app 启动钩子）。
* 蓝图统一从 :data:`starwatt.web.blueprints.ALL_BLUEPRINTS` 注册 ——
  「加一个页面」= 加一个蓝图文件 + 加一行，不改工厂。
* 未认证请求一律返回 **JSON 401**（Q21：彻底移除浏览器原生 Basic Auth）。
* 机器人回调（``/qq/events`` / ``/feishu/event``）是**第七个蓝图**
  :mod:`starwatt.web.blueprints.feishu` —— 它们是「平台推给我们」，认证靠
  平台签名，与用户登录态无关。
* 前端 SPA 由 :func:`register_spa_fallback` 兜底（M5）：非 API 的 GET 回
  ``static/index.html``，``/api/*`` 的未知路径仍然是 JSON 404。

``/healthz`` 为什么必须存在
==========================

Dockerfile 的 ``HEALTHCHECK`` 与 ``deploy/dorm-web.service`` 都依赖它。
它**不碰数据库、不碰网络** —— 探活端点自己变成故障源是最糟糕的设计。
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, abort, jsonify, send_from_directory

from starwatt.config import get_settings
from starwatt.web.blueprints import ALL_BLUEPRINTS
from starwatt.web.blueprints.health import APP_VERSION

__all__ = [
    "APP_VERSION",
    "RESERVED_PREFIXES",
    "create_app",
    "register_blueprints",
    "register_spa_fallback",
    "register_startup_tasks",
]

logger = logging.getLogger("starwatt.web")


def _static_dir() -> str | None:
    """前端构建产物目录（M5 产出）。

    不存在时传 ``None`` 给 Flask —— 否则 Flask 会对每个 ``/static/*``
    请求去 stat 一个不存在的目录，并在启动时打印警告噪音。
    """
    candidate = Path(get_settings().project_root) / "static"
    return str(candidate) if candidate.is_dir() else None


#: 这些前缀下的未知路径**必须**回 JSON 404，不能落到 SPA 兜底上
#: （否则前端会拿到一份 HTML，`JSON.parse` 报「服务端返回了非 JSON 响应」）
RESERVED_PREFIXES: tuple[str, ...] = (
    "api/",
    "static/",
    "feishu/",
    "qq/",
    "healthz",
)


def register_spa_fallback(app: Flask) -> bool:
    """把**非 API** 的 GET 交给前端 SPA（M5 §5.9）。

    为什么需要它：前端是客户端路由（`/login`、`/admin/config` 都是前端页面），
    用户直接刷新或收藏这些地址时，请求会打到后端 —— 后端必须回
    ``index.html`` 让前端自己路由，否则用户看到的是 JSON 404。

    三条边界（都在测试里钉着）：

    * 只在 **``static/index.html`` 存在**时注册 —— 没构建前端时行为与以前
      完全一致（未知路径 = JSON 404），后端不依赖前端产物
    * **``/api/*`` 等保留前缀**即使未注册也回 JSON 404（见 ``RESERVED_PREFIXES``）
    * **只接管 GET/HEAD** —— ``POST /login`` 这类错法仍回 JSON 405

    Returns:
        是否注册成功（未构建前端时返回 ``False``）。
    """
    if not app.config.get("SPA_FALLBACK", True):
        return False

    static_dir = _static_dir()
    if static_dir is None:
        return False
    index = Path(static_dir) / "index.html"
    if not index.is_file():
        return False

    @app.get("/")
    @app.get("/<path:path>")
    def spa(path: str = ""):
        """SPA 壳：把路由交给前端（真正的 404 由前端 ``/:pathMatch(.*)*`` 处理）。"""
        if path.startswith(RESERVED_PREFIXES):
            abort(404)  # → app.errorhandler(404) 的 JSON 响应
        response = send_from_directory(static_dir, "index.html")
        # 壳文件不缓存：升级后要立刻拿到新的 asset 清单（asset 名带 hash，本身可长缓存）
        response.headers["Cache-Control"] = "no-cache"
        return response

    logger.debug("SPA 兜底已注册：%s", index)
    return True


def register_blueprints(app: Flask) -> None:
    """注册全部业务蓝图（``starwatt.web.blueprints.ALL_BLUEPRINTS``）。"""
    for blueprint in ALL_BLUEPRINTS:
        app.register_blueprint(blueprint)
    logger.debug("已注册蓝图：%s", [b.name for b in ALL_BLUEPRINTS])


def register_startup_tasks(app: Flask) -> dict:
    """执行首次启动的例行维护（B4）—— **幂等**。

    做四件事（见 :func:`starwatt.services.admin_service.first_run_maintenance`）：

    1. ``db.init()`` 建表
    2. 删掉已下线的旧键（``admin_password``，L20）
    3. 把未写入的配置项按默认值落库
    4. **bootstrap 建号**（``BOOTSTRAP_ADMIN_PASSWORD``，用后即焚）+ 注册日志脱敏

    ⚠️ 由 ``web.py:startup_once()`` 在 **worker 进程**里调用 ——
    ``create_app()`` 本身保持纯装配（测试里不该建号、不该建表）。
    ⚠️ 失败**不阻断启动**：维护出问题（例如磁盘只读）时 Web 仍应起来。

    Returns:
        维护报告（``{"purged", "defaults", "bootstrap"}``）；失败时返回 ``{}``。
    """
    from starwatt.services.admin_service import first_run_maintenance

    with app.app_context():
        try:
            report = first_run_maintenance()
            logger.info(
                "启动维护完成：清理旧键 %s 项、写入默认值 %s 项、bootstrap=%s",
                report["purged"],
                report["defaults"],
                report["bootstrap"]["username"] or "跳过",
            )
            return report
        except Exception:  # noqa: BLE001 —— 维护失败不该阻止服务启动
            logger.exception("启动维护失败（忽略，服务继续启动）")
            return {}


def create_app(config: dict | None = None) -> Flask:
    """构造并返回 Flask 应用。

    Args:
        config: 测试注入的额外配置（``app.config.update``）；``None`` 表示不覆盖。

    Returns:
        已注册七个蓝图（含平台事件订阅）的 :class:`flask.Flask` 实例。
    """
    app = Flask(
        __name__,
        static_folder=_static_dir(),
        static_url_path="/static",
    )

    # Q21：SESSION cookie 只走 HttpOnly + SameSite，且密钥来自启动配置
    settings = get_settings()
    app.config.update(
        SECRET_KEY=settings.flask_secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # 配置导入等 POST 的体积上限
    )
    if config:
        app.config.update(config)

    # ⚠️ 建表与迁移必须在这里跑，不能只依赖 gunicorn 的 post_fork。
    #
    # 背景：``web.py:startup_once()``（由 post_fork 在 **worker 进程**里调用）才做
    # ``db.init()``，而 ``create_app()`` 刻意保持纯净（测试里不该建号）。
    # 于是**只有** gunicorn 那条路会迁移库结构，其它入口一律不会 ——
    # 拿旧库配新代码启动时会变成「登录 500 / no such column: disabled」，
    # 看起来像新版本坏了，实际是结构没升。
    #
    # 所以这里只补上**幂等**的 ``db.init()``（建表 + 加列，不建号、不写默认值、
    # 不启动调度器）——那些仍然留在 startup_once 里，保持「单实例只做一次」。
    # 演练时正是这一步抓到的：旧库 + create_app() → 登录 500。
    from starwatt.db.connection import init as db_init

    db_init()

    register_blueprints(app)
    register_spa_fallback(app)

    @app.errorhandler(404)
    def _not_found(_error):
        """JSON 404（SPA 兜底未接管时走到这里，见 register_spa_fallback）。"""
        return jsonify({"ok": False, "error": "not_found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_error):
        return jsonify({"ok": False, "error": "method_not_allowed"}), 405

    return app
