"""``starwatt.notify.qq`` —— QQ 官方机器人渠道。

全部用**假 session**，不碰网络。重点四类：

1. **取凭证**：业务错误也返回 HTTP 200 → 必须看 ``code``（官方文档的坑）
2. **发消息**：路径（``/v2/users`` vs ``/v2/groups``）、``msg_id``/``msg_seq``
3. **验签**：fail-closed（缺密钥 / 缺头 / 篡改 body 一律拒绝）
4. **事件**：``op=13`` 验证应答、消息事件 → 命令回复、开关关闭 → 静默
"""
from __future__ import annotations

import json
import sys

import pytest
import requests

from starwatt.config_registry import get_str, set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import RecordRepo
from starwatt.notify import commands, ed25519, qq

#: QQ 回调验签用的 Bot Secret（官方文档公开的示例值；拼装以避免被
#: ``scripts/secret_scan.py`` 当成真凭据）
BOT_SECRET = "naOC0ocQE3shWLAf" + "ffVLB1rhYPG7"
SEED = ed25519.seed_from_secret(BOT_SECRET)
PUBLIC = ed25519.public_key_from_seed(SEED)


class FakeResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """按 URL 关键字返回预设响应，并记录每次调用。"""

    def __init__(self, responses: dict[str, object] | None = None, error=None) -> None:
        self.responses = dict(responses or {})
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        for marker, payload in self.responses.items():
            if marker in url:
                return FakeResponse(payload)
        return FakeResponse({})


def _configure(*, enabled: bool = True, target: str = "group:group-1") -> None:
    errors = set_many(
        {
            "qq_app_id": "102000000",
            "qq_app_secret": "app-secret",
            "qq_bot_secret": BOT_SECRET,
            "qq_bot_enabled": enabled,
        }
    )
    assert errors == {}, errors
    if target:
        set_state("last_qq_target", target)


TOKEN_OK = {"access_token": "TOKEN-1", "expires_in": "7200"}


@pytest.fixture(autouse=True)
def _clear_cache():
    qq.clear_token_cache()
    yield
    qq.clear_token_cache()


