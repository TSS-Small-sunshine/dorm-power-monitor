"""StarWatt —— 宿舍电量监控 / dorm power monitor.

版本与代号（Q23 / ``docs/NAMING.md``）::

    __version__  = "2.0.2"
    __codename__ = ("星核", "Star Core")
    __product__  = ("星瓦", "StarWatt")

📌 **patch 版本要跟着 tag 走**：`2.0.1` 发布时这里还停在 `2.0.0`，于是
`/healthz` 与 ``--version`` 报的版本和镜像 tag 对不上 —— 而升级验收正是
靠 `/healthz` 确认「装的是哪一版」（见 ``docs/AGENT_RUNBOOK.md``）。

本模块**刻意不 import 任何子模块**，避免包导入期的循环依赖与副作用。
"""
from __future__ import annotations

__version__ = "2.0.2"
__codename__ = ("星核", "Star Core")
__product__ = ("星瓦", "StarWatt")

__all__ = ["__codename__", "__product__", "__version__"]


def version_string() -> str:
    """``starwatt 2.0.2 (星核 / Star Core)`` —— CLI ``--version`` 与 ``/healthz`` 共用。"""
    zh, en = __codename__
    return f"starwatt {__version__} ({zh} / {en})"
