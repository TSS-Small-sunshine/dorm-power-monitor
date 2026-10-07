"""全项目唯一时间源 —— 显式锁定 ``Asia/Shanghai``（Q10）。

为什么需要本模块
================

旧代码全栈依赖 ``datetime.now()``（跟随**服务器时区**），而
``deploy/dorm-web.service`` / ``dorm-cron.txt`` **都没有设置 TZ**。
你的服务器恰好是 CST 所以能跑；但「每个宿舍自己搭建」时，阿里云/腾讯云的
VPS 默认经常是 UTC —— 会导致：

* L3 日报在北京时间 17:00 推送（而不是 09:00）
* 卡片与前端时间戳全部错 8 小时

本模块把「现在」**显式钉在 Asia/Shanghai**，与服务器时区解耦。

约定（与旧数据 100% 兼容）
==========================

* 返回**朴素** datetime（``tzinfo=None``）
* 写库格式 ``YYYY-MM-DD HH:MM:SS``
* 因此 **零数据迁移** —— 现有 ``records.db`` 的字符串比较全部继续有效

强制
====

``scripts/ast_guard.py`` 的 R7 规则禁止 ``starwatt/**`` 裸用
``datetime.now()`` / ``datetime.today()`` / ``date.today()``，
**唯一例外就是本文件**。

允许的写法（显式带时区，R7 放行）::

    datetime.now(ZoneInfo("Asia/Shanghai"))

禁止的写法::

    datetime.now()      # ← 跟随服务器时区
    date.today()        # ← 同上
"""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

#: 项目唯一的时区对象
CST = ZoneInfo("Asia/Shanghai")

#: 写库格式（与旧数据一致，勿改）
STAMP_FMT = "%Y-%m-%d %H:%M:%S"

__all__ = [
    "CST",
    "STAMP_FMT",
    "now_cst",
    "parse_stamp",
    "stamp",
    "to_stamp",
    "today_cst",
]


def now_cst() -> datetime:
    """当前**朴素北京时间**（``tzinfo=None``），与服务器时区无关。"""
    return datetime.now(CST).replace(tzinfo=None)


def today_cst() -> date:
    """当前北京日期。"""
    return now_cst().date()


def stamp() -> str:
    """当前时间的写库格式字符串（``YYYY-MM-DD HH:MM:SS``）。"""
    return now_cst().strftime(STAMP_FMT)


def to_stamp(moment: datetime) -> str:
    """把任意 datetime 转成写库格式。"""
    return moment.strftime(STAMP_FMT)


def parse_stamp(raw: object) -> datetime | None:
    """解析写库格式字符串；无法解析时返回 ``None``（不抛异常）。

    旧代码里散落着 ``try: datetime.fromisoformat(x) except ValueError``，
    统一收敛到这里。
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, STAMP_FMT)
    except ValueError:
        return None
