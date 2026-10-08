"""``starwatt.notify.transport`` —— 唯一的推送出口。

重点三条：

1. 签名算法是飞书规范里那个**反直觉**的写法（待签串当 key、消息体留空）
2. 失败默认**不抛**（K7），只有显式 ``raise_on_error=True`` 才抛（OOBE 测试按钮）
3. 未配置 webhook → ``False`` + NOTICE，且**不发请求**
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest
import requests

from starwatt.notify import transport


class FakeResponse:
    def __init__(
        self, status_code: int = 200, text: str = '{"code":0}', payload=None
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    """记录调用的假会话（可注入错误 / 按 URL 匹配响应）。"""

    def __init__(
        self,
        response: FakeResponse | None = None,
        error=None,
        responses: dict | None = None,
    ) -> None:
        self.response = response or FakeResponse()
        self.error = error
        self.responses = dict(responses or {})
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        if self.error is not None:
            raise self.error
        for marker, payload in self.responses.items():
            if marker in url:
                if isinstance(payload, FakeResponse):
                    return payload
                return FakeResponse(payload=payload)
        return self.response


CARD = {"msg_type": "interactive", "card": {"header": {}, "elements": []}}
WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/x"


def _configure_webhook(url: str = WEBHOOK, secret: str = "") -> None:
    from starwatt.config_registry import set_many

    errors = set_many({"feishu_webhook_url": url, "feishu_secret": secret})
    assert errors == {}, errors


class TestSign:
    def test_matches_the_official_algorithm(self) -> None:
        """``sig = b64(HMAC-SHA256(key=ts+"\\n"+secret, msg=b""))`` —— 别改成常规 HMAC。"""
        ts, sig = transport.sign("s3cr3t", timestamp=1700000000)
        expected = base64.b64encode(
            hmac.new(b"1700000000\ns3cr3t", digestmod=hashlib.sha256).digest()
        ).decode("utf-8")
        assert ts == "1700000000"
        assert sig == expected
        assert len(base64.b64decode(sig)) == 32

    def test_timestamp_defaults_to_now(self) -> None:
        ts, _ = transport.sign("s")
        assert ts.isdigit() and len(ts) == 10


class TestPostCard:
    def test_success(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        assert transport.post_card(CARD, session=session) is True
        assert session.calls[0]["url"] == WEBHOOK
        assert session.calls[0]["json"]["msg_type"] == "interactive"

    def test_bare_card_is_wrapped(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        transport.post_card({"header": {}, "elements": []}, session=session)
        sent = session.calls[0]["json"]
        assert sent["msg_type"] == "interactive" and "header" in sent["card"]

    def test_signature_is_attached_when_secret_set(self, tmp_db) -> None:
        _configure_webhook(secret="s3cr3t")
        session = FakeSession()
        transport.post_card(CARD, session=session)
        sent = session.calls[0]["json"]
        assert sent["timestamp"].isdigit() and sent["sign"]

    def test_no_secret_means_no_signature_fields(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        transport.post_card(CARD, session=session)
        assert "sign" not in session.calls[0]["json"]

    def test_missing_webhook_skips_the_request(self, tmp_db) -> None:
        session = FakeSession()
        assert transport.post_card(CARD, session=session) is False
        assert session.calls == []

    def test_http_error_is_swallowed_by_default(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession(response=FakeResponse(status_code=400, text="bad"))
        assert transport.post_card(CARD, session=session) is False

    def test_http_error_raises_when_asked(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession(response=FakeResponse(status_code=500))
        with pytest.raises(requests.HTTPError):
            transport.post_card(CARD, session=session, raise_on_error=True)

    def test_transport_error_is_swallowed(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession(error=requests.ConnectionError("boom"))
        assert transport.post_card(CARD, session=session) is False

    def test_explicit_webhook_overrides_config(self, tmp_db) -> None:
        """OOBE「发送测试消息」在配置还没保存时用显式 webhook。"""
        session = FakeSession()
        assert (
            transport.post_card(
                CARD, webhook_url="https://example.com/hook", session=session
            )
            is True
        )
        assert session.calls[0]["url"] == "https://example.com/hook"

    def test_timeout_is_passed(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        transport.post_card(CARD, session=session, timeout_sec=3)
        assert session.calls[0]["timeout"] == 3


class TestPostText:
    def test_shape(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        assert transport.post_text("你好", session=session) is True
        assert session.calls[0]["json"] == {
            "msg_type": "text",
            "content": {"text": "你好"},
        }


class TestWebhookNotifier:
    def test_disabled_without_webhook(self, tmp_db) -> None:
        assert transport.webhook_notifier().enabled() is False

    def test_enabled_with_webhook(self, tmp_db) -> None:
        _configure_webhook()
        assert transport.webhook_notifier().enabled() is True

    def test_disabled_by_master_switch(self, tmp_db) -> None:
        """总开关关闭 → 分项开关无效（Q16 静默降级）。"""
        from starwatt.config_registry import set_many

        _configure_webhook()
        set_many({"push_group_enabled": False})
        assert transport.webhook_notifier().enabled() is False

    def test_name_matches_the_protocol(self) -> None:
        assert transport.WebhookNotifier.name == "feishu_group"

    def test_send_card_never_raises(self, tmp_db, monkeypatch) -> None:
        """协议约定：``send_*`` 失败只记日志，不向上抛。"""
        _configure_webhook()
        monkeypatch.setattr(
            transport,
            "post_card",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        transport.WebhookNotifier().send_card(CARD)  # 不应抛

    def test_send_text_never_raises(self, tmp_db, monkeypatch) -> None:
        _configure_webhook()
        monkeypatch.setattr(
            transport,
            "post_text",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("x")),
        )
        transport.WebhookNotifier().send_text("hi")  # 不应抛

    def test_send_card_uses_injected_session(self, tmp_db) -> None:
        _configure_webhook()
        session = FakeSession()
        transport.WebhookNotifier(session=session).send_card(CARD)
        assert len(session.calls) == 1


class TestResolve:
    def test_resolve_returns_decrypted_config(self, tmp_db) -> None:
        _configure_webhook(secret="s3cr3t")
        assert transport.resolve_webhook() == WEBHOOK
        assert transport.resolve_secret() == "s3cr3t"

    def test_resolve_empty_by_default(self, tmp_db) -> None:
        assert transport.resolve_webhook() == ""
        assert transport.resolve_secret() == ""


# ===========================================================================
# 飞书应用机器人 API（私聊回复）
# ===========================================================================
APP_TOKEN = {"code": 0, "tenant_access_token": "T-1", "expire": 7200}


def _configure_app() -> None:
    from starwatt.config_registry import set_many

    errors = set_many({"feishu_app_id": "cli_x", "feishu_app_secret": "s"})
    assert errors == {}, errors


@pytest.fixture(autouse=True)
def _clear_app_token():
    transport.clear_token_cache()
    yield
    transport.clear_token_cache()


class TestTenantToken:
    def test_fetch_and_cache(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN})
        assert transport.tenant_access_token(session=session) == "T-1"
        assert transport.tenant_access_token(session=session) == "T-1"
        assert len(session.calls) == 1

    def test_request_shape(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN})
        transport.tenant_access_token(session=session)
        call = session.calls[0]
        assert call["url"].endswith("/auth/v3/tenant_access_token/internal")
        assert call["json"] == {"app_id": "cli_x", "app_secret": "s"}

    def test_missing_credentials(self, tmp_db) -> None:
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN})
        assert transport.tenant_access_token(session=session) == ""
        assert session.calls == []

    def test_business_error(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": {"code": 10003, "msg": "bad"}})
        assert transport.tenant_access_token(session=session) == ""

    def test_network_error(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(error=requests.ConnectionError("boom"))
        assert transport.tenant_access_token(session=session) == ""

    def test_clear_cache_forces_refetch(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN})
        transport.tenant_access_token(session=session)
        transport.clear_token_cache()
        transport.tenant_access_token(session=session)
        assert len(session.calls) == 2


class TestAppBotMessages:
    def test_upload_image_returns_key(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={
                "tenant_access_token": APP_TOKEN,
                "/im/v1/images": {"code": 0, "data": {"image_key": "img_v2_x"}},
            }
        )
        assert transport.upload_image(b"\x89PNG", session=session) == "img_v2_x"
        call = session.calls[-1]
        assert call["data"] == {"image_type": "message"}  # 🔑 必须是表单字段
        assert "image" in call["files"]

    def test_upload_without_token(self, tmp_db) -> None:
        session = FakeSession(responses={"/im/v1/images": {"code": 0, "data": {"image_key": "x"}}})
        assert transport.upload_image(b"\x89PNG", session=session) == ""
        assert session.calls == []

    def test_upload_business_error(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN, "/im/v1/images": {"code": 234001}}
        )
        assert transport.upload_image(b"x", session=session) == ""

    def test_reply_text_payload(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN, "/im/v1/messages": {"code": 0}}
        )
        assert transport.reply_text("ou_1", "你好", session=session) is True
        call = session.calls[-1]
        assert call["url"].endswith("/im/v1/messages?receive_id_type=open_id")
        assert call["json"]["msg_type"] == "text"
        # content 必须是**序列化后的 JSON 字符串**
        assert json.loads(call["json"]["content"]) == {"text": "你好"}
        assert call["headers"]["Authorization"] == "Bearer T-1"

    def test_reply_card_payload(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN, "/im/v1/messages": {"code": 0}}
        )
        card = transport.build_text_card("标题", "正文")
        assert transport.reply_card("ou_1", card, session=session) is True
        sent = session.calls[-1]["json"]
        assert sent["msg_type"] == "interactive"
        assert json.loads(sent["content"])["header"]["title"]["content"] == "标题"

    def test_reply_failure_returns_false(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN, "/im/v1/messages": {"code": 230001}}
        )
        assert transport.reply_text("ou_1", "x", session=session) is False

    def test_reply_without_receive_id(self, tmp_db) -> None:
        _configure_app()
        session = FakeSession(responses={"tenant_access_token": APP_TOKEN})
        assert transport.reply_text("", "x", session=session) is False


class TestAppBotCards:
    def test_image_card_uses_img_key(self, tmp_db) -> None:
        """🔑 上传返回 ``image_key``，卡片元素必须写 ``img_key``（否则 11310）。"""
        card = transport.build_image_card("img_v2_1", "标题", footer="PNG 1 bytes")
        assert card["elements"][0] == {"tag": "img", "img_key": "img_v2_1"}
        assert "image_key" not in card["elements"][0]
        assert card["elements"][-1]["tag"] == "note"
        assert card["config"] == {"wide_screen_mode": True}

    def test_text_card_shape(self, tmp_db) -> None:
        card = transport.build_text_card("T", "正文")
        assert card["elements"][0]["text"] == {"tag": "lark_md", "content": "正文"}
        assert card["header"]["template"] == "blue"

    def test_no_footer_when_empty(self, tmp_db) -> None:
        assert len(transport.build_image_card("k", "T")["elements"]) == 1
        assert len(transport.build_text_card("T", "b")["elements"]) == 1
