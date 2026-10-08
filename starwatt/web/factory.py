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

``/healthz`` 为什么必须存在
==========================

Dockerfile 的 ``HEALTHCHECK`` 与 ``deploy/dorm-web.service`` 都依赖它。
它**不碰数据库、不碰网络** —— 探活端点自己变成故障源是最糟糕的设计。
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, jsonify

from starwatt.config import get_settings
from starwatt.web.blueprints import ALL_BLUEPRINTS
from starwatt.web.blueprints.health import APP_VERSION

__all__ = [
    "APP_VERSION",
    "create_app",
    "register_blueprints",
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

    register_blueprints(app)

    @app.errorhandler(404)
    def _not_found(_error):
        """JSON 404（SPA 路由由 M5 的静态兜底处理）。"""
        return jsonify({"ok": False, "error": "not_found"}), 404

    @app.errorhandler(405)
    def _method_not_allowed(_error):
        return jsonify({"ok": False, "error": "method_not_allowed"}), 405

    return app
