"""类型强制转换 —— 收敛旧代码里散落的两份实现（L9）。

旧代码里 ``_coerce_float`` 同时存在于 ``db.py`` 与 ``dorm_power.py``，
``_coerce_str`` 同理。重写后**只此一份**，被数据层与抓取层共用。

设计原则
========

* **不抛异常** —— 门户返回的 JSON 字段类型不稳定（``"12.34"`` / ``12.34`` /
  ``null`` / ``""`` 都出现过），解析失败一律返回 ``None``，
  由调用方决定「缺失」的业务含义
* **空串等于缺失** —— ``""`` 与 ``None`` 同等对待（门户用空串表示未抄表）
* **不做四舍五入** —— 精度由展示层决定（``_stats`` 保留 2 位、
  ``run_status`` 保留 3 位），数据层保持原值
"""
from __future__ import annotations

from typing import Any

__all__ = ["coerce_bool", "coerce_float", "coerce_int", "coerce_str"]


def coerce_float(value: Any) -> float | None:
    """转 ``float``；``None`` / ``""`` / 不可解析 → ``None``。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):          # bool 是 int 的子类，但语义上不是数值
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def coerce_str(value: Any) -> str | None:
    """转去空白的 ``str``；``None`` / 空串 / 全空白 → ``None``。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def coerce_int(value: Any) -> int | None:
    """转 ``int``；浮点先取整，不可解析 → ``None``。"""
    number = coerce_float(value)
    if number is None:
        return None
    return int(number)


def coerce_bool(value: Any, default: bool = False) -> bool:
    """宽松布尔解析。

    接受（不分大小写）：``1`` ``true`` ``yes`` ``on`` ``y`` ``t`` → ``True``
    　　　　　　　　　　``0`` ``false`` ``no`` ``off`` ``n`` ``f`` → ``False``

    ``None`` / ``""`` / 其他值 → ``default``。

    这是给「配置项从 meta 表读出来的字符串」用的 —— 与旧代码
    ``raw == "1"`` 的严格判断不同，这里额外容忍 ``true`` / ``yes``，
    因为 WebUI 的表单控件可能提交这些值。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on", "y", "t"):
        return True
    if text in ("0", "false", "no", "off", "n", "f"):
        return False
    return default
