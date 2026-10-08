"""``starwatt.notify.dispatcher`` —— 飞书私聊（事件订阅）。

全部用**假 session**，不碰网络。重点：

1. **fail-closed 验签** + 加密事件解密（解不开也不能 5xx）
2. **三级降级**：图片卡片 → 文本卡片 → 纯文本（legacy Round 16 的教训）
3. **v2 事件形状**：文本在 ``message.content``（JSON 字符串）里，不是顶层字段
"""
from __future__ import annotations

import hashlib
import json

import pytest
import requests
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from starwatt.config_registry import set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import RecordRepo
from starwatt.notify import commands, crypto, dispatcher, transport

ENCRYPT_KEY = "a" * 32
OPEN_ID = "ou_test_user"


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
    """按 URL 关键字返回预设响应，并记录调用（``fail`` 里的关键字会抛错）。"""

    def __init__(self, responses: dict | None = None, fail: tuple[str, ...] = ()) -> None:
        self.responses = dict(responses or {})
        self.fail = fail
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        for marker in self.fail:
            if marker in url:
                raise requests.ConnectionError(f"{marker} down")
        for marker, payload in self.responses.items():
            if marker in url:
                return FakeResponse(payload)
        return FakeResponse({"code": 0, "data": {}})

    # -- 断言辅助 ---------------------------------------------------------
    @property
    def sent(self) -> list[dict]:
        """所有发消息的请求体。"""
        return [c["json"] for c in self.calls if "/im/v1/messages" in c["url"]]

    @property
    def uploads(self) -> int:
        return sum(1 for c in self.calls if "/im/v1/images" in c["url"])


TOKEN_OK = {"code": 0, "tenant_access_token": "T-1", "expire": 7200}


def _configure(**overrides) -> None:
    payload = {
        "feishu_app_id": "cli_test",
        "feishu_app_secret": "secret",
        "push_bot_enabled": True,
        "quiet_hours_start": "00:00",
        "quiet_hours_end": "00:00",
    }
    payload.update(overrides)
    errors = set_many(payload)
    assert errors == {}, errors


def _encrypted_body(payload: dict) -> dict:
    """按飞书官方规范加密（与 ``test_notify_crypto`` 同一套）。"""
    digest = hashlib.sha256(ENCRYPT_KEY.encode()).digest()
    plain = b"\x00" * 16 + json.dumps(payload).encode()
    cipher = AES.new(digest, AES.MODE_CBC, digest[:16])
    import base64

    return {"encrypt": base64.b64encode(cipher.encrypt(pad(plain, 16))).decode()}