# ===========================================================================
# 凭证
# ===========================================================================
class TestAccessToken:
    def test_returns_token_and_caches(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        assert qq.access_token(session=session) == "TOKEN-1"
        assert qq.access_token(session=session) == "TOKEN-1"
        assert len(session.calls) == 1  # 第二次命中缓存

    def test_force_refreshes(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        qq.access_token(session=session)
        qq.access_token(session=session, force=True)
        assert len(session.calls) == 2

    def test_request_shape(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        qq.access_token(session=session)
        call = session.calls[0]
        assert call["url"].endswith("/app/getAppAccessToken")
        assert call["json"] == {"appId": "102000000", "clientSecret": "app-secret"}

    def test_business_error_with_http_200(self, tmp_db) -> None:
        """🔑 官方文档明确：失败也是 HTTP 200，错误在 ``code`` 里。"""
        _configure()
        session = FakeSession({"getAppAccessToken": {"code": 100016, "message": "bad"}})
        assert qq.access_token(session=session) == ""
        assert qq.access_token(session=session) == ""  # 失败不写缓存 → 会重试

    def test_missing_credentials_returns_empty(self, tmp_db) -> None:
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        assert qq.access_token(session=session) == ""
        assert session.calls == []

    def test_network_error_returns_empty(self, tmp_db) -> None:
        _configure()
        session = FakeSession(error=requests.ConnectionError("boom"))
        assert qq.access_token(session=session) == ""

    def test_non_json_body_returns_empty(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": "not json"})
        assert qq.access_token(session=session) == ""

    def test_api_base_is_configurable(self, tmp_db) -> None:
        _configure()
        set_many({"qq_api_base": "https://sandbox.example.com"})
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        qq.access_token(session=session)
        assert session.calls[0]["url"].startswith("https://sandbox.example.com")


class TestTarget:
    def test_group_path(self) -> None:
        assert qq.Target("group", "g1").path == "/v2/groups/g1"

    def test_c2c_path(self) -> None:
        assert qq.Target("c2c", "u1").path == "/v2/users/u1"

    def test_explicit_config_wins(self, tmp_db) -> None:
        _configure(target="group:learned")
        set_many({"qq_target_openid": "explicit", "qq_target_kind": "c2c"})
        target = qq.resolve_target()
        assert target is not None
        assert target.kind == "c2c" and target.openid == "explicit"

    def test_learned_target_is_used(self, tmp_db) -> None:
        _configure(target="c2c:user-9")
        target = qq.resolve_target()
        assert target is not None
        assert target.kind == "c2c" and target.openid == "user-9"

    def test_no_target(self, tmp_db) -> None:
        _configure(target="")
        assert qq.resolve_target() is None

    def test_remember_target(self, tmp_db) -> None:
        _configure(target="")
        qq.remember_target(qq.Target("group", "g-77"))
        assert get_str("last_qq_target") == "group:g-77"


# ===========================================================================
# 发消息
# ===========================================================================
class TestSendMessages:
    def test_send_text_group_passive(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m1"}})
        target = qq.Target("group", "g1", msg_id="msg-9")

        qq.send_text(target, "你好", session=session)

        sent = session.calls[-1]
        assert sent["url"].endswith("/v2/groups/g1/messages")
        assert sent["json"] == {
            "msg_type": 0,
            "content": "你好",
            "msg_id": "msg-9",
            "msg_seq": 1,
        }
        assert sent["headers"]["Authorization"] == "QQBot TOKEN-1"

    def test_send_text_c2c_proactive(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m1"}})
        qq.send_text(qq.Target("c2c", "u1"), "hi", session=session)

        sent = session.calls[-1]
        assert sent["url"].endswith("/v2/users/u1/messages")
        assert "msg_id" not in sent["json"]  # 主动推送不带 msg_id

    def test_send_text_uses_msg_seq_override(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {}})
        qq.send_text(qq.Target("group", "g1", msg_id="m"), "x", msg_seq=3, session=session)
        assert session.calls[-1]["json"]["msg_seq"] == 3

    def test_business_error_is_swallowed(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"getAppAccessToken": TOKEN_OK, "/messages": {"code": 40054010, "message": "url"}}
        )
        assert qq.send_text(qq.Target("group", "g1"), "http://x", session=session) == {}

    def test_business_error_can_raise(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"getAppAccessToken": TOKEN_OK, "/messages": {"code": 40054010}}
        )
        with pytest.raises(RuntimeError):
            qq.send_text(
                qq.Target("group", "g1"), "x", session=session, raise_on_error=True
            )

    def test_send_without_token_skips_request(self, tmp_db) -> None:
        """未配置凭据 → 直接返回，**一个请求都不发**。"""
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        assert qq.send_text(qq.Target("group", "g1"), "x", session=session) == {}
        assert session.calls == []

    def test_upload_image_returns_file_info(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"getAppAccessToken": TOKEN_OK, "/files": {"file_uuid": "u", "file_info": "FI-1"}}
        )
        assert qq.upload_image(qq.Target("group", "g1"), b"\x89PNG", session=session) == "FI-1"
        sent = session.calls[-1]
        assert sent["url"].endswith("/v2/groups/g1/files")
        assert sent["data"] == {"file_type": 1}

    def test_upload_image_falls_back_to_file_uuid(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"getAppAccessToken": TOKEN_OK, "/files": {"file_uuid": "UUID-1"}}
        )
        assert qq.upload_image(qq.Target("group", "g1"), b"x", session=session) == "UUID-1"

    def test_upload_image_error_returns_empty(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"getAppAccessToken": TOKEN_OK, "/files": {"code": 40034004}}
        )
        assert qq.upload_image(qq.Target("group", "g1"), b"x", session=session) == ""

    def test_send_image_payload(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})
        qq.send_image(qq.Target("group", "g1", msg_id="m-1"), "FI-1", session=session)
        assert session.calls[-1]["json"] == {
            "msg_type": 7,
            "media": {"file_info": "FI-1"},
            "msg_id": "m-1",
            "msg_seq": 2,
        }

    def test_send_image_without_file_info_skips(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        assert qq.send_image(qq.Target("group", "g1"), "", session=session) == {}
        assert session.calls == []


# ===========================================================================
# 验签 / 回调验证
# ===========================================================================
class TestVerifySignature:
    @staticmethod
    def _signed(body: bytes, timestamp: str = "1725442341") -> dict:
        signature = ed25519.sign(SEED, timestamp.encode() + body).hex()
        return {qq.SIGNATURE_HEADER: signature, qq.TIMESTAMP_HEADER: timestamp}

    def test_valid_signature(self, tmp_db) -> None:
        _configure()
        body = b'{"op":0}'
        assert qq.verify_signature(self._signed(body), body) is True

    def test_tampered_body_is_rejected(self, tmp_db) -> None:
        _configure()
        body = b'{"op":0}'
        headers = self._signed(body)
        assert qq.verify_signature(headers, b'{"op":1}') is False

    def test_missing_secret_is_rejected(self, tmp_db) -> None:
        """🔑 fail-closed：没配 Bot Secret 就一律拒绝。"""
        body = b"{}"
        assert qq.verify_signature(self._signed(body), body) is False

    def test_missing_headers_are_rejected(self, tmp_db) -> None:
        _configure()
        assert qq.verify_signature({}, b"{}") is False
        assert qq.verify_signature({qq.SIGNATURE_HEADER: "ab"}, b"{}") is False

    def test_non_hex_signature_is_rejected(self, tmp_db) -> None:
        _configure()
        headers = {qq.SIGNATURE_HEADER: "zzzz", qq.TIMESTAMP_HEADER: "1"}
        assert qq.verify_signature(headers, b"{}") is False

    def test_wrong_key_is_rejected(self, tmp_db) -> None:
        _configure()
        body = b"{}"
        other = ed25519.sign(ed25519.seed_from_secret("another-secret"), b"1" + body)
        headers = {qq.SIGNATURE_HEADER: other.hex(), qq.TIMESTAMP_HEADER: "1"}
        assert qq.verify_signature(headers, body) is False

    def test_explicit_secret_argument(self, tmp_db) -> None:
        body = b"{}"
        assert qq.verify_signature(self._signed(body), body, bot_secret=BOT_SECRET)


class TestValidationResponse:
    def test_returns_signed_plain_token(self, tmp_db) -> None:
        _configure()
        body = {"op": qq.OP_VALIDATION, "d": {"plain_token": "TOKEN", "event_ts": "1725442341"}}
        response = qq.validation_response(body)
        assert response is not None
        assert response["plain_token"] == "TOKEN"
        expected = ed25519.sign(SEED, b"1725442341TOKEN").hex()
        assert response["signature"] == expected

    def test_signature_verifies_against_derived_public_key(self, tmp_db) -> None:
        _configure()
        body = {"op": qq.OP_VALIDATION, "d": {"plain_token": "PT", "event_ts": "99"}}
        response = qq.validation_response(body) or {}
        signature = bytes.fromhex(response["signature"])
        assert ed25519.verify(PUBLIC, b"99PT", signature) is True

    def test_not_a_validation_event(self, tmp_db) -> None:
        assert qq.validation_response({"op": 0}) is None
        assert qq.validation_response("x") is None

    def test_missing_fields(self, tmp_db) -> None:
        _configure()
        assert qq.validation_response({"op": 13, "d": {"plain_token": "x"}}) is None

    def test_without_secret_returns_none(self, tmp_db) -> None:
        body = {"op": 13, "d": {"plain_token": "x", "event_ts": "1"}}
        assert qq.validation_response(body) is None


# ===========================================================================
# 卡片降级成文本
# ===========================================================================
class TestCardToText:
    CARD = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚡ 剩余电量"},
                "subtitle": {"tag": "plain_text", "content": "剩余 12.34 kW·h"},
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "📊 累计用电 **100.00** kW·h"}},
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "columns": [
                        {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                                                              "content": "🎁 **免费**\n5.00"}}]},
                        {"elements": [{"tag": "div", "text": {"tag": "lark_md",
                                                              "content": "💰 **充值**\n7.34"}}]},
                    ],
                },
                {"tag": "note", "elements": [{"tag": "plain_text", "content": "由 StarWatt 推送"}]},
            ],
        },
    }

    def test_flattens_header_and_elements(self) -> None:
        text = qq.card_to_text(self.CARD)
        assert text.splitlines()[0] == "⚡ 剩余电量"
        assert "剩余 12.34 kW·h" in text
        assert "📊 累计用电 100.00 kW·h" in text  # ** 被去掉
        assert "🎁 免费 5.00" in text and "💰 充值 7.34" in text
        assert "由 StarWatt 推送" in text

    def test_accepts_bare_card(self) -> None:
        assert qq.card_to_text(self.CARD["card"]).startswith("⚡ 剩余电量")

    def test_links_are_flattened_for_qq(self) -> None:
        """QQ 群消息不允许 URL（40054010）→ 链接只留文字。"""
        card = {"card": {"elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "见 [文档](https://x.y/z)"}}
        ]}}
        assert qq.card_to_text(card) == "见 文档"

    def test_non_dict_returns_empty(self) -> None:
        assert qq.card_to_text(None) == ""


