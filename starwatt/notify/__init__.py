"""``starwatt.notify`` —— 通知层（M3）。

M3 进度
=======

* ✅ ``crypto.py`` —— 飞书事件解密（3 条路径）+ 签名校验（**fail-closed**）
* ✅ ``card_builder.py`` —— L1 / L2 / L4 卡片（与 ``cards.json`` 快照逐字节一致）
* ✅ ``transport.py`` —— **唯一**推送出口（webhook 签名 + ``Notifier`` 实现）
* ✅ ``policies.py`` —— 告警判定 / 冷却 / 幂等打点 / L3 节奏
* ✅ ``reports.py`` —— L3 报表内容（日报 / 周报 / 月报摘要）
* ✅ ``commands.py`` —— 9 个命令 + 菜单（**渠道无关**）
* ✅ ``ed25519.py`` —— 纯 Python Ed25519（QQ 回调签名，零新增依赖）
* ✅ ``qq.py`` —— **QQ 官方机器人**渠道（OpenAPI + 事件 + 验签）
* ✅ ``channels.py`` —— 多渠道注册表 + 广播（Q18 扩展点）
* ✅ ``renderer.py`` —— PNG 卡片（Pillow + MiSans + NotoEmoji 混排）
* ✅ ``dispatcher.py`` —— 飞书私聊（解密 / 验签 / 9 命令 / 菜单 / 图片回复）

五条推送路径的落点（Q16 的「关闭即静默降级」）
=============================================

============  ==============================  ==========================
层             触发                            入口
============  ==============================  ==========================
L1 低电        remain 跌破红色阈值              ``policies.push_low_battery_if_due``
L2 摘要        每次抓取成功                     ``policies.push_summary``
L3 日报/周报   到点（HH:MM）+ 未推过            ``policies.l3_due`` + 报表内容
L4 离线        电表状态不在在线白名单           ``policies.push_offline_if_due``
stale          距上次成功 > ``stale_hours``     ``policies.push_stale_if_due``
违规           F3 有新记录 + 过冷却             ``policies.push_violations_if_due``
============  ==============================  ==========================

导入顺序（**不要调整**）
========================

``policies`` 依赖 ``card_builder`` 与 ``transport``，而 Python 在导入
``starwatt.notify.policies`` 时会先执行本文件 —— 所以这里必须按
「crypto → card_builder → transport → policies → reports → commands →
ed25519 → qq → channels → renderer → dispatcher」的顺序导入，
否则 ``from starwatt.notify import card_builder`` 会拿到半成品包对象。
"""
from __future__ import annotations

from starwatt.notify import (
    card_builder,
    channels,
    commands,
    crypto,
    dispatcher,
    ed25519,
    policies,
    qq,
    renderer,
    reports,
    transport,
)
from starwatt.notify.card_builder import (
    build_l3_card,
    build_low_battery_card,
    build_offline_card,
    build_stale_card,
    build_summary_card,
    build_violation_card,
    template_for_remain,
)
from starwatt.notify.crypto import (
    CryptoError,
    decrypt_payload,
    is_encrypted,
    verify_lark_signature,
    verify_token,
)
from starwatt.notify.policies import (
    is_offline,
    l3_due,
    push_l3_if_due,
    push_low_battery_if_due,
    push_offline_if_due,
    push_stale_if_due,
    push_summary,
    push_violations_if_due,
    stamp_l3,
)
from starwatt.notify.reports import (
    build_summary,
    daily_summary,
    monthly_summary,
    weekly_summary,
)
from starwatt.notify.transport import (
    WebhookNotifier,
    post_card,
    post_text,
    sign,
    webhook_notifier,
)

__all__ = [
    "CryptoError",
    "WebhookNotifier",
    "build_l3_card",
    "build_low_battery_card",
    "build_offline_card",
    "build_stale_card",
    "build_summary",
    "build_summary_card",
    "build_violation_card",
    "card_builder",
    "card_to_text",
    "channels",
    "commands",
    "crypto",
    "daily_summary",
    "decrypt_payload",
    "dispatcher",
    "ed25519",
    "is_encrypted",
    "is_offline",
    "l3_due",
    "monthly_summary",
    "policies",
    "post_card",
    "post_text",
    "push_l3_if_due",
    "push_low_battery_if_due",
    "push_offline_if_due",
    "push_stale_if_due",
    "push_summary",
    "push_violations_if_due",
    "qq",
    "renderer",
    "reports",
    "sign",
    "stamp_l3",
    "template_for_remain",
    "transport",
    "verify_lark_signature",
    "verify_token",
    "webhook_notifier",
    "weekly_summary",
]
