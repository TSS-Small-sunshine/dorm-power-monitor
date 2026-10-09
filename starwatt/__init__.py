"""StarWatt —— 宿舍电量监控 / dorm power monitor.

版本与代号（Q23 / ``docs/NAMING.md``）::

    __version__  = "2.0.0"
    __codename__ = ("星核", "Star Core")
    __product__  = ("星瓦", "StarWatt")

本模块**刻意不 import 任何子模块**，避免包导入期的循环依赖与副作用。
"""
from __future__ import annotations

__version__ = "2.0.0"
__codename__ = ("星核", "Star Core")
__product__ = ("星瓦", "StarWatt")

__all__ = ["__codename__", "__product__", "__version__"]


def version_string() -> str:
    """``starwatt 2.0.0 (星核 / Star Core)`` —— CLI ``--version`` 与 ``/healthz`` 共用。"""
    zh, en = __codename__
    return f"starwatt {__version__} ({zh} / {en})"
