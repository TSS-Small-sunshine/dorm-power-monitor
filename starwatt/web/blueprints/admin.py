"""管理后台 API —— 配置七端点 + 用户管理 + 审计（M4 §4.2 / §4.6）。

七个配置端点（Q15）
==================

================================  ======  ==========================
路径                               方法     作用
================================  ======  ==========================
``/api/admin/config/schema``      GET     表单元数据（分组 / 类型 / 校验）
``/api/admin/config``             GET     当前值（secret 脱敏）
``/api/admin/config``             PUT     批量更新（逐键独立校验）
``/api/admin/config/test``        POST    发送测试消息（合并 OOBE 的两个 validate）
``/api/admin/config/export``      GET     导出 JSON（N16）
``/api/admin/config/import``      POST    导入 JSON（合并语义）
``/api/admin/config/reset``       POST    恢复默认值
================================  ======  ==========================

用户管理（F2：列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除）
=========================================================

============================================  ======  ================
路径                                           方法     作用
============================================  ======  ================
``/api/admin/users``                          GET     列表
``/api/admin/users``                          POST    建号（OOBE 已无建号步，这里是唯一入口）
``/api/admin/users/<id>``                     PATCH   改角色 / 禁用 / 启用
``/api/admin/users/<id>/password``            POST    重置密码（目标用户强制首登改密）
``/api/admin/users/<id>``                     DELETE  删除
``/api/admin/audit``                          GET     审计日志
============================================  ======  ================

访问控制
========

* 全部走 :func:`starwatt.web.security.admin_access` —— **仅管理员**
* 写操作额外要求 CSRF（``require_csrf`` 必须在内层）：
  用户登录态存在 Cookie 里，没有 CSRF 的话第三方页面可以诱导浏览器
  替用户改配置（Q17 的「输入校验 + CSRF」）
* 用户管理的三条安全护栏（都在这里挡住，service 层另有兜底）：
  ① **不能禁用/删除自己**（否则当场把自己锁在门外）
  ② **不能把最后一个管理员降级/禁用/删除**（否则没人能进后台）
  ③ 重置密码后目标用户**必须首登改密**（临时密码不等于本人持有）
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from starwatt.auth.decorators import current_user, require_csrf
from starwatt.services import admin_service, auth_service
from starwatt.web.security import admin_access

bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")

logger = logging.getLogger("starwatt.web")

__all__ = ["bp"]


def _json_body() -> dict[str, Any]:
    """取 JSON 请求体（非法 / 空 → 空 dict）。"""
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _audit(action: str, target: str | None = None, details: dict | None = None) -> None:
    """写一条审计（配置类操作**必须留痕**，Q17）。"""
    from starwatt.auth.service import write_audit

    user = current_user()
    write_audit(
        action,
        user_id=getattr(user, "id", None),
        target=target,
        ip=request.remote_addr,
        user_agent=request.headers.get("User-Agent"),
        details=details,
    )


def _result(result: dict[str, Any]):
    """service 的统一响应 → HTTP。

    service 返回 ``{"ok": bool, "error": ..., "status": int}``（见
    :mod:`starwatt.services.auth_service`），这里只做「状态码 + JSON」的翻译，
    不改变语义 —— 蓝图不重复业务判断。
    """
    if result.get("ok"):
        return jsonify(result), 200
    return jsonify(result), int(result.get("status") or 400)


def _is_self(user_id: int) -> bool:
    """目标用户是不是当前登录者（护栏 ① 用）。"""
    return getattr(current_user(), "id", None) == user_id


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
@bp.get("/config/schema")
@admin_access
def config_schema():
    """配置项元数据（前端渲染表单用）。"""
    return jsonify(admin_service.config_schema())


@bp.get("/config")
@admin_access
def config_get():
    """当前配置值（secret 默认脱敏）。"""
    include_secrets = request.args.get("include_secrets") in ("1", "true", "yes")
    return jsonify(admin_service.config_values(include_secrets=include_secrets))


@bp.put("/config")
@admin_access
@require_csrf
def config_put():
    """批量更新配置。"""
    payload = _json_body()
    result = admin_service.config_update(payload)
    _audit("config.update", details={"applied": result["applied"], "errors": result["errors"]})
    status = 200 if not result["errors"] else 207  # 207 = 部分成功
    return jsonify(result), status


@bp.post("/config/test")
@admin_access
@require_csrf
def config_test():
    """发送测试消息（``{"target": "feishu_group" | "qq"}``）。"""
    target = str(_json_body().get("target") or "")
    result = admin_service.config_test(target)
    _audit("config.test", target=target, details={"ok": result["ok"]})
    return jsonify(result), (200 if result["ok"] else 400)


@bp.get("/config/export")
@admin_access
def config_export():
    """导出配置（``?include_secrets=1`` 才带明文凭据）。"""
    include_secrets = request.args.get("include_secrets") in ("1", "true", "yes")
    payload = admin_service.config_export(include_secrets=include_secrets)
    _audit("config.export", details={"include_secrets": include_secrets})
    return jsonify(payload)


@bp.post("/config/import")
@admin_access
@require_csrf
def config_import():
    """导入配置（合并语义；脱敏值自动跳过）。"""
    allow_secrets = bool(_json_body().get("allow_secrets"))
    payload = _json_body().get("payload") or _json_body()
    result = admin_service.config_import(payload, allow_secrets=allow_secrets)
    _audit(
        "config.import",
        details={
            "applied": len(result["applied"]),
            "skipped": len(result["skipped"]),
            "errors": len(result["errors"]),
        },
    )
    return jsonify(result), (200 if not result["errors"] else 207)


@bp.post("/config/reset")
@admin_access
@require_csrf
def config_reset():
    """恢复默认值（不动运行时状态）。"""
    result = admin_service.config_reset()
    _audit("config.reset", details=result)
    return jsonify(result)


# ---------------------------------------------------------------------------
# 功能开关 / 日志状态（5.7 / 5.8）
# ---------------------------------------------------------------------------
@bp.get("/flags")
@admin_access
def flags():
    """全部开关 + 此刻的生效状态与抑制原因（前端「功能开关」页用）。"""
    return jsonify(admin_service.flags_state())


@bp.get("/logging")
@admin_access
def logging_status():
    """日志设置 + 每个类别**实际生效**的级别（前端「日志设置」页用）。"""
    return jsonify(admin_service.logging_state())


# ---------------------------------------------------------------------------
# 用户 / 审计
# ---------------------------------------------------------------------------
@bp.get("/users")
@admin_access
def users():
    """用户列表（不含密码哈希）。"""
    return jsonify({"users": admin_service.list_users()})


@bp.post("/users")
@admin_access
@require_csrf
def create_user():
    """建号（``{username, password, role?}``）—— OOBE 已无建号步，这是唯一入口。"""
    payload = _json_body()
    result = auth_service.create_user(
        str(payload.get("username") or "").strip(),
        str(payload.get("password") or ""),
        role=str(payload.get("role") or auth_service.ROLE_VIEWER),
    )
    _audit(
        "user.create",
        target=str(payload.get("username") or ""),
        details={"ok": result["ok"]},
    )
    return _result(result)


@bp.patch("/users/<int:user_id>")
@admin_access
@require_csrf
def update_user(user_id: int):
    """改角色 / 禁用 / 启用（``{role?}`` 与 ``{disabled?}`` 可同时给）。"""
    payload = _json_body()
    if not any(key in payload for key in ("role", "disabled")):
        return jsonify({"ok": False, "error": "请提供 role 或 disabled"}), 400

    if _is_self(user_id) and payload.get("disabled") is True:
        return jsonify({"ok": False, "error": "不能禁用当前登录的账号"}), 400

    result: dict[str, Any] = {"ok": True}
    if "role" in payload:
        result = auth_service.set_role(user_id, str(payload.get("role") or ""))
        _audit("user.role", target=str(user_id), details={"role": payload.get("role")})
        if not result["ok"]:
            return _result(result)
    if "disabled" in payload:
        result = auth_service.set_disabled(user_id, bool(payload.get("disabled")))
        _audit(
            "user.disabled",
            target=str(user_id),
            details={"disabled": bool(payload.get("disabled"))},
        )
        if not result["ok"]:
            return _result(result)
    return _result(result)


@bp.post("/users/<int:user_id>/password")
@admin_access
@require_csrf
def reset_user_password(user_id: int):
    """重置密码（``{password}``）—— 目标用户被踢下线并**强制首登改密**。"""
    result = auth_service.reset_password(user_id, str(_json_body().get("password") or ""))
    _audit("user.password_reset", target=str(user_id), details={"ok": result["ok"]})
    return _result(result)


@bp.delete("/users/<int:user_id>")
@admin_access
@require_csrf
def delete_user(user_id: int):
    """删除用户（不能删自己，也不能删最后一个管理员）。"""
    if _is_self(user_id):
        return jsonify({"ok": False, "error": "不能删除当前登录的账号"}), 400
    result = auth_service.delete_user(user_id)
    _audit("user.delete", target=str(user_id), details={"ok": result["ok"]})
    return _result(result)


@bp.get("/audit")
@admin_access
def audit():
    """审计日志（``?limit=&action=``）。"""
    raw_limit = request.args.get("limit", "100")
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        limit = 100
    action = (request.args.get("action") or "").strip() or None
    return jsonify({"entries": admin_service.list_audit(limit=limit, action=action)})
