"""OOBE 引导（M4 §4.4，B4 / B5）—— **只有两个端点**。

============================  ======  ==========================================
路径                           方法     说明
============================  ======  ==========================================
``/api/oobe/state``           GET     当前步 / 全部步 / 当前值
``/api/oobe/advance``         POST    保存本步并前进 / 后退 / 跳过 / 完成
============================  ======  ==========================================

legacy 的 10 个端点（save-state / next / prev / skip-step / validate-feishu /
validate-webhook / complete / admin/api/oobe/save …）**全部合并**到这两个：

* 前进 / 后退 / 跳过 → ``advance`` 的 ``direction`` + ``skip``
* 完成 → 走到最后一步即完成（不需要单独的 ``/complete``）
* 渠道连通性测试 → ``POST /api/admin/config/test``

访问控制：**仅管理员**（OOBE 是配置操作，读也要登录态 —— 用 ``admin_access``）。
写操作额外要求 CSRF。
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from starwatt.auth.decorators import current_user, require_csrf
from starwatt.services import oobe_service
from starwatt.web.security import admin_access

bp = Blueprint("oobe_api", __name__, url_prefix="/api/oobe")

logger = logging.getLogger("starwatt.web")

__all__ = ["bp"]


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


@bp.get("/state")
@admin_access
def get_state():
    """当前引导状态（含每一步的表单元数据）。"""
    return jsonify(oobe_service.state())


@bp.post("/advance")
@admin_access
@require_csrf
def advance():
    """保存本步并移动。

    请求体：``{"direction": "next"|"prev", "values": {...}, "skip": false}``
    """
    from starwatt.auth.service import write_audit

    body = _json_body()
    direction = str(body.get("direction") or "next")
    values = body.get("values")
    payload = oobe_service.advance(
        direction=direction,
        values=values if isinstance(values, dict) else None,
        skip=bool(body.get("skip")),
    )

    user = current_user()
    write_audit(
        "oobe.advance",
        user_id=getattr(user, "id", None),
        target=payload["current"]["key"],
        ip=request.remote_addr,
        details={
            "step": payload["step"],
            "completed": payload["completed"],
            "applied": payload["saved"]["applied"],
        },
    )
    status = 200 if not payload["saved"]["errors"] else 207
    return jsonify(payload), status
