"""``starwatt.services`` —— 用例编排（Web 与调度器共享，M4 §4.1）。

为什么要这一层
==============

legacy 把「取数 + 算 + 拼 JSON」全写在 Flask 视图里，于是：

* 调度器想复用同一个算法只能 import ``web.py``（**反向依赖**）
* 视图函数长达 100+ 行，测试必须起 Flask 才能验证算法

重写后：视图只做「取参数 → 调 service → 返回 JSON」，算法在 service，
纯计算在 :mod:`starwatt.domain`。

==============  ==========================================================
模块             职责
==============  ==========================================================
``dashboard_service``  仪表盘：``/api/data`` / ``/api/live`` 的 payload
``admin_service``      配置七端点（Q15）+ 用户 / 审计 + 启动维护（B4）
``auth_service``       登录 / 登出 / 改密 / 用户管理（Web 适配层）
``oobe_service``       OOBE 引导（配置中心的子集视图，B5）
``setup_service``      自助配置（openid → roomId 解析，N4）
==============  ==========================================================

📌 这些模块**都不 import Flask** —— 蓝图只做 HTTP 装配，因此同一套用例
可以在 CLI / 调度器 / 测试里直接调用。
"""
from __future__ import annotations

from starwatt.services import (
    admin_service,
    auth_service,
    dashboard_service,
    oobe_service,
    setup_service,
)

__all__ = [
    "admin_service",
    "auth_service",
    "dashboard_service",
    "oobe_service",
    "setup_service",
]
