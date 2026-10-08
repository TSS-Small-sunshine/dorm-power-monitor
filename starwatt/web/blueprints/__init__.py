"""Web 蓝图包（M4 §4.2）。

七个蓝图（``REWRITE_PLAN`` §1.2）
================================

==============  ==========================================================
蓝图             路由
==============  ==========================================================
``health``       ✅ 探活（``/healthz``）
``dashboard``    ✅ 只读数据 API（``/api/data`` / ``/api/live``）
``admin``        ✅ 配置七端点 + 用户 / 审计（``/api/admin/*``）
``auth_api``     ✅ 登录 / 登出 / 改密（``/api/auth/*``）
``oobe``         ✅ 引导（B4/B5，端点 10 → 2）
``setup``        ✅ 自助配置（openid → roomId）
``feishu``       ✅ 平台事件订阅（``/feishu/event`` + ``/qq/events``）
==============  ==========================================================

📌 蓝图**只做 HTTP 装配**：取参数 → 调 ``starwatt.services`` → 返回 JSON。
业务逻辑在 service，纯计算在 ``starwatt.domain`` —— 这样调度器与 Web
共享同一套算法，测试也不需要起 Flask。

📌 ``feishu`` 蓝图**不走会话认证**：两家平台都靠自身签名鉴权（fail-closed），
详见 :mod:`starwatt.web.blueprints.feishu`。
"""
from __future__ import annotations

from starwatt.web.blueprints import (
    admin,
    auth_api,
    dashboard,
    feishu,
    health,
    oobe,
    setup,
)

#: 全部蓝图（``factory.create_app`` 按这个顺序注册）
ALL_BLUEPRINTS = (
    health.bp,
    auth_api.bp,
    dashboard.bp,
    admin.bp,
    oobe.bp,
    setup.bp,
    feishu.bp,
)

__all__ = [
    "ALL_BLUEPRINTS",
    "admin",
    "auth_api",
    "dashboard",
    "feishu",
    "health",
    "oobe",
    "setup",
]
