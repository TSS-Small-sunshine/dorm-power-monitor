"""认证用例层（Web 视角）—— 登录 / 登出 / 改密 / 用户管理。

与 :mod:`starwatt.auth.service` 的分工
======================================

``auth.service`` 是**底层用例**（返回 ``User`` / ``AuthResult``，会抛异常）；
本模块是**给 Web 用的适配层**：把结果统一成「可直接 ``jsonify`` 的 dict」，
并把「哪些错误该回 4xx」一并定好。蓝图因此只做 HTTP 装配。

统一响应约定
============

::

    {"ok": bool, ...}                    成功
    {"ok": false, "error": "<中文说明>"}  失败（HTTP 400 / 401 / 403）

📌 **绝不回显**密码、密码哈希、token（除了登录成功时那一次）与 openid（Q20）。
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any

from starwatt.auth import service as auth
from starwatt.auth import session as session_mod
from starwatt.auth.constants import ROLE_ADMIN, ROLE_VIEWER, VALID_ROLES
from starwatt.db.repositories import UserRepo

logger = logging.getLogger("starwatt.auth")

__all__ = [
    "ROLE_ADMIN",
    "ROLE_VIEWER",
    "VALID_ROLES",
    "change_password",
    "create_user",
    "delete_user",
    "login",
    "logout",
    "public_user",
    "reset_password",
    "set_disabled",
    "set_role",
]

#: ``AuthResult.error`` → (HTTP 状态码, 中文提示)
_ERROR_MAP: dict[str, tuple[int, str]] = {
    "invalid": (401, "用户名或密码不正确"),
    "locked": (429, "失败次数过多，请 15 分钟后再试"),
    "disabled": (403, "该账号已被禁用"),
}


def public_user(user) -> dict[str, Any]:
    """用户的可公开字段（**不含**密码哈希）。"""
    if user is None:
        return {}
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "must_change_password": bool(user.must_change_password),
    }


def login(
    username: str,
    password: str,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> dict[str, Any]:
    """登录。

    Returns:
        成功：``{"ok": true, "user": {...}, "token": "...", "must_change_password": bool}``
        失败：``{"ok": false, "error": "..."}`` + ``"status"``（HTTP 码）
    """
    result = auth.authenticate(
        (username or "").strip(), password or "", ip=ip, user_agent=user_agent
    )
    if not result.ok:
        status, message = _ERROR_MAP.get(result.error or "", (401, "登录失败"))
        return {"ok": False, "error": message, "status": status}

    return {
        "ok": True,
        "user": public_user(result.user),
        "token": result.token,
        "must_change_password": result.must_change_password,
    }


def logout(token: str | None) -> dict[str, Any]:
    """登出（删除服务端会话行）。"""
    session_mod.revoke_session(token)
    return {"ok": True}


def change_password(user_id: int, new_password: str) -> dict[str, Any]:
    """改密 —— 成功后**踢掉该用户全部会话**，并为当前设备重签一个。

    为什么要重签：``auth.change_password`` 会 revoke 全部会话（旧 token 可能
    已泄露）。但用户刚改完密码不该被登出，所以这里立刻为当前设备建新会话。

    Args:
        user_id: 目标用户。
        new_password: 新密码（强度不足会被拒绝）。

    Returns:
        ``{"ok": true, "token": "..."}`` 或 ``{"ok": false, "error": "..."}``
    """
    try:
        auth.change_password(user_id, new_password)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}

    token = session_mod.create_session(user_id)
    logger.info("用户 %s 修改了密码，已重置全部会话", user_id)
    return {"ok": True, "token": token}


# ---------------------------------------------------------------------------
# 用户管理（管理后台）
# ---------------------------------------------------------------------------
def create_user(
    username: str, password: str, role: str = ROLE_VIEWER
) -> dict[str, Any]:
    """建号（OOBE 已无建号步，这里是管理后台的唯一入口）。"""
    try:
        user = auth.create_user(username, password, role=role)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}
    except sqlite3.IntegrityError:
        return {"ok": False, "error": f"用户名 {username!r} 已存在", "status": 409}
    return {"ok": True, "user": public_user(user)}


def set_role(user_id: int, role: str) -> dict[str, Any]:
    """改角色（**不能把最后一个管理员降级** —— 否则没人能进后台）。"""
    if role not in VALID_ROLES:
        return {"ok": False, "error": f"角色只能是 {VALID_ROLES}", "status": 400}
    user = UserRepo.get_by_id(user_id)
    if user is None:
        return {"ok": False, "error": "用户不存在", "status": 404}
    if user.is_admin and role != ROLE_ADMIN and _admin_count() <= 1:
        return {
            "ok": False,
            "error": "这是最后一个管理员账号，降级后将无人能登录后台",
            "status": 400,
        }
    try:
        auth.set_role(user_id, role)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}
    return {"ok": True}


def set_disabled(user_id: int, disabled: bool) -> dict[str, Any]:
    """禁用 / 启用（禁用会立刻踢掉其全部会话，且**不能禁用最后一个管理员**）。"""
    user = UserRepo.get_by_id(user_id)
    if user is None:
        return {"ok": False, "error": "用户不存在", "status": 404}
    if disabled and user.is_admin and _admin_count() <= 1:
        return {
            "ok": False,
            "error": "这是最后一个管理员账号，禁用后将无人能登录后台",
            "status": 400,
        }
    auth.set_disabled(user_id, disabled)
    return {"ok": True}


def reset_password(user_id: int, new_password: str) -> dict[str, Any]:
    """管理员重置他人密码（F2「改密码」）。

    目标用户会被**立刻踢下线**，并被要求**首登再改一次**（``must_change``）——
    临时密码是管理员转达的，不等于用户本人持有。
    """
    if UserRepo.get_by_id(user_id) is None:
        return {"ok": False, "error": "用户不存在", "status": 404}
    try:
        auth.reset_password(user_id, new_password)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "status": 400}
    return {"ok": True}


def delete_user(user_id: int) -> dict[str, Any]:
    """删除用户（**不能删掉最后一个管理员** —— 否则没人能进后台）。"""
    user = UserRepo.get_by_id(user_id)
    if user is None:
        return {"ok": False, "error": "用户不存在", "status": 404}
    if user.is_admin and _admin_count() <= 1:
        return {
            "ok": False,
            "error": "这是最后一个管理员账号，删除后将无人能登录后台",
            "status": 400,
        }
    auth.delete_user(user_id)
    return {"ok": True}


def _admin_count() -> int:
    """当前管理员数量。"""
    return sum(1 for user in UserRepo.list_all() if user.is_admin)