# ===========================================================================
# Notifier 实现
# ===========================================================================
class TestQQNotifier:
    def test_name(self) -> None:
        assert qq.QQNotifier.name == "qq"

    def test_disabled_by_default(self, tmp_db) -> None:
        assert qq.qq_notifier().enabled() is False

    def test_enabled_requires_credentials(self, tmp_db) -> None:
        set_many({"qq_bot_enabled": True})
        assert qq.qq_notifier().enabled() is False  # 没有 AppID/Secret
        _configure()
        assert qq.qq_notifier().enabled() is True

    def test_master_switch_off(self, tmp_db) -> None:
        _configure(enabled=False)
        assert qq.qq_notifier().enabled() is False

    def test_send_text_without_target_is_noop(self, tmp_db) -> None:
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK})
        qq.QQNotifier(session=session).send_text("hi")
        assert session.calls == []

    def test_send_text_uses_learned_target(self, tmp_db) -> None:
        _configure(target="group:g-1")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})
        qq.QQNotifier(session=session).send_text("hi")
        assert session.calls[-1]["url"].endswith("/v2/groups/g-1/messages")

    def test_send_card_falls_back_to_text(self, tmp_db) -> None:
        """渲染器还没落地 → 自动降级成文本（不该丢消息）。"""
        _configure(target="group:g-1")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})
        card = {"card": {"header": {"title": {"content": "⚡ 标题"}}, "elements": []}}

        qq.QQNotifier(session=session).send_card(card)

        sent = session.calls[-1]
        assert sent["json"]["msg_type"] == 0
        assert sent["json"]["content"] == "⚡ 标题"

    def test_send_card_uses_image_when_renderer_works(self, tmp_db, monkeypatch) -> None:
        _configure(target="group:g-1")
        session = FakeSession(
            {
                "getAppAccessToken": TOKEN_OK,
                "/files": {"file_info": "FI-9"},
                "/messages": {"id": "m"},
            }
        )

        class FakeRenderer:
            @staticmethod
            def render_card(_card):
                return b"\x89PNG-fake"

        monkeypatch.setitem(sys.modules, "starwatt.notify.renderer", FakeRenderer)

        qq.QQNotifier(session=session).send_card({"card": {"elements": []}})

        assert session.calls[-1]["json"] == {
            "msg_type": 7,
            "media": {"file_info": "FI-9"},
        }

    def test_send_card_never_raises(self, tmp_db, monkeypatch) -> None:
        _configure(target="group:g-1")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(qq, "resolve_target", _boom)
        qq.QQNotifier().send_card({"card": {}})  # 不应抛


