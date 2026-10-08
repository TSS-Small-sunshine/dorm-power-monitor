"""``starwatt.domain`` —— **纯计算层**（零 IO，R1 守卫强制）。

为什么单独一层
==============

``scripts/ast_guard.py`` 的 R1 规则禁止本包 import ``requests`` / ``flask`` /
``sqlite3`` / ``starwatt.db`` / ``starwatt.config_registry``。这不是洁癖：

* 纯函数**可以 100% 单测**（不需要 DB、不需要假网络、不需要冻结时间）
* 抓取层、Web 层、通知层**共用同一套算法**，不会出现「三份真相」

三个模块
========

========================  ==================================================
``metrics.py``            日均 / 剩余天数 / 累计差值 / 充值合计
``thresholds.py``         低电颜色分区 / 离线判定 / stale 判定
``pricing.py``            单价回退链 / 金额换算
========================  ==================================================
"""
from __future__ import annotations

from starwatt.domain.metrics import (
    MAX_DAILY_KWH,
    avg_daily_from_cumulative,
    days_remaining,
    sort_usable,
    span_delta,
    step_delta,
    sum_recharges,
)
from starwatt.domain.pricing import DEFAULT_EQPRICE, money, parse_price
from starwatt.domain.thresholds import (
    DEFAULT_ONLINE_VALUES,
    color_zone,
    is_low,
    is_offline,
    is_stale,
)

__all__ = [
    "DEFAULT_EQPRICE",
    "DEFAULT_ONLINE_VALUES",
    "MAX_DAILY_KWH",
    "avg_daily_from_cumulative",
    "color_zone",
    "days_remaining",
    "is_low",
    "is_offline",
    "is_stale",
    "money",
    "parse_price",
    "sort_usable",
    "span_delta",
    "step_delta",
    "sum_recharges",
]
