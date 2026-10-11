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
（需要「DB 可写 / 调度器在跑」的深度就绪探测见
``/healthz/deep``，由 :mod:`starwatt.web.blueprints.health` 提供。）

安全响应头
==========

:func:`_register_security_headers` 给**每个**响应补上 nosniff / DENY /
Referrer-Policy / Permissions-Policy。**不加 CSP**（Vue SPA 的内联样式与
``modulepreload`` 很容易被 CSP 打坏，需要真实浏览器验证后再单开一轮）。
HSTS **只在请求本身是 HTTPS 时**才发 —— 纯 HTTP 部署发这个头毫无意义，
还会把浏览器钉到 https 上。

``ProxyFix`` 与 ``trust_proxy``
==============================

``X-Forwarded-For`` 是**客户端可伪造**的头：无条件信任它等于让攻击者
随便伪造来源 IP，登录失败锁定与 SSRF 判断会一起失效。所以默认
``trust_proxy=false``，只有 :func:`init_proxy_fix`（按配置）
才会启用 Werkzeug 的 ``ProxyFix``。
"""
from __future__ import annotations

import logging
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

from starwatt.config import get_settings
from starwatt.web.blueprints import ALL_BLUEPRINTS
from starwatt.web.blueprints.health import APP_VERSION

__all__ = [
    "APP_VERSION",
    "RESERVED_PREFIXES",
    "SECURITY_HEADERS",
    "create_app",
    "init_proxy_fix",
    "register_blueprints",
    "register_spa_fallback",
    "register_startup_tasks",
]

logger = logging.getLogger("starwatt.web")

#: 每个响应都补的安全头（值固定，不受请求影响）
SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}

#: HSTS 头（**仅 HTTPS 请求**才发；见 ``_register_security_headers``）
HSTS_HEADER = ("Strict-Transport-Security", "max-age=31536000; includeSubDomains")


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


def _register_security_headers(app: Flask) -> None:
    """给每个响应补安全头（审计缺陷 2）。

    * 只补**缺失**的头 —— 已有同名头（例如某个视图自己设了 nosniff）
      不覆盖、不重复添加，避免出现重复头
    * HSTS **只在** ``request.is_secure`` 为真时发：HTTP 部署发 HSTS
      等于告诉浏览器「以后都走 https」，而它根本没有 https
    """
    hsts_name, hsts_value = HSTS_HEADER

    @app.after_request
    def _security_headers(response):
        for name, value in SECURITY_HEADERS.items():
            if name not in response.headers:
                response.headers[name] = value
        if request.is_secure and hsts_name not in response.headers:
            response.headers[hsts_name] = hsts_value
        return response


def init_proxy_fix(app: Flask) -> bool:
    """``trust_proxy=true`` 时用 Werkzeug ``ProxyFix`` 包住 ``app.wsgi_app``。

    为什么默认不包：``X-Forwarded-For`` 可被客户端伪造，无条件信任它会让
    ``request.remote_addr`` 变成攻击者任意填的值 —— 登录失败锁定与 SSRF
    判断都会跟着失真（见 ``docs/audit/security.md`` F-04）。

    只取 **1 跳**（``x_for=x_proto=x_host=1``）：面对多级反代时宁可少信一层，
    也不要把整条用户可控的头链当真相。

    Returns:
        是否真的包上了（配置关闭时为 ``False``）。
    """
    if not app.config.get("TRUST_PROXY", False):
        return False
    if getattr(app.wsgi_app, "_starwatt_proxy_fix", False):
        return True  # 幂等：重复调用不叠加

    from werkzeug.middleware.proxy_fix import ProxyFix

    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    app.wsgi_app._starwatt_proxy_fix = True  # type: ignore[attr-defined]
    logger.info("已启用 ProxyFix（trust_proxy=true）：信任 1 跳 X-Forwarded-*")
    return True


def create_app(config: dict | None = None) -> Flask:
    """构造并返回 Flask 应用。

    Args:
        config: 测试注入的额外配置（``app.config.update``）；``None`` 表示不覆盖。
            同名键优先于配置中心读到的值（``SESSION_COOKIE_SECURE`` /
            ``TRUST_PROXY`` 都走这里）。

    Returns:
        已注册七个蓝图（含平台事件订阅）的 :class:`flask.Flask` 实例。
    """
    app = Flask(
        __name__,
        static_folder=_static_dir(),
        static_url_path="/static",
    )

    # ⚠️ 建表必须**先于**任何配置中心读取。
    #
    # ``MetaRepo.get()`` 直接 ``SELECT value FROM meta WHERE key = ?``，而
    # ``db.init()`` 之外的入口没人建这张表 —— 空数据目录（首次启动、或
    # 任何干净目录）里读配置会当场 ``no such table: meta``。
    #
    # 背景：``web.py:startup_once()``（由 post_fork 在 **worker 进程**里调用）才做
    # ``db.init()``，而 ``create_app()`` 在装配期就要读配置。所以这里补上
    # **幂等**的 ``db.init()``（建表 + 补列，不建号、不写默认值、不启动调度器）
    # ——那些仍然留在 startup_once 里，保持「单实例只做一次」。
    # 演练时正是这一步抓到的：旧库 + create_app() → 登录 500。
    from starwatt.db.connection import init as db_init

    db_init()

    # Q21：SESSION cookie 只走 HttpOnly + SameSite；Secure 与 ProxyFix 由配置决定
    # （审计缺陷 1：``SESSION_COOKIE_SECURE`` 以前从没人写，恒为 False）
    from starwatt.config_registry import get_bool

    settings = get_settings()
    app.config.update(
        SECRET_KEY=settings.flask_secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=get_bool("session_cookie_secure", False),
        TRUST_PROXY=get_bool("trust_proxy", False),
        JSON_AS_ASCII=False,
        MAX_CONTENT_LENGTH=4 * 1024 * 1024,  # 配置导入等 POST 的体积上限
    )
    if config:
        app.config.update(config)

    # ProxyFix 必须在处理第一个请求前包好（配置读在应用装配期，改它要重启）
    init_proxy_fix(app)
    _register_security_headers(app)

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
