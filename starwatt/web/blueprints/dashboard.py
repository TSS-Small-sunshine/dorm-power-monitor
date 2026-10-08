"""只读数据 API —— ``/api/data`` 与 ``/api/live``（M4 §4.2）。

契约**冻结**（``tests/regression/fixtures/api.json``）
====================================================

``GET /api/data?hours=24``（也支持 ``?start=&end=`` 显式区间）
    → ``{"hours", "rows": [{id, read_time, remain, ts}], "stats": {...}}``

``GET /api/live``
    → ``{"eqprice", "latest_ts", "monthly_breakdown", "monthly_projection",
        "run_status", "scrape_status", "stale", "stats"}``

两个端点都走 :func:`starwatt.web.security.read_access`（Q11）：
默认要求登录，``public_readonly=1`` 时允许匿名只读。

参数容错
========

``hours`` 非法（非数字 / 越界）→ 回落到 24，**不报 400**：
legacy 的行为是「怎么填都能出图」，前端不用为参数错误写错误处理。
"""
from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from starwatt.services import dashboard_service
from starwatt.web.security import read_access

bp = Blueprint("dashboard_api", __name__)

logger = logging.getLogger("starwatt.web")

__all__ = ["bp", "parse_hours"]

#: ``hours`` 的合法区间（1 小时 ~ 30 天）
MIN_HOURS = 1
MAX_HOURS = 720


def parse_hours(raw: str | None, default: int = dashboard_service.DEFAULT_HOURS) -> int:
    """``?hours=`` 解析：非法 / 越界一律回落默认值（不抛 400）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < MIN_HOURS or value > MAX_HOURS:
        return default
    return value


@bp.get("/api/data")
@read_access
def api_data():
    """历史数据 + 4 个 stat 值。"""
    hours = parse_hours(request.args.get("hours"))
    start = (request.args.get("start") or "").strip() or None
    end = (request.args.get("end") or "").strip() or None
    return jsonify(
        dashboard_service.history(hours=hours, start=start, end=end)
    )


@bp.get("/api/live")
@read_access
def api_live():
    """最新值 + 电表快照 + 本月预计电费。"""
    return jsonify(dashboard_service.live())
