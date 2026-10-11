"""飞书 webhook 的**业务码**校验（🩺 审计 stability 缺陷修复）。

背景：飞书在「限流 / 机器人被移出群 / 签名错误」时返回的是
**HTTP 200 + ``code != 0``**。修复前的 ``transport._post`` 只看 HTTP 状态码，
于是这类失败被记成「飞书推送成功」—— 告警静默全丢。

这里逐条钉住新语义：

* ``{"code":19001,...}`` → 失败，且日志里**不能**出现「成功」
* ``{"code":0}`` → 成功
* 非 JSON 响应体 → 不崩、判失败、保留 200 字符内的原始片段
* 老式机器人只回 ``Extra``/``StatusCode`` → 不误判为失败
* ``raise_on_error=True``（OOBE 测试按钮）→ 抛 ``requests`` 异常族
"""
from __future__ import annotations

import json

import pytest
import requests

from starwatt.notify import transport

WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/x"

CARD = {"msg_type": "interactive", "card": {"header": {}, "elements": []}}


class FakeResponse:
    """最小响应替身：可指定 HTTP 码与原始文本（``json()`` 按文本解析）。"""

    def __init__(self, status_code: int = 200, text: str = '{"code":0}') -> None:
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}
        self.url = WEBHOOK
        self.content = text.encode("utf-8")

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeSession:
    def __init__(self, response: FakeResponse | None = None) -> None:
        self.response = response or FakeResponse()
        self.calls: list[dict] = []

    def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


def _configure_webhook() -> None:
    from starwatt.config_registry import set_many

    errors = set_many({"feishu_webhook_url": WEBHOOK, "feishu_secret": ""})
    assert errors == {}, errors


def _records(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records]


class TestBusinessCodeFailure:
    def test_non_zero_code_is_a_failure(self, tmp_db, caplog) -> None:
        """🔑 (a) 核心回归：HTTP 200 + ``code=19001`` 必须判失败。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text='{"code":19001,"msg":"机器人不在群"}'))

        with caplog.at_level("DEBUG"):
            assert transport.post_card(CARD, session=session) is False

        messages = _records(caplog)
        # 「业务未成功」里本身含「成功」二字，所以断言必须盯住成功分支那句原文
        assert not any("推送成功" in m for m in messages), messages
        assert any("19001" in m and "机器人不在群" in m for m in messages), messages

    def test_non_zero_code_raises_when_asked(self, tmp_db) -> None:
        """OOBE「发送测试消息」按 ``requests`` 异常族处理，业务错也走同一族。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text='{"code":19001,"msg":"限流"}'))

        with pytest.raises(requests.RequestException) as excinfo:
            transport.post_card(CARD, session=session, raise_on_error=True)

        assert "19001" in str(excinfo.value)
        assert excinfo.value.response is not None
        assert excinfo.value.response.status_code == 200

    def test_status_code_field_is_also_checked(self, tmp_db, caplog) -> None:
        """老/新版飞书字段名不同（``StatusCode`` / ``status_code``），都要判。"""
        _configure_webhook()
        for text in ('{"StatusCode":9499,"msg":"签名错误"}',
                     '{"status_code":9499,"message":"签名错误"}'):
            caplog.clear()
            session = FakeSession(FakeResponse(text=text))
            with caplog.at_level("DEBUG"):
                assert transport.post_card(CARD, session=session) is False
            assert any("9499" in m for m in _records(caplog)), text

    def test_zero_code_is_success(self, tmp_db, caplog) -> None:
        """(b) ``{"code":0}`` 判成功（修复不能把好路径也判死）。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text='{"code":0,"msg":"success"}'))

        with caplog.at_level("DEBUG"):
            assert transport.post_card(CARD, session=session) is True

        assert any("推送成功" in m for m in _records(caplog))

    def test_legacy_extra_only_response_is_success(self, tmp_db) -> None:
        """老式机器人只回 ``{"Extra":null,"StatusCode":0}`` 之外的 ``Extra``：
        没有业务码字段时**不误判**为失败。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text='{"Extra":null}'))
        assert transport.post_card(CARD, session=session) is True


class TestNonJsonResponse:
    def test_non_json_does_not_crash(self, tmp_db, caplog) -> None:
        """(c) 响应体不是 JSON：不崩、判失败、保留原始片段。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text="<html>502 Bad Gateway</html>"))

        with caplog.at_level("DEBUG"):
            assert transport.post_card(CARD, session=session) is False

        messages = _records(caplog)
        assert any("502 Bad Gateway" in m for m in messages), messages
        assert not any("推送成功" in m for m in messages), messages

    def test_non_json_fragment_is_truncated_to_200_chars(self, tmp_db, caplog) -> None:
        """原始片段截断到 200 字符内（日志不该被一个 1MB 的 HTML 撑爆）。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text="x" * 5000))

        with caplog.at_level("DEBUG"):
            assert transport.post_card(CARD, session=session) is False

        joined = "\n".join(_records(caplog))
        assert "x" * 200 in joined
        assert "x" * 201 not in joined

    def test_json_array_is_not_silently_ok(self, tmp_db) -> None:
        """JSON 但不是对象（顶层数组）→ 没有业务码可判 → 判失败。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text="[1,2,3]"))
        assert transport.post_card(CARD, session=session) is False


class TestHttpLayerUnchanged:
    def test_http_error_still_fails_without_raising(self, tmp_db) -> None:
        """修复不能改变 HTTP 层语义：4xx/5xx 仍默认不抛。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(status_code=500, text="oops"))
        assert transport.post_card(CARD, session=session) is False

    def test_payload_shape_unchanged(self, tmp_db) -> None:
        """返回 True 时请求体结构不变（调用方依赖 ``msg_type`` 分支）。"""
        _configure_webhook()
        session = FakeSession(FakeResponse(text='{"code":0}'))
        assert transport.post_card(CARD, session=session) is True
        assert session.calls[0]["json"]["msg_type"] == "interactive"
