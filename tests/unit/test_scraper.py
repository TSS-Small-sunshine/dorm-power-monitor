"""``starwatt.scraper.ssrf`` + ``client`` 单元测试 —— SSRF 4 层与重试语义。

这些用例是**安全回归网**：SSRF 防护一旦被削弱，必须立刻在 CI 红掉。
"""
from __future__ import annotations

import socket

import pytest
import requests

from starwatt.scraper import client as cl
from starwatt.scraper import ssrf


# ===========================================================================
# L1 —— scheme
# ===========================================================================
class TestLayer1Scheme:
    @pytest.mark.parametrize("url", ["http://a.example.com", "https://a.example.com/x"])
    def test_allows_http_https(self, url) -> None:
        assert ssrf.check_scheme(url) == "a.example.com"

    @pytest.mark.parametrize(
        "url",
        ["ftp://a.com", "file:///etc/passwd", "gopher://a.com",
         "javascript:alert(1)", "data:text/html,x", "//a.com"],
    )
    def test_rejects_other_schemes(self, url) -> None:
        with pytest.raises(ssrf.SsrfError):
            ssrf.check_scheme(url)

    @pytest.mark.parametrize("url", ["", "   ", None, "http://"])
    def test_rejects_empty_or_hostless(self, url) -> None:
        with pytest.raises(ssrf.SsrfError):
            ssrf.check_scheme(url)


# ===========================================================================
# L2 —— 主机名形态（离线可用，不发 DNS）
# ===========================================================================
class TestLayer2HostShape:
    @pytest.mark.parametrize(
        "host",
        ["localhost", "LOCALHOST", "localhost.", "ip6-localhost",
         "metadata.google.internal", "metadata"],
    )
    def test_rejects_blocked_hostnames(self, host) -> None:
        with pytest.raises(ssrf.SsrfError):
            ssrf.check_host_shape(host)

    @pytest.mark.parametrize(
        "host",
        ["a.localhost", "router.local", "svc.internal", "x.home.arpa"],
    )
    def test_rejects_blocked_suffixes(self, host) -> None:
        with pytest.raises(ssrf.SsrfError):
            ssrf.check_host_shape(host)

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254",
         "0.0.0.0", "255.255.255.255"],
    )
    def test_rejects_bare_ipv4(self, host) -> None:
        """裸 IP 一律拒 —— 正常学校域名不会是 IP。"""
        with pytest.raises(ssrf.SsrfError, match="域名"):
            ssrf.check_host_shape(host)

    def test_rejects_bare_ipv6(self) -> None:
        with pytest.raises(ssrf.SsrfError, match="域名"):
            ssrf.check_host_shape("::1")

    def test_allows_normal_domain(self) -> None:
        ssrf.check_host_shape("xydf.example.edu.cn")
        ssrf.check_host_shape("A.Example.COM.")  # 大小写与尾点都归一化


# ===========================================================================
# L3 —— 解析后的 IP
# ===========================================================================
class TestLayer3ResolvedIp:
    @pytest.mark.parametrize(
        "ip",
        ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
         "169.254.169.254", "100.64.0.1", "224.0.0.1", "0.0.0.0",
         "::1", "fe80::1", "fc00::1", "::", "255.255.255.255"],
    )
    def test_blocks_private_and_reserved(self, ip) -> None:
        assert ssrf.is_blocked_ip(ip) is True, ip

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "223.5.5.5"])
    def test_allows_public(self, ip) -> None:
        assert ssrf.is_blocked_ip(ip) is False, ip

    def test_unparseable_is_treated_as_unsafe(self) -> None:
        """解析不了 → 当作不安全（fail-closed，Q17）。"""
        assert ssrf.is_blocked_ip("not-an-ip") is True
        assert ssrf.is_blocked_ip("999.999.999.999") is True

    def test_dns_failure_is_ssrf_error(self, monkeypatch) -> None:
        """DNS 查不到 → SsrfError（消息含中文说明，可直接给前端）。

        ⚠️ 用 mock 而不是真域名：某些 DNS（运营商劫持 / captive portal）
        会**解析成功**任何域名，那样这条用例就变成看环境脸色。
        """
        import socket

        def _boom(*_args, **_kwargs):
            raise socket.gaierror("Name or service not known")

        monkeypatch.setattr(socket, "getaddrinfo", _boom)
        with pytest.raises(ssrf.SsrfError, match="无法解析"):
            ssrf.resolve_ips("no-such-host.example")

    def test_dns_rebinding_is_blocked(self, monkeypatch) -> None:
        """🔑 L3 的核心价值：域名解析到内网 → 必须拒绝。"""
        monkeypatch.setattr(
            ssrf, "resolve_ips", lambda host: ["127.0.0.1"]
        )
        with pytest.raises(ssrf.SsrfError, match="内网地址"):
            ssrf.assert_public_url("https://evil.example.com/")

    def test_public_domain_passes(self, monkeypatch) -> None:
        monkeypatch.setattr(ssrf, "resolve_ips", lambda host: ["93.184.216.34"])
        assert ssrf.assert_public_url("https://x.example.com/") == "x.example.com"

    def test_allow_private_escape_hatch(self, monkeypatch) -> None:
        """自建 LAN 代理场景：显式开启才跳过 L2/L3（默认关）。"""
        assert ssrf.assert_public_url(
            "http://192.168.1.10:8080/", allow_private=True
        ) == "192.168.1.10"

    def test_allow_private_still_enforces_l1(self) -> None:
        """逃生舱**不能**绕过 L1 —— scheme 白名单永远生效。"""
        with pytest.raises(ssrf.SsrfError):
            ssrf.assert_public_url("ftp://192.168.1.10/", allow_private=True)


