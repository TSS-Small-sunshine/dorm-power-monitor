"""``starwatt.notify.channels`` —— 多渠道注册表 + 广播（Q18 扩展点）。

「加一个渠道 = 实现 Notifier + 注册 + 加凭据项」这条 SOP 是否真的成立，
由这些用例来证明：注册、开关、隔离、扇出，一个都不能少。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from starwatt.config_registry import set_many, set_state
from starwatt.notify import channels, policies, transport

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/x"
CARD = {"msg_type": "interactive", "card": {"header": {}, "elements": []}}
NOW = datetime(2026, 10, 6, 12, 0, 0)


class FakeNotifier:
    """符合 ``Notifier`` 协议的最小替身。"""

    name = "qq"

    def __init__(self, enabled: bool = True, error: Exception | None = None) -> None:
        self._enabled = enabled
        self._error = error
        self.cards: list[dict] = []
        self.texts: list[str] = []

    def enabled(self) -> bool:
        return self._enabled

    def send_card(self, card: dict) -> None:
        if self._error is not None:
            raise self._error
        self.cards.append(card)

    def send_text(self, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.texts.append(text)


@pytest.fixture
def fake_qq(monkeypatch):
    """把 ``qq`` 渠道换成替身（避免任何网络与凭据依赖）。"""
    notifier = FakeNotifier()
    monkeypatch.setitem(channels.CHANNELS, "qq", lambda: notifier)
    return notifier


class TestRegistry:
    def test_both_channels_are_registered(self) -> None:
        assert set(channels.CHANNELS) == {"feishu_group", "qq"}

    def test_notifier_for(self) -> None:
        assert isinstance(channels.notifier_for("feishu_group"), transport.WebhookNotifier)
        assert channels.notifier_for("nope") is None

    def test_nothing_enabled_by_default(self, tmp_db) -> None:
        """默认：没配 webhook、QQ 总开关关着 → 一个渠道都不启用。"""
        assert channels.enabled_channels() == []

    def test_feishu_enabled_with_webhook(self, tmp_db) -> None:
        set_many({"feishu_webhook_url": WEBHOOK})
        names = [n.name for n in channels.enabled_channels()]
        assert names == ["feishu_group"]

    def test_qq_enabled_with_credentials_and_flag(self, tmp_db) -> None:
        set_many(
            {
                "qq_app_id": "102000000",
                "qq_app_secret": "s",
                "qq_bot_secret": "b",
                "qq_bot_enabled": True,
            }
        )
        assert [n.name for n in channels.enabled_channels()] == ["qq"]

    def test_broken_channel_is_skipped(self, tmp_db, monkeypatch) -> None:
        """某个渠道的配置坏了，不该拖垮其它渠道。"""

        def _boom():
            raise RuntimeError("bad config")

        monkeypatch.setitem(channels.CHANNELS, "qq", _boom)
        set_many({"feishu_webhook_url": WEBHOOK})
        assert [n.name for n in channels.enabled_channels()] == ["feishu_group"]


class TestBroadcast:
    def test_broadcast_card(self, tmp_db, fake_qq, monkeypatch) -> None:
        set_many({"feishu_webhook_url": WEBHOOK})
        monkeypatch.setattr(transport, "post_card", lambda *_a, **_k: True)

        sent = channels.broadcast_card(CARD)

        assert sent == ["feishu_group", "qq"]
        assert fake_qq.cards == [CARD]

    def test_broadcast_can_exclude(self, tmp_db, fake_qq) -> None:
        assert channels.broadcast_card(CARD, exclude={"feishu_group"}) == ["qq"]

    def test_broadcast_text(self, tmp_db, fake_qq) -> None:
        assert channels.broadcast_text("你好") == ["qq"]
        assert fake_qq.texts == ["你好"]

    def test_one_channel_failure_does_not_stop_the_other(
        self, tmp_db, monkeypatch
    ) -> None:
        monkeypatch.setitem(
            channels.CHANNELS, "qq", lambda: FakeNotifier(error=RuntimeError("qq down"))
        )
        good = FakeNotifier()
        good.name = "feishu_group"
        monkeypatch.setitem(channels.CHANNELS, "feishu_group", lambda: good)

        assert channels.broadcast_card(CARD) == ["feishu_group"]
        assert good.cards == [CARD]


class TestPoliciesFanOut:
    """``policies._send`` 的扇出语义：层闸门管「发不发」，渠道管「发到哪」。"""

    def test_qq_only_delivery_counts_as_sent(self, tmp_db, fake_qq) -> None:
        """🔑 飞书没配 webhook、但 QQ 配好了 → 算发出去了 → 才会打点。"""
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")

        assert policies.push_stale_if_due(now=NOW) is True
        assert fake_qq.cards and len(fake_qq.cards) == 1
        assert policies.STATE_KEYS["stale"] and _state(policies.STATE_KEYS["stale"])

    def test_no_channel_means_not_sent(self, tmp_db, monkeypatch) -> None:
        """一个渠道都没启用 → 返回 False → 不打点（等配好之后补发）。"""
        monkeypatch.setitem(channels.CHANNELS, "qq", lambda: FakeNotifier(enabled=False))
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")

        assert policies.push_stale_if_due(now=NOW) is False
        assert _state(policies.STATE_KEYS["stale"]) == ""

    def test_layer_gate_suppresses_every_channel(self, tmp_db, fake_qq) -> None:
        """层开关关闭 → 飞书和 QQ 都不发（内容层语义）。"""
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")
        set_many({"push_stale_enable": False})

        assert policies.push_stale_if_due(now=NOW) is False
        assert fake_qq.cards == []

    def test_fanout_failure_does_not_break_feishu(self, tmp_db, monkeypatch) -> None:
        monkeypatch.setitem(
            channels.CHANNELS,
            "qq",
            lambda: FakeNotifier(error=RuntimeError("qq down")),
        )
        monkeypatch.setattr(transport, "post_card", lambda *_a, **_k: True)
        set_state(policies.STATE_KEYS["scrape"], "2026-10-06 09:00:00")

        assert policies.push_stale_if_due(now=NOW) is True


def _state(key: str) -> str:
    from starwatt.config_registry import get_str

    return get_str(key, "")
