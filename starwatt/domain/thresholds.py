"""阈值判定 —— **纯函数**（零 IO，R1 守卫强制）。

为什么要有这一层
================

「低电是哪个颜色」「电表算不算离线」「数据算不算陈旧」这三件事在 legacy 里
散落在 4 个地方（``dorm_power._template_for_remain`` /
``dorm_power._is_offline`` / ``feishu_bot._is_offline`` / ``web.py`` 的模板），
判定口径已经开始不一致（例如 ``feishu_bot`` 用固定三个中文标签，
``dorm_power`` 读 ``offline_status_values`` 配置）。

重写后：**判定逻辑只在这里**，阈值由调用方传入（配置在调用方读）。

📌 为什么参数是显式的而不是在这里读配置：``starwatt.config_registry``
在 R1 的禁止清单里 —— domain 层必须能脱离 DB 单测。
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

__all__ = [
    "DEFAULT_ORANGE_BELOW",
    "DEFAULT_RED_BELOW",
    "DEFAULT_BLUE_BELOW",
    "DEFAULT_ONLINE_VALUES",
    "color_zone",
    "is_low",
    "is_offline",
    "is_stale",
]

#: legacy 的低电三档（与注册表默认值一致）
DEFAULT_RED_BELOW = 30.0
DEFAULT_ORANGE_BELOW = 80.0
DEFAULT_BLUE_BELOW = 200.0

#: 视为「在线」的电表状态值（注册表 ``offline_status_values`` 的默认值）
DEFAULT_ONLINE_VALUES: tuple[str, ...] = ("在线", "正常", "通讯正常")


def color_zone(
    remain: float | None,
    *,
    red_below: float = DEFAULT_RED_BELOW,
    orange_below: float = DEFAULT_ORANGE_BELOW,
    blue_below: float = DEFAULT_BLUE_BELOW,
) -> str:
    """剩余电量 → 颜色分区（**边界严格小于**，与 ``cards.json`` 快照一致）。

    ==============  =========  ==========
    条件            颜色        语义
    ==============  =========  ==========
    缺失            ``blue``    数据没拿到，不吓人
    ``< red``       ``red``     危险
    ``< orange``    ``orange``  偏紧
    ``< blue``      ``blue``    正常
    其余            ``green``   充足
    ==============  =========  ==========
    """
    if remain is None:
        return "blue"
    if remain < red_below:
        return "red"
    if remain < orange_below:
        return "orange"
    if remain < blue_below:
        return "blue"
    return "green"


def is_low(remain: float | None, *, red_below: float = DEFAULT_RED_BELOW) -> bool:
    """是否跌破红色阈值（L1 低电告警的判定）。"""
    return remain is not None and remain < red_below


def is_offline(
    label: Any, online_values: Iterable[str] = DEFAULT_ONLINE_VALUES
) -> bool:
    """电表是否离线。

    📌 **缺字段也算离线**（B6）：宁可误报，也不能静默失败 ——
    legacy 曾经因为「字段缺失 → 不判定 → 不告警」让用户以为系统正常。
    """
    if label is None:
        return True
    text = str(label).strip()
    if not text:
        return True
    return text not in tuple(online_values)


def is_stale(gap_seconds: float | None, stale_hours: int) -> bool:
    """距上次成功抓取是否已超过 ``stale_hours``（B7）。

    ``gap_seconds`` 为 ``None``（从未成功过）→ ``False``：
    那不是「陈旧」，是「还没开始」。
    """
    if gap_seconds is None:
        return False
    return gap_seconds >= stale_hours * 3600
