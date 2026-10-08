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
from starwatt.web.security import read_access, refresh_access

bp = Blueprint("dashboard_api", __name__)

logger = logging.getLogger("starwatt.web")

__all__ = ["bp", "parse_days", "parse_hours"]

#: ``hours`` 的合法区间（1 小时 ~ 30 天）
MIN_HOURS = 1
MAX_HOURS = 720

#: ``days`` / ``limit`` 的上限（10 年）—— 挡住扫全表的输入
MAX_DAYS = 3650


def parse_hours(raw: str | None, default: int = dashboard_service.DEFAULT_HOURS) -> int:
    """``?hours=`` 解析：非法 / 越界一律回落默认值（不抛 400）。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < MIN_HOURS or value > MAX_HOURS:
        return default
    return value


def parse_days(raw: str | None, default: int) -> int:
    """``?days=`` / ``?limit=`` 解析（同 ``parse_hours``：容错优先）。

    上限 3650（10 年）—— 挡住 ``?days=99999999`` 这类让 SQL 扫全表的输入。
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if value < 1 or value > MAX_DAYS:
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


@bp.get("/api/site")
def api_site():
    """品牌信息（M5 §5.2）—— **公开**，无需登录。

    为什么公开：登录页本身就要显示站点名与主题色（否则首屏只能写死一个
    标题，用户改 `site_name` 后登录页不变）。这里**只**暴露三个字段，
    且都不是秘密：

    * ``site_name`` / ``theme_color`` —— 配置中心「站点」分组，本来就印在页面上
    * ``app_version`` —— 排障用，``/healthz`` 也公开返回它

    ⚠️ 不要往这里加任何「数据」字段（电量、房间、openid）—— 那会把一个
    品牌端点变成未认证的数据出口。数据一律走 ``read_access`` 的
    ``/api/data`` / ``/api/live``（Q11：默认需登录）。
    """
    return jsonify(dashboard_service.site())


# ---------------------------------------------------------------------------
# Dashboard 4 段（E1）：概览 / 历史 / 违规 / 电表
# ---------------------------------------------------------------------------
@bp.get("/api/violations")
@read_access
def api_violations():
    """违规记录（E9）。"""
    return jsonify(dashboard_service.violations(days=parse_days(request.args.get("days"), 30)))


@bp.get("/api/payments")
@read_access
def api_payments():
    """缴费记录（E10）。"""
    return jsonify(
        dashboard_service.payments(
            days=parse_days(request.args.get("days"), 90),
            limit=parse_days(request.args.get("limit"), 200),
        )
    )


@bp.get("/api/daily")
@read_access
def api_daily():
    """每日用电曲线（E6）。"""
    return jsonify(dashboard_service.daily_usage(days=parse_days(request.args.get("days"), 30)))


@bp.post("/api/refresh")
@refresh_access
def api_refresh():
    """手动刷新（E13）—— 触发一轮抓取，**不推送飞书**。

    为什么这个端点不是 ``read_access``：它会真的去访问学校接口并写库
    （SCOPE_DECISION G7：「``/api/refresh`` 是写操作，token 守卫默认改为开」）。
    两种调用方都能用（守卫细节见 :func:`starwatt.web.security.refresh_access`）：

    * **浏览器**：管理员会话 + CSRF（页面上那个「刷新」按钮）
    * **脚本 / 调度器**：``X-Internal-Token``

    ⚠️ CSRF 由守卫**内层**处理，这里不要再叠 ``@require_csrf``。
    ⚠️ 只读用户（viewer）点不了 —— 让他触发外部请求没有正当理由。
    """
    result = dashboard_service.refresh()
    logger.info(
        "手动刷新：ok=%s room=%s records=%s", result["ok"], result["room_id"] or "-", result["records"]
    )
    return jsonify(result)