# ===========================================================================
# client —— 重试 / 重定向 L4 / 体积上限
# ===========================================================================
class _FakeResponse:
    """最小化的 ``requests.Response`` 替身。"""

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"{}",
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.encoding = "utf-8"
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size: int = 8192):
        if self._chunks is not None:
            yield from self._chunks
        else:
            yield self._body

    def close(self) -> None:
        self.closed = True


class _FakeSession:
    """按顺序吐出预设响应的假会话（完全不碰网络）。"""

    def __init__(self, responses: list[object]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        item = self._responses.pop(0) if self._responses else _FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item

    def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _no_dns(monkeypatch):
    """把 DNS 固定到一个公网 IP —— 用例不依赖外网。

    注意 patch 的是 **``socket.getaddrinfo``**（最底层），而不是
    ``ssrf.resolve_ips``：这样 ``resolve_ips`` 自身的逻辑（去重、空结果
    检查、gaierror → SsrfError 转换）仍然被覆盖到。
    """
    def _fake(*_args, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "",
             ("93.184.216.34", 0))
        ]

    monkeypatch.setattr(socket, "getaddrinfo", _fake)


def _make(responses: list[object], **kwargs) -> tuple[cl.SchoolClient, _FakeSession]:
    session = _FakeSession(responses)
    kwargs.setdefault("retry_count", 2)
    kwargs.setdefault("user_agent", "WeChat-UA")
    kwargs.setdefault("sleeper", lambda _s: None)  # 测试不真睡
    return cl.SchoolClient("https://x.example.com", session=session, **kwargs), session


class TestRequestHeaders:
    def test_sends_disguise_headers(self) -> None:
        """A9：微信 UA + Referer + X-Requested-With，缺一个都可能被 WAF 拦。"""
        client, session = _make([_FakeResponse()])
        client.get("/a")
        headers = session.calls[0]["headers"]
        assert headers["User-Agent"] == "WeChat-UA"
        assert headers["X-Requested-With"] == "XMLHttpRequest"
        assert headers["Referer"].endswith("/campus/webchat/dormEmRealRead/finduser")

    def test_url_joins_base_and_path(self) -> None:
        client, session = _make([_FakeResponse()])
        client.get("/campus/webchat/x")
        assert session.calls[0]["url"] == "https://x.example.com/campus/webchat/x"

    def test_redirects_disabled_for_manual_following(self) -> None:
        """L4：必须自己跟重定向，才能逐跳复检。"""
        client, session = _make([_FakeResponse()])
        client.get("/a")
        assert session.calls[0]["allow_redirects"] is False


class TestRetrySemantics:
    def test_retries_transport_errors(self) -> None:
        client, session = _make([
            requests.ConnectionError("boom"),
            _FakeResponse(status=200),
        ])
        assert client.get("/a") == "{}"
        assert len(session.calls) == 2

    def test_retries_5xx(self) -> None:
        client, session = _make([
            _FakeResponse(status=503),
            _FakeResponse(status=200),
        ])
        assert client.get("/a") == "{}"
        assert len(session.calls) == 2

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_does_not_retry_4xx(self, status) -> None:
        """🔑 4xx 是客户端错误 —— 旧代码会白等 3 轮，重写后立刻失败。"""
        client, session = _make([_FakeResponse(status=status)])
        with pytest.raises(cl.ScrapeError, match=str(status)):
            client.get("/a")
        assert len(session.calls) == 1

    def test_gives_up_after_retries(self) -> None:
        client, session = _make([requests.Timeout("t")] * 3)
        with pytest.raises(cl.ScrapeError, match="仍失败"):
            client.get("/a")
        assert len(session.calls) == 3  # 首次 + 2 次重试

    def test_backoff_is_capped(self) -> None:
        slept: list[float] = []
        client, _ = _make(
            [requests.Timeout("t")] * 6, retry_count=5, sleeper=slept.append
        )
        with pytest.raises(cl.ScrapeError):
            client.get("/a")
        assert slept == [1, 2, 4, 8, 8]  # 指数退避，8s 封顶