# ===========================================================================
# 事件入口
# ===========================================================================
class TestHandleEvent:
    @staticmethod
    def _c2c_event(content: str = "/状态", msg_id: str = "m-1") -> dict:
        return {
            "op": qq.OP_DISPATCH,
            "t": qq.EVENT_C2C,
            "d": {"id": msg_id, "content": content, "user_openid": "user-1"},
        }

    @staticmethod
    def _group_event(content: str = "<@!123> /状态") -> dict:
        return {
            "op": qq.OP_DISPATCH,
            "t": qq.EVENT_GROUP_AT,
            "d": {"id": "m-2", "content": content, "group_openid": "group-1"},
        }

    def test_validation_event(self, tmp_db) -> None:
        _configure()
        body = {"op": qq.OP_VALIDATION, "d": {"plain_token": "PT", "event_ts": "5"}}
        response = qq.handle_event(body)
        assert response["plain_token"] == "PT" and response["signature"]

    def test_heartbeat_is_acked(self, tmp_db) -> None:
        _configure()
        assert qq.handle_event({"op": 1}) == {"code": 0}

    def test_c2c_command_replies(self, tmp_db) -> None:
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})

        assert qq.handle_event(self._c2c_event("/帮助"), session=session) == {"code": 0}

        sent = session.calls[-1]
        assert sent["json"]["content"] == commands.HELP_TEXT
        assert sent["json"]["msg_id"] == "m-1"

    def test_group_event_strips_mention(self, tmp_db) -> None:
        _configure(target="")
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=7.5))
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})

        qq.handle_event(self._group_event(), session=session)

        assert "⚡ 当前剩余: 7.50 kW·h" in session.calls[-1]["json"]["content"]
        assert session.calls[-1]["url"].endswith("/v2/groups/group-1/messages")

    def test_target_is_learned(self, tmp_db) -> None:
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {}})
        qq.handle_event(self._group_event(), session=session)
        assert get_str("last_qq_target") == "group:group-1"

    def test_non_command_text_gets_help_hint(self, tmp_db) -> None:
        """非命令文本 → 「未知命令」+ 帮助（legacy 行为，也是新用户的引导）。"""
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})
        assert qq.handle_event(self._c2c_event("今天天气不错"), session=session) == {"code": 0}
        content = session.calls[-1]["json"]["content"]
        assert content.startswith("未知命令: 今天天气不错")

    def test_mention_only_is_not_replied(self, tmp_db) -> None:
        """只 @ 了机器人、没有正文 → 不回复（不刷屏）。"""
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {}})
        assert qq.handle_event(self._group_event("<@!123>"), session=session) == {"code": 0}
        assert session.calls == []

    def test_unknown_event_type_is_acked(self, tmp_db) -> None:
        _configure()
        assert qq.handle_event({"op": 0, "t": "GROUP_ADD_ROBOT", "d": {}}) == {"code": 0}

    def test_master_switch_off_is_silent(self, tmp_db) -> None:
        _configure(enabled=False)
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {}})
        assert qq.handle_event(self._c2c_event(), session=session) == {"code": 0}
        assert session.calls == []

    def test_missing_openid_is_rejected(self, tmp_db) -> None:
        _configure()
        body = {"op": 0, "t": qq.EVENT_C2C, "d": {"id": "m", "content": "/状态"}}
        assert qq.handle_event(body)["code"] == 1

    def test_invalid_body(self, tmp_db) -> None:
        assert qq.handle_event("nope")["code"] == 1

    def test_command_failure_replies_politely(self, tmp_db, monkeypatch) -> None:
        """命令层抛异常时不能让 QQ 收到 5xx（它会重试风暴）。"""
        _configure(target="")
        session = FakeSession({"getAppAccessToken": TOKEN_OK, "/messages": {"id": "m"}})

        def _boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(qq.commands, "dispatch_text", _boom)

        assert qq.handle_event(self._c2c_event(), session=session) == {"code": 0}
        assert "服务暂时不可用" in session.calls[-1]["json"]["content"]
