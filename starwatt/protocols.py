"""扩展点协议 —— ``Notifier`` / ``Endpoint``（Q18）。

设计目标
========

加新功能时改**几个文件、写几行**，而不是散弹式改 5 处：

| 想做什么 | 步骤 | 涉及文件 |
|---|---|---|
| 加通知渠道（微信 / 邮件 / Telegram） | 实现 ``Notifier`` + 注册 | 2 |
| 适配其他学校 | 实现 ``Endpoint`` 集合 | 2 |
| 加告警层 | ``policies`` + ``flags`` + ``registry`` | 3 |

本模块**只放协议定义**，不含任何实现，也不 import 任何子模块 —— 保证
``starwatt.protocols`` 可以被任意层安全导入（含 ``starwatt/domain/``）。

关于 ``runtime_checkable``
==========================

刻意**不加** ``@runtime_checkable``：两个协议都带数据成员
（``name`` / ``key`` / ``interval_sec``），而带数据成员的 Protocol 在
``issubclass()`` 上会抛 ``TypeError``。测试改用「方法存在性 + 可调用」检查
（见 ``tests/unit/test_protocols.py``）。
"""
from __future__ import annotations

from typing import Any, Protocol

__all__ = ["Endpoint", "FetchContext", "Notifier"]


class FetchContext(Protocol):
    """抓取上下文 —— ``Endpoint.fetch()`` 的入参。

    刻意做成协议而非具体类，这样 ``starwatt/scraper`` 可以自由实现它，
    而 ``starwatt/domain`` 与测试替身都不需要依赖真实 HTTP 会话。
    """

    openid: str
    room_id: str

    def get(self, path: str, **kwargs: Any) -> Any: ...
    def post_form(self, path: str, data: dict[str, Any]) -> Any: ...


class Notifier(Protocol):
    """通知渠道协议。

    实现方负责：渠道凭据、请求签名、错误处理。
    调用方（``starwatt.notify.transport`` / ``policies``）只认这三个方法。

    约定
    ----

    * ``enabled()`` 必须**快速返回**（读缓存，不发网络请求）
    * ``send_*()`` 被调用时渠道已确认启用；实现方**不得**再抛
      「渠道未配置」类异常 —— 那是 ``enabled()`` 的职责
    * ``send_*()`` 失败时记录 ``ERROR`` 日志并返回，**不向上抛**
      （单点失败不得影响抓取主流程，K7）
    """

    #: 渠道标识，如 ``"feishu_group"`` / ``"feishu_bot"`` / ``"wecom"``
    name: str

    def enabled(self) -> bool:
        """该渠道是否已启用（总开关 + 凭据齐备）。"""
        ...

    def send_card(self, card: dict[str, Any]) -> None:
        """发送一张交互卡片。"""
        ...

    def send_text(self, text: str) -> None:
        """发送纯文本。"""
        ...


class Endpoint(Protocol):
    """学校数据源协议。

    旧代码把 F1–F5 的节流、请求、解析、落库全塞在 ``dorm_power.py`` 里；
    重写后每个数据源是一个 ``Endpoint`` 实现，节流由 ``interval_sec`` 声明。

    约定
    ----

    * ``interval_sec is None`` → 每次抓取都跑（F1 / F4）
    * ``interval_sec = 86400`` → 24 小时节流（F2 / F5）
    * ``interval_sec = 3600`` → 1 小时节流（F3）
    * ``fetch()`` 只负责**请求 + 解析**，返回规范化后的行列表；
      **不得**直接写库、不得发通知
    * ``persist()`` 只负责落库，**不得**发网络请求
    """

    #: 数据源标识，如 ``"F1"`` / ``"F2"`` … ``"F5"``
    key: str

    #: 节流间隔（秒）；``None`` = 每次抓取
    interval_sec: int | None

    def fetch(self, ctx: FetchContext) -> list[dict[str, Any]]:
        """请求并解析，返回规范化后的行。"""
        ...

    def persist(self, rows: list[dict[str, Any]]) -> None:
        """把 ``fetch()`` 的结果落库。"""
        ...
