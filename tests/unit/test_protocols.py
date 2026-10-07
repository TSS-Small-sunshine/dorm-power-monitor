"""``starwatt.protocols`` 扩展点协议测试（Q18）。

协议是「结构化的」—— 不做 ``issubclass`` 检查（带数据成员的 Protocol
在 ``issubclass`` 上会抛 ``TypeError``），而是验证：

1. 协议**声明**了预期的成员
2. 一个符合协议的实现（test double）能**满足**调用方所需的方法
"""
from __future__ import annotations

from typing import Any

from starwatt.protocols import Endpoint, FetchContext, Notifier


# ---------------------------------------------------------------------------
# Test doubles —— 同时也是「按 EXTENDING.md 加一个渠道」的最小样例
# ---------------------------------------------------------------------------
class FakeNotifier:
    """符合 ``Notifier`` 的最小实现（记录调用，不发网络请求）。"""

    name = "fake"

    def __init__(self, enabled: bool = True) -> None:
        self._enabled = enabled
        self.sent: list[tuple[str, Any]] = []

    def enabled(self) -> bool:
        return self._enabled

    def send_card(self, card: dict[str, Any]) -> None:
        self.sent.append(("card", card))

    def send_text(self, text: str) -> None:
        self.sent.append(("text", text))


class FakeEndpoint:
    """符合 ``Endpoint`` 的最小实现。"""

    key = "F9"
    interval_sec: int | None = None

    def __init__(self) -> None:
        self.persisted: list[dict[str, Any]] | None = None
        self.calls = 0

    def fetch(self, ctx: FetchContext) -> list[dict[str, Any]]:
        self.calls += 1
        return [{"roomId": ctx.room_id, "n": self.calls}]

    def persist(self, rows: list[dict[str, Any]]) -> None:
        self.persisted = rows


# ---------------------------------------------------------------------------
# 协议声明
# ---------------------------------------------------------------------------
class TestNotifierProtocol:
    def test_declares_three_methods(self) -> None:
        for method in ("enabled", "send_card", "send_text"):
            assert callable(getattr(Notifier, method, None)), f"Notifier 缺 {method}"

    def test_declares_name_attribute(self) -> None:
        assert "name" in getattr(Notifier, "__annotations__", {})


class TestEndpointProtocol:
    def test_declares_fetch_and_persist(self) -> None:
        for method in ("fetch", "persist"):
            assert callable(getattr(Endpoint, method, None)), f"Endpoint 缺 {method}"

    def test_declares_key_and_interval(self) -> None:
        annotations = getattr(Endpoint, "__annotations__", {})
        assert "key" in annotations
        assert "interval_sec" in annotations


class TestFetchContextProtocol:
    def test_declares_openid_room_and_transport(self) -> None:
        annotations = getattr(FetchContext, "__annotations__", {})
        assert "openid" in annotations
        assert "room_id" in annotations
        for method in ("get", "post_form"):
            assert callable(getattr(FetchContext, method, None)), f"FetchContext 缺 {method}"


# ---------------------------------------------------------------------------
# 实现满足协议（duck typing）
# ---------------------------------------------------------------------------
class TestDoublesSatisfyProtocols:
    def test_fake_notifier_behaves_as_expected(self) -> None:
        n = FakeNotifier()
        assert n.name == "fake"
        assert n.enabled() is True
        n.send_card({"msg_type": "interactive"})
        n.send_text("hi")
        assert n.sent == [("card", {"msg_type": "interactive"}), ("text", "hi")]

    def test_fake_notifier_can_be_disabled(self) -> None:
        assert FakeNotifier(enabled=False).enabled() is False

    def test_fake_endpoint_fetches_and_persists(self) -> None:
        ep = FakeEndpoint()
        ctx = _Ctx(openid="oid", room_id="room-1")
        rows = ep.fetch(ctx)
        assert rows == [{"roomId": "room-1", "n": 1}]
        ep.persist(rows)
        assert ep.persisted == rows

    def test_endpoint_interval_none_means_every_scrape(self) -> None:
        assert FakeEndpoint().interval_sec is None

    def test_doubles_have_protocol_members(self) -> None:
        """结构化校验：实现方具备协议要求的全部成员。"""
        n = FakeNotifier()
        for attr in ("name", "enabled", "send_card", "send_text"):
            assert hasattr(n, attr), f"FakeNotifier 缺 {attr}"

        ep = FakeEndpoint()
        for attr in ("key", "interval_sec", "fetch", "persist"):
            assert hasattr(ep, attr), f"FakeEndpoint 缺 {attr}"


class _Ctx:
    """最小 ``FetchContext`` 实现。"""

    def __init__(self, openid: str, room_id: str) -> None:
        self.openid = openid
        self.room_id = room_id

    def get(self, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def post_form(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