class TestRedirectLayer4:
    def test_follows_same_host_redirect(self) -> None:
        client, session = _make([
            _FakeResponse(status=302, headers={"Location": "/b"}),
            _FakeResponse(status=200, body=b'{"ok":1}'),
        ])
        assert client.get("/a") == '{"ok":1}'
        assert session.calls[1]["url"] == "https://x.example.com/b"

    def test_blocks_redirect_to_private_ip(self, monkeypatch) -> None:
        """🔑 **L4 的核心用例**：公网 URL 302 到云元数据地址必须被拦。"""
        def _resolve(host):
            return (
                ["93.184.216.34"] if host == "x.example.com" else ["169.254.169.254"]
            )

        monkeypatch.setattr(ssrf, "resolve_ips", _resolve)
        client, session = _make([
            _FakeResponse(
                status=302,
                headers={"Location": "http://169.254.169.254/latest/meta-data/"},
            ),
        ])
        with pytest.raises(ssrf.SsrfError):
            client.get("/a")
        assert len(session.calls) == 1  # 没有发出第二跳

    def test_blocks_redirect_to_localhost(self) -> None:
        client, session = _make([
            _FakeResponse(
                status=302, headers={"Location": "http://localhost:6379/"}
            ),
        ])
        with pytest.raises(ssrf.SsrfError):
            client.get("/a")
        assert len(session.calls) == 1

    def test_rejects_too_many_redirects(self) -> None:
        hops = [
            _FakeResponse(status=302, headers={"Location": "/next"})
            for _ in range(cl.MAX_REDIRECTS + 1)
        ]
        client, _ = _make(hops)
        with pytest.raises(cl.ScrapeError, match="重定向"):
            client.get("/a")

    def test_rejects_redirect_without_location(self) -> None:
        client, _ = _make([_FakeResponse(status=302)])
        with pytest.raises(cl.ScrapeError, match="Location"):
            client.get("/a")

    def test_303_becomes_get(self) -> None:
        client, session = _make([
            _FakeResponse(status=303, headers={"Location": "/done"}),
            _FakeResponse(status=200),
        ])
        client.request("POST", "/a", data={"k": "v"})
        assert session.calls[1]["method"] == "GET"
        assert session.calls[1]["data"] is None


class TestSizeCap:
    def test_rejects_oversized_body(self) -> None:
        """L4：超大响应体立即中止，防止内存被撑爆。"""
        big = [b"x" * (512 * 1024)] * 5  # 2.5 MB > 2 MB 上限
        client, _ = _make([_FakeResponse(chunks=big)])
        with pytest.raises(cl.ScrapeError, match="MB"):
            client.get("/a")

    def test_accepts_normal_body(self) -> None:
        client, _ = _make([_FakeResponse(body=b'{"a":1}')])
        assert client.get("/a") == '{"a":1}'


class TestPostForm:
    def test_returns_dict(self) -> None:
        client, _ = _make([_FakeResponse(body=b'{"data":[]}')])
        assert client.post_form("/a", {"roomId": "x"}) == {"data": []}

    def test_non_json_raises_scrape_error(self) -> None:
        client, _ = _make([_FakeResponse(body=b"<html>login</html>")])
        with pytest.raises(cl.ScrapeError, match="不是 JSON"):
            client.post_form("/a", {})

    def test_json_array_raises(self) -> None:
        client, _ = _make([_FakeResponse(body=b"[1,2,3]")])
        with pytest.raises(cl.ScrapeError, match="不是对象"):
            client.post_form("/a", {})


class TestSsrfNotRetried:
    def test_ssrf_rejection_is_not_retried(self) -> None:
        """安全拒绝重试没有意义 —— 直接抛出，不浪费 3 轮。"""
        client, session = _make([_FakeResponse()], retry_count=5)
        with pytest.raises(ssrf.SsrfError):
            client.get("http://localhost/a")
        assert session.calls == []
