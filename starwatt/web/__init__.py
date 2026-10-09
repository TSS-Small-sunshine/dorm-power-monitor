"""``starwatt.web`` —— Web 层（Flask 应用工厂 + 蓝图）。

现状（M4 完成）
==============

* ``factory.create_app`` —— 应用装配（配置 → 蓝图 → 错误处理），
  **不启动调度器、不建表**（那是 ``web.py:startup_once()`` 的活）
* ``blueprints/`` —— **七个蓝图**：``health`` / ``auth_api`` / ``dashboard_api``
  / ``admin_api`` / ``oobe_api`` / ``setup_api`` / ``feishu``
  （最后一个是飞书 + QQ 的平台事件订阅入口）
* ``security.py`` —— 访问控制（``read_access`` / ``admin_access`` / CSRF）

为什么 ``__init__`` 不 import ``factory``
========================================

与 ``starwatt.auth`` 同一原则：**导入包不该拉起 Web 栈**。
CLI / 调度器 / 抓取器只需要 ``starwatt.scraper``，让它们被迫 import
Flask 是没必要的耦合（也拖慢启动）。需要应用时显式::

    from starwatt.web.factory import create_app
"""
from __future__ import annotations

__all__: list[str] = []
