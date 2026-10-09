"""多渠道注册表 —— 「加一个渠道」只需要在这里加一行（Q18 扩展点）。

现状
====

========================  ==================================================
渠道名                     实现
========================  ==================================================
``feishu_group``           :class:`starwatt.notify.transport.WebhookNotifier`
                           （飞书群自定义机器人 webhook）
``qq``                     :class:`starwatt.notify.qq.QQNotifier`
                           （QQ 官方机器人 OpenAPI）
========================  ==================================================

用法
====

::

    from starwatt.notify.channels import enabled_channels, broadcast_card

    broadcast_card(card)          # 发给所有**已启用**的渠道
    [n.name for n in enabled_channels()]

约定（与 ``protocols.Notifier`` 一致）
====================================

* ``enabled()`` 读缓存、不发网络请求；未配置凭据 / 开关关闭 → ``False``
* ``send_*()`` **不向上抛**：单点失败只记 ERROR，绝不影响抓取主流程（K7）
* 每个渠道自己决定「卡片怎么降级」（飞书发卡片；QQ 渲染 PNG，失败退文本）

⚠️ 飞书**群**推送的开关体系（``push_group_enabled`` + 各层开关 + 静默时段）
仍由 :func:`starwatt.flags.should_push` 负责 —— 本模块只做「其它渠道的扇出」，
不参与那套层判定。
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from starwatt.notify import qq, transport
from starwatt.protocols import Notifier

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "CHANNELS",
    "broadcast_card",
    "broadcast_text",
    "enabled_channels",
    "notifier_for",
]

#: 渠道名 → 工厂（工厂返回实现 ``Notifier`` 协议的对象）
CHANNELS: dict[str, Callable[[], Notifier]] = {
    transport.WebhookNotifier.name: transport.webhook_notifier,
    qq.QQNotifier.name: qq.qq_notifier,
}


def notifier_for(name: str) -> Notifier | None:
    """按名字取渠道（未注册返回 ``None``）。"""
    factory = CHANNELS.get(name)
    return factory() if factory else None


def enabled_channels() -> list[Notifier]:
    """所有**已启用**的渠道（``enabled()`` 内部已读缓存）。"""
    channels: list[Notifier] = []
    for name, factory in CHANNELS.items():
        try:
            notifier = factory()
            if notifier.enabled():
                channels.append(notifier)
        except Exception:  # noqa: BLE001 —— 单个渠道的配置坏了不该拖垮其它渠道
            logger.warning("渠道 %s 初始化失败（已跳过）", name, exc_info=True)
    return channels


def broadcast_card(card: dict, *, exclude: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """把卡片广播给所有已启用渠道（可排除某些渠道），返回**成功的渠道名**。

    渠道之间的失败互相隔离：QQ 挂了不影响飞书（反之亦然）。
    """
    sent: list[str] = []
    for notifier in enabled_channels():
        if notifier.name in exclude:
            continue
        try:
            notifier.send_card(card)
            sent.append(notifier.name)
        except Exception:  # noqa: BLE001 —— 协议要求不抛，这里再兜一层
            logger.exception("渠道 %s 推送卡片失败（已忽略）", notifier.name)
    return sent


def broadcast_text(text: str, *, exclude: frozenset[str] | set[str] = frozenset()) -> list[str]:
    """把纯文本广播给所有已启用渠道。"""
    sent: list[str] = []
    for notifier in enabled_channels():
        if notifier.name in exclude:
            continue
        try:
            notifier.send_text(text)
            sent.append(notifier.name)
        except Exception:  # noqa: BLE001
            logger.exception("渠道 %s 推送文本失败（已忽略）", notifier.name)
    return sent
