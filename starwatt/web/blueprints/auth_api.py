"""登录 / 登出 / 改密（M4 §4.2）。

端点
====

==========================  ======  ==========================================
路径                         方法     说明
==========================  ======  ==========================================
``/api/auth/login``         POST    登录（成功时**下发会话 Cookie**）
``/api/auth/logout``        POST    登出
``/api/auth/password``      POST    改密（强制首登改密也走这里）
``/api/auth/me``            GET     当前登录态（前端路由守卫用）
==========================  ======  ==========================================

CSRF 与 Cookie
==============

* 登录**不需要** CSRF（此时还没有会话，攻击者拿不到任何已登录上下文）
* 登出 / 改密需要 CSRF（:func:`starwatt.auth.decorators.require_csrf`）
* 会话 Cookie 一律 ``HttpOnly`` + ``SameSite=Lax``；``Secure`` 由
  :func:`starwatt.web.factory.create_app` 按环境决定（本地 HTTP 调试要关掉）
"""
from __future__ import annotations

import logging

from flask import Blueprint, current_app, g, jsonify, request

from starwatt.auth.constants import SESSION_COOKIE_NAME
from starwatt.auth.decorators import current_user, require_auth, require_csrf
from starwatt.services import auth_service

bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")

logger = logging.getLogger("starwatt.web")

__all__ = ["bp", "set_session_cookie"]


def _client_ip() -> str | None:
    """真实客户端 IP（``ProxyFix`` 已处理 ``X-Forwarded-For``）。"""
    return request.remote_addr


def set_session_cookie(response, token: str, *, max_age: int):
    """下发会话 Cookie（集中一处 —— 属性不容易漏配）。"""
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=max_age,
        httponly=True,
        samesite="Lax",
        secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", False)),
        path="/",
    )
    return response


def clear_session_cookie(response):
    """清掉会话 Cookie。"""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@bp.post("/login")
def login():
    """登录。"""
    payload = request.get_json(silent=True) or request.form
    username = str(payload.get("username") or "")
    password = str(payload.get("password") or "")

    result = auth_service.login(
        username,
        password,
        ip=_client_ip(),
        user_agent=request.headers.get("User-Agent"),
    )
    if not result["ok"]:
        status = result.pop("status", 401)
        return jsonify(result), status

    token = result["token"]
    response = jsonify(result)
    set_session_cookie(response, token, max_age=_session_max_age())
    return response


@bp.post("/logout")
@require_auth()
@require_csrf
def logout():
    """登出（删除服务端会话行 + 清 Cookie）。"""
    result = auth_service.logout(getattr(g, "session_token", None))
    return clear_session_cookie(jsonify(result))


@bp.post("/password")
@require_auth()
@require_csrf
def change_password():
    """改密（``{"old_password": ..., "new_password": ...}``）。

    ``must_change_password=1`` 的用户**必须**先走这里才能用其它接口
    （B4 的拦截由前端路由守卫 + ``/api/auth/me`` 的标记共同实现）。
    """
    from starwatt.auth import service as auth

    user = current_user()
    payload = request.get_json(silent=True) or request.form
    old_password = str(payload.get("old_password") or "")
    new_password = str(payload.get("new_password") or "")

    if not auth.authenticate(user.username, old_password, ip=_client_ip()).ok:
        auth.write_audit(
            "password.change_failed",
            user_id=user.id,
            target=user.username,
            ip=_client_ip(),
        )
        return jsonify({"ok": False, "error": "当前密码不正确"}), 403

    result = auth_service.change_password(user.id, new_password)
    if not result["ok"]:
        status = result.pop("status", 400)
        return jsonify(result), status

    auth.write_audit(
        "password.changed", user_id=user.id, target=user.username, ip=_client_ip()
    )
    response = jsonify(result)
    set_session_cookie(response, result["token"], max_age=_session_max_age())
    return response


@bp.get("/me")
@require_auth(allow_anonymous_read=True)
def me():
    """当前登录态（未登录返回 ``ok=false``，**不**返回 401 —— 供路由守卫判断）。"""
    user = current_user()
    if user is None:
        return jsonify({"ok": False, "authenticated": False, "user": None})
    return jsonify(
        {
            "ok": True,
            "authenticated": True,
            "user": auth_service.public_user(user),
            "csrf_token": getattr(g, "csrf_token", ""),
        }
    )


def _session_max_age() -> int:
    """Cookie 寿命 = 会话时长（小时 → 秒）。"""
    return int(current_app.config.get("SESSION_HOURS", 24)) * 3600
