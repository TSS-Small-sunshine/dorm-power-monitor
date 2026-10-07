"""Flask 视图装饰器 —— ``require_auth`` / ``require_csrf``。

读取顺序（token）
=================

1. Cookie ``dorm_session``（浏览器默认路径）
2. 请求头 ``Authorization: Bearer <token>``（脚本 / API 调用）
3. 请求头 ``X-Auth-Token``

注入到 ``flask.g``
==================

===========================  ==========================================
``g.current_user``           ``User`` 或 ``None``
``g.session_token``          原始 token（用于签发 CSRF token）
``g.csrf_token``             该会话的 CSRF token（模板直接可用）
===========================  ==========================================

错误响应统一为 ``{"ok": false, "error": "<code>"}``，
``401`` 未登录 / ``403`` 权限不足或 CSRF 失败。
"""
from __future__ import annotations

import functools
import logging

from flask import g, jsonify, request

from starwatt.auth import csrf as csrf_mod
from starwatt.auth import session as session_mod
from starwatt.auth.constants import ROLE_ADMIN, SESSION_COOKIE_NAME

logger = logging.getLogger("starwatt.auth.decorators")

__all__ = [
    "AUTH_HEADER",
    "BEARER_PREFIX",
    "current_user",
    "extract_token",
    "login_required",
    "require_admin",
    "require_auth",
    "require_csrf",
]

AUTH_HEADER = "Authorization"
BEARER_PREFIX = "Bearer "
TOKEN_HEADER = "X-Auth-Token"


def extract_token() -> str | None:
    """按 cookie → ``Authorization`` → ``X-Auth-Token`` 顺序取 token。"""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        return token
    header = request.headers.get(AUTH_HEADER) or ""
    if header.startswith(BEARER_PREFIX):
        return header[len(BEARER_PREFIX) :].strip() or None
    return (request.headers.get(TOKEN_HEADER) or "").strip() or None


def current_user():
    """返回当前 ``User``（未登录 ``None``）。依赖 ``_load_identity`` 已跑过。"""
    return getattr(g, "current_user", None)


def _load_identity() -> None:
    """把 token → 用户 + CSRF token 注入 ``g``（每个请求一次）。"""
    if getattr(g, "_identity_loaded", False):
        return
    token = extract_token()
    user = session_mod.validate_session(token) if token else None
    g.session_token = token if user is not None else None
    g.current_user = user
    g.csrf_token = csrf_mod.issue(g.session_token) if user is not None else ""
    g._identity_loaded = True


def _deny(code: str, status: int):
    return jsonify({"ok": False, "error": code}), status


def require_auth(role: str | None = None, allow_anonymous_read: bool = False):
    """要求已登录；``role="admin"`` 时额外要求管理员。

    Args:
        role: ``None`` 表示任意已登录用户；``"admin"`` 表示仅管理员。
        allow_anonymous_read: ``True`` 时放行 ``GET``/``HEAD``/``OPTIONS``
            （对应 Q11 的 ``public_readonly`` 开关）。
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            _load_identity()

            if g.current_user is None:
                if allow_anonymous_read and request.method in csrf_mod.SAFE_METHODS:
                    return view(*args, **kwargs)
                return _deny("unauthorized", 401)

            if role == ROLE_ADMIN and not g.current_user.is_admin:
                return _deny("forbidden", 403)

            return view(*args, **kwargs)

        return wrapper

    return decorator


def require_admin(view):
    """仅管理员（等价 ``require_auth(role="admin")``）。"""
    return require_auth(role=ROLE_ADMIN)(view)


def require_csrf(view):
    """对非幂等方法校验 CSRF。必须**内层**（在 ``require_auth`` 之后）。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if request.method in csrf_mod.SAFE_METHODS:
            return view(*args, **kwargs)

        _load_identity()
        submitted = request.headers.get(csrf_mod.CSRF_HEADER)
        if not submitted:
            submitted = request.form.get(csrf_mod.CSRF_FORM_FIELD)
        if not submitted and request.is_json:
            payload = request.get_json(silent=True) or {}
            submitted = payload.get(csrf_mod.CSRF_FORM_FIELD)

        if not csrf_mod.validate(g.session_token, submitted):
            logger.warning("CSRF 校验失败：%s %s", request.method, request.path)
            return _deny("csrf_failed", 403)
        return view(*args, **kwargs)

    return wrapper


#: ``login_required`` 是 ``require_auth()`` 的别名，便于阅读
login_required = require_auth
