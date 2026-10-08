"""自助配置（M4 §4.5，N4）—— 粘贴 H5 URL → 解析 → 验证 → 落库。

==============================  ======  ========================================
路径                             方法     说明
==============================  ======  ========================================
``/api/setup/parse-url``        POST    解析 H5 地址（SSRF 4 层防护 + 参数提取）
``/api/setup/verify``           POST    用解析结果真跑一次 F1（**不落库**）
``/api/setup/commit``           POST    写入配置（dorm_openid / room / eqprice）
==============================  ======  ========================================

访问控制：**仅管理员** + 写操作 CSRF。

📌 ``verify`` 不写任何配置：验证失败不该留下半截状态，用户可反复试。
📌 所有响应**绝不**回显 openid（Q20）。
"""
from __future__ import annotations

import logging
from typing import Any

from flask import Blueprint, jsonify, request

from starwatt.auth.decorators import current_user, require_csrf
from starwatt.services import setup_service
from starwatt.web.security import admin_access

bp = Blueprint("setup_api", __name__, url_prefix="/api/setup")

logger = logging.getLogger("starwatt.web")

__all__ = ["bp"]


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else {}


def _audit(action: str, target: str | None = None, details: dict | None = None) -> None:
    from starwatt.auth.service import write_audit

    user = current_user()
    write_audit(
        action,
        user_id=getattr(user, "id", None),
        target=target,
        ip=request.remote_addr,
        details=details,
    )


@bp.post("/parse-url")
@admin_access
@require_csrf
def parse_url():
    """解析学校 H5 地址（``{"url": "https://..."}``）。"""
    body = _json_body()
    try:
        parsed = setup_service.parse_url(str(body.get("url") or ""))
    except ValueError as exc:
        _audit("setup.parse_failed", details={"reason": str(exc)[:120]})
        return jsonify({"ok": False, "error": str(exc)}), 400

    _audit("setup.parsed", target=parsed["base_url"], details={"room_id": parsed["room_id"]})
    return jsonify({"ok": True, **parsed})


@bp.post("/verify")
@admin_access
@require_csrf
def verify():
    """真跑一次 F1 验证凭据（**不写配置**）。"""
    body = _json_body()
    result = setup_service.verify(
        openid=str(body.get("openid") or ""),
        room_id=str(body.get("room_id") or ""),
        base_url=str(body.get("base_url") or ""),
    )
    _audit("setup.verify", target=result.get("room_id") or None, details={"ok": result["ok"]})
    return jsonify(result), (200 if result["ok"] else 400)


@bp.post("/commit")
@admin_access
@require_csrf
def commit():
    """写入配置（``{"openid", "room_id", "eqprice", "base_url"}``）。"""
    body = _json_body()
    eqprice = body.get("eqprice")
    try:
        eqprice_value = float(eqprice) if eqprice not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "电价必须是数字"}), 400

    result = setup_service.commit(
        openid=str(body.get("openid") or ""),
        room_id=str(body.get("room_id") or ""),
        eqprice=eqprice_value,
        base_url=str(body.get("base_url") or ""),
    )
    _audit(
        "setup.commit",
        target=result.get("room_id") or None,
        details={"applied": result["applied"]},
    )
    status = 200 if not result["errors"] else 207
    return jsonify({"ok": not result["errors"], **result}), status