def _message_event(text: str = "/状态", *, message_type: str = "text") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": dispatcher.MESSAGE_EVENT},
        "event": {
            "sender": {"sender_id": {"open_id": OPEN_ID}},
            "message": {
                "message_type": message_type,
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


def _menu_event(key: str = "menu_remain") -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": dispatcher.MENU_EVENT},
        "event": {"event_key": key, "sender": {"sender_id": {"open_id": OPEN_ID}}},
    }


def _body_of(sent: dict) -> str:
    """从 ``text`` / ``interactive`` 两种消息里取出正文（断言用）。"""
    payload = json.loads(sent["content"])
    if sent["msg_type"] == "text":
        return str(payload.get("text") or "")
    for element in payload.get("elements") or []:
        text = (element.get("text") or {}).get("content")
        if text:
            return str(text)
    return ""


@pytest.fixture(autouse=True)
def _clear_cache():
    transport.clear_token_cache()
    yield
    transport.clear_token_cache()


# ===========================================================================
# 验签 / 解密
# ===========================================================================
class TestVerifyRequest:
    @staticmethod
    def _headers(body: bytes, ts: str = "1700000000") -> dict:
        sig = hashlib.sha256((ts + "n" + ENCRYPT_KEY + body.decode()).encode()).hexdigest()
        return {
            crypto.SIGNATURE_HEADERS[0]: sig,
            crypto.SIGNATURE_HEADERS[1]: ts,
            crypto.SIGNATURE_HEADERS[2]: "n",
        }

    def test_valid_signature(self, tmp_db) -> None:
        _configure(feishu_encrypt_key=ENCRYPT_KEY)
        body = b'{"type":"event_callback"}'
        assert dispatcher.verify_request(self._headers(body), body) is True

    def test_missing_headers_is_rejected(self, tmp_db) -> None:
        """fail-closed：缺签名头一律拒绝。"""
        _configure(feishu_encrypt_key=ENCRYPT_KEY)
        assert dispatcher.verify_request({}, b"{}") is False

    def test_explicit_keys(self, tmp_db) -> None:
        body = b"{}"
        headers = self._headers(body)
        assert dispatcher.verify_request(
            headers, body, encrypt_key=ENCRYPT_KEY, verification_token="n"
        )


class TestDecryptBody:
    def test_plain_body_passes_through(self, tmp_db) -> None:
        body = {"type": "event_callback", "event": {}}
        assert dispatcher.decrypt_body(body) == body

    def test_encrypted_body_is_decrypted(self, tmp_db) -> None:
        _configure(feishu_encrypt_key=ENCRYPT_KEY)
        payload = {"type": "event_callback", "event": {"message": {}}}
        assert dispatcher.decrypt_body(_encrypted_body(payload)) == payload

    def test_wrong_key_returns_empty_without_raising(self, tmp_db) -> None:
        """解不开也**不抛**：宁可当作无法处理，也不要给飞书 5xx（它会重试）。"""
        _configure(feishu_encrypt_key="b" * 32)
        assert dispatcher.decrypt_body(_encrypted_body({"a": 1})) == {}

    def test_missing_key_returns_empty(self, tmp_db) -> None:
        assert dispatcher.decrypt_body(_encrypted_body({"a": 1})) == {}

    def test_non_dict(self, tmp_db) -> None:
        assert dispatcher.decrypt_body("nope") == {}


class TestUrlVerification:
    def test_echoes_challenge(self, tmp_db) -> None:
        body = {"type": "url_verification", "challenge": "CH-1"}
        assert dispatcher.handle_url_verification(body) == {"challenge": "CH-1"}

    def test_token_mismatch(self, tmp_db) -> None:
        _configure(feishu_verification_token="expected")
        body = {"type": "url_verification", "challenge": "C", "token": "other"}
        assert dispatcher.handle_url_verification(body) == {
            "code": 401,
            "msg": "invalid token",
        }

    def test_not_a_handshake(self, tmp_db) -> None:
        assert dispatcher.handle_url_verification({"type": "event_callback"}) is None
        assert dispatcher.handle_url_verification("x") is None


# ===========================================================================
# 事件 → 消息
# ===========================================================================
class TestExtractMessage:
    def test_text_message(self, tmp_db) -> None:
        message = dispatcher.extract_message(_message_event("@_user_1 /电表"))
        assert message is not None
        assert message.open_id == OPEN_ID
        assert message.text == "@_user_1 /电表"
        assert message.is_menu is False

    def test_non_text_message_is_ignored(self, tmp_db) -> None:
        assert dispatcher.extract_message(_message_event(message_type="image")) is None

    def test_menu_event(self, tmp_db) -> None:
        message = dispatcher.extract_message(_menu_event())
        assert message is not None
        assert message.is_menu is True and message.menu_key == "menu_remain"

    def test_unknown_event(self, tmp_db) -> None:
        assert dispatcher.extract_message({"header": {"event_type": "im.chat.updated"}}) is None

    def test_malformed_content(self, tmp_db) -> None:
        body = _message_event()
        body["event"]["message"]["content"] = "not-json"
        message = dispatcher.extract_message(body)
        assert message is not None and message.text == ""

    def test_non_dict(self, tmp_db) -> None:
        assert dispatcher.extract_message("x") is None


# ===========================================================================
# 三级降级回复
# ===========================================================================
class TestReply:
    @staticmethod
    def _reply(text: str = "⚡ 当前剩余: 12.34 kW·h", title: str = "⚡ 剩余电量"):
        return commands.Reply(text=text, title=title)

    def test_image_card_path(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"tenant_access_token": TOKEN_OK, "/im/v1/images": {"code": 0, "data": {"image_key": "img_1"}}}
        )
        message = dispatcher.Message(open_id=OPEN_ID)

        assert dispatcher.reply(message, self._reply(), session=session) is True
        assert session.uploads == 1
        sent = session.sent[-1]
        assert sent["msg_type"] == "interactive"
        card = json.loads(sent["content"])
        assert card["elements"][0]["img_key"] == "img_1"  # 🔑 卡片里叫 img_key

    def test_falls_back_to_text_card_when_upload_fails(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK}, fail=("/im/v1/images",))
        message = dispatcher.Message(open_id=OPEN_ID)

        assert dispatcher.reply(message, self._reply(), session=session) is True
        sent = session.sent[-1]
        assert sent["msg_type"] == "interactive"
        card = json.loads(sent["content"])
        assert card["elements"][0]["tag"] == "div"  # 没有图片，退成文本卡片

    def test_falls_back_to_plain_text_when_cards_fail(self, tmp_db) -> None:
        _configure()
        session = FakeSession(
            {"tenant_access_token": TOKEN_OK}, fail=("/im/v1/images",)
        )
        # 让「发卡片」失败：只保留纯文本可用
        original = session.post

        def _post(url, **kwargs):
            if "/im/v1/messages" in url and kwargs.get("json", {}).get("msg_type") == "interactive":
                raise requests.ConnectionError("cards down")
            return original(url, **kwargs)

        session.post = _post  # type: ignore[method-assign]
        message = dispatcher.Message(open_id=OPEN_ID)

        assert dispatcher.reply(message, self._reply(), session=session) is True
        assert session.sent[-1]["msg_type"] == "text"

    def test_plain_reply_when_no_title(self, tmp_db) -> None:
        """没有标题（例如「未知菜单项」）→ 直接发文本，不做图片。"""
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        message = dispatcher.Message(open_id=OPEN_ID)

        assert dispatcher.reply(message, commands.Reply(text="未知菜单项。"), session=session)
        assert session.uploads == 0
        assert session.sent[-1]["msg_type"] == "text"

    def test_no_open_id(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        assert dispatcher.reply(dispatcher.Message(open_id=""), self._reply(), session=session) is False
        assert session.calls == []

    def test_empty_reply(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        assert dispatcher.reply(dispatcher.Message(open_id=OPEN_ID), commands.Reply(text=""), session=session) is False

    def test_all_paths_fail_returns_false(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK}, fail=("/im/v1/",))
        message = dispatcher.Message(open_id=OPEN_ID)
        assert dispatcher.reply(message, self._reply(), session=session) is False


# ===========================================================================
# handle_event
# ===========================================================================
class TestHandleEvent:
    def test_url_verification(self, tmp_db) -> None:
        _configure()
        body = {"type": "url_verification", "challenge": "CH"}
        assert dispatcher.handle_event(body) == {"challenge": "CH"}

    def test_invalid_body(self, tmp_db) -> None:
        assert dispatcher.handle_event("nope") == {"code": 1, "msg": "invalid body"}

    def test_master_switch_off_is_silent(self, tmp_db) -> None:
        _configure(push_bot_enabled=False)
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        assert dispatcher.handle_event(_message_event(), session=session) == {
            "code": 0,
            "msg": "ok",
        }
        assert session.calls == []

    def test_command_replies(self, tmp_db) -> None:
        _configure()
        set_state("last_room_id", "room-1")
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=7.5))
        session = FakeSession(
            {"tenant_access_token": TOKEN_OK, "/im/v1/images": {"code": 0, "data": {"image_key": "img"}}}
        )

        assert dispatcher.handle_event(_message_event("/状态"), session=session) == {
            "code": 0,
            "msg": "ok",
        }
        assert session.uploads == 1  # 走的是图片卡片

    def test_menu_event_replies(self, tmp_db) -> None:
        _configure()
        set_state("last_room_id", "room-1")
        session = FakeSession({"tenant_access_token": TOKEN_OK})

        dispatcher.handle_event(_menu_event("menu_help"), session=session)

        # 有标题 → 走卡片（图片失败时降级为文本卡片，仍是 interactive）
        assert session.sent and session.sent[-1]["msg_type"] == "interactive"
        assert commands.HELP_TEXT in _body_of(session.sent[-1])

    def test_unknown_menu_is_polite(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        dispatcher.handle_event(_menu_event("menu_nope"), session=session)
        assert "未知菜单项" in _body_of(session.sent[-1])

    def test_mention_only_is_not_replied(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        dispatcher.handle_event(_message_event("@_user_1"), session=session)
        assert session.sent == []

    def test_unknown_event_is_acked(self, tmp_db) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})
        body = {"header": {"event_type": "im.chat.updated"}, "event": {}}
        assert dispatcher.handle_event(body, session=session) == {"code": 0, "msg": "ok"}
        assert session.calls == []

    def test_command_error_gets_fallback_text(self, tmp_db, monkeypatch) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})

        def _boom(*_args, **_kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(dispatcher.commands, "dispatch_text", _boom)

        assert dispatcher.handle_event(_message_event(), session=session) == {
            "code": 0,
            "msg": "ok",
        }
        assert dispatcher.FALLBACK_TEXT in json.loads(session.sent[-1]["content"])["text"]

    def test_reply_failure_gets_fallback_text(self, tmp_db, monkeypatch) -> None:
        _configure()
        session = FakeSession({"tenant_access_token": TOKEN_OK})

        def _boom(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(dispatcher, "reply", _boom)

        dispatcher.handle_event(_message_event("/帮助"), session=session)
        assert dispatcher.FALLBACK_TEXT in json.loads(session.sent[-1]["content"])["text"]
