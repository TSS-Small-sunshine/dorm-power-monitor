"""单价与金额 —— **纯函数**（零 IO，R1 守卫强制）。

单价回退链（唯一实现）
======================

legacy 有**两份**单价读取（``dorm_power._read_eqprice`` 与
``web._read_eqprice``），Round 42 才把它们对齐。重写只留这一份：

::

    1. 配置中心 eqprice      （用户在 WebUI 里填的）
    2. 环境变量 DORM_EQPRICE （运维覆盖）
    3. 0.5 元/度             （默认值 —— 与 legacy 的硬默认一致）

:func:`parse_price` 负责「把字符串解析成数字」，**读哪个源**由调用方决定
（domain 层不能碰配置与环境变量）。
"""
from __future__ import annotations

__all__ = ["DEFAULT_EQPRICE", "money", "parse_price"]

#: 默认电价（元/度）—— 与 legacy 的硬默认一致
DEFAULT_EQPRICE = 0.5


def parse_price(*sources: object, default: float = DEFAULT_EQPRICE) -> float:
    """按顺序取第一个可解析的单价；全都不可解析 → ``default``。

    Args:
        *sources: 候选来源（配置值、环境变量、字符串……），**顺序即优先级**。
        default: 兜底值。

    Returns:
        保留 4 位小数的单价（与 legacy 的 ``round(float(raw), 4)`` 一致）。
    """
    for raw in sources:
        if raw is None:
            continue
        text = str(raw).strip()
        if not text:
            continue
        try:
            return round(float(text), 4)
        except (TypeError, ValueError):
            continue
    return default


def money(kwh: float | None, price: float | None) -> float | None:
    """电量 × 单价 → 金额（元，2 位小数）。

    任一参数缺失 / 非法 → ``None``（前端渲染「—」，而不是骗人的 ``0.00``）。
    """
    if kwh is None or price is None:
        return None
    try:
        return round(float(kwh) * float(price), 2)
    except (TypeError, ValueError):
        return None
