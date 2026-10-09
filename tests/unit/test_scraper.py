"""``starwatt.scraper.ssrf`` + ``client`` 单元测试 —— SSRF 4 层与重试语义。

这些用例是**安全回归网**：SSRF 防护一旦被削弱，必须立刻在 CI 红掉。
"""
from __future__ import annotations

import socket

import pytest
import requests

from starwatt.scraper import client as cl
from starwatt.scraper import endpoints as ep
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

    def test_returns_list(self) -> None:
        """F2 / F3 / F5 返回的是**数组** —— 必须原样透传。

        📌 真实学校响应实测：``getEmDayElectQuery`` 返回
        ``[{"dt":"2026-10-08","eebm":8245.35,...}, ...]``。

        ⚠️ 这条测试以前是**反的**（``test_json_array_raises`` 断言数组必须
        抛 ScrapeError）—— 那个断言让 F2 / F3 / F5 在生产里永远失败，
        ``daily_elec`` / ``violations`` / ``pay_history`` 三张表恒为空。
        形状归一化是 :func:`as_list` 的职责，客户端只判「是不是 JSON」。
        """
        client, _ = _make([_FakeResponse(body=b'[{"dt":"2026-10-08","eebm":8245.35}]')])
        payload = client.post_form("/a", {})
        assert isinstance(payload, list)
        assert payload[0]["eebm"] == 8245.35

    def test_list_payload_passes_status_check(self) -> None:
        """数组响应没有 ``status`` 字段 —— 那不是失败，不能抛。"""
        ep.check_status([{"dt": "2026-10-08", "eebm": 8245.35}], "/x")


class TestSsrfNotRetried:
    def test_ssrf_rejection_is_not_retried(self) -> None:
        """安全拒绝重试没有意义 —— 直接抛出，不浪费 3 轮。"""
        client, session = _make([_FakeResponse()], retry_count=5)
        with pytest.raises(ssrf.SsrfError):
            client.get("http://localhost/a")
        assert session.calls == []


# ===========================================================================
# endpoints —— 响应整形 / 房间发现 / 字段映射
# ===========================================================================
class TestAsList:
    """学校对空结果会返回裸对象 —— 容器名是实测出来的，不能改。"""

    @pytest.mark.parametrize("key", ["rows", "data", "list", "records"])
    def test_unwraps_known_containers(self, key) -> None:
        assert ep.as_list({key: [{"a": 1}]}) == [{"a": 1}]

    def test_bare_list(self) -> None:
        assert ep.as_list([{"a": 1}]) == [{"a": 1}]

    def test_filters_non_dicts(self) -> None:
        assert ep.as_list([{"a": 1}, "junk", 3, None]) == [{"a": 1}]

    def test_unknown_shape_yields_zero_rows(self) -> None:
        """错误字符串 / 未知结构一律当 0 行，不抛异常。"""
        assert ep.as_list({"unexpected": 1}) == []
        assert ep.as_list("error") == []
        assert ep.as_list(None) == []


class TestCheckStatus:
    def test_accepts_zero(self) -> None:
        ep.check_status({"status": "0"}, "/x")
        ep.check_status({"status": 0}, "/x")
        ep.check_status({}, "/x")  # 没有 status 字段 = 不校验

    def test_rejects_failure_code(self) -> None:
        with pytest.raises(cl.ScrapeError, match="失败码"):
            ep.check_status({"status": "-1", "message": "登录失效"}, "/x")

    def test_includes_message(self) -> None:
        with pytest.raises(cl.ScrapeError, match="登录失效"):
            ep.check_status({"status": "-1", "message": "登录失效"}, "/x")


class TestDateRange:
    def test_default_window(self) -> None:
        from datetime import datetime

        stime, etime = ep.date_range(30, today=datetime(2026, 10, 6))
        assert (stime, etime) == ("2026-09-06", "2026-10-06")

    def test_pay_range_is_anchored_to_semester(self) -> None:
        """📌 不是 30 天滚动窗口 —— 否则 9/7 的充值永远查不到。"""
        from datetime import datetime

        stime, etime = ep.pay_date_range(today=datetime(2026, 10, 6))
        assert stime == ep.PAY_SEMESTER_START == "2026-09-07"
        assert etime == "2026-10-06"


class TestDiscoverRoom:
    def test_parses_hidden_room_id_and_label(self) -> None:
        html = (
            '<input type="hidden" id="roomId" value="abc-123">'
            '<input type="text" id="roomNo" value="6号楼-1-119">'
        )
        info = ep.discover_room(html)
        assert info.room_id == "abc-123"
        assert info.room_label == "6号楼-1-119"
        assert info.found is True

    def test_handles_reversed_attribute_order(self) -> None:
        """📌 Round 34A：学校 HTML 的属性顺序不保证，两种都要认。"""
        html = (
            '<input value="B-1-119" id="roomNo">'
            '<input type="hidden" id="roomId" value="xyz">'
        )
        info = ep.discover_room(html)
        assert info.room_id == "xyz"
        assert info.room_label == "B-1-119"

    def test_ignores_non_hidden_roomid(self) -> None:
        html = '<input type="text" id="roomId" value="should-not-use">'
        assert ep.discover_room(html).room_id is None

    def test_missing_label_is_none(self) -> None:
        info = ep.discover_room('<input type="hidden" id="roomId" value="abc">')
        assert info.room_id == "abc" and info.room_label is None

    def test_empty_or_garbage_html(self) -> None:
        assert ep.discover_room("").found is False
        assert ep.discover_room("<html>login</html>").found is False


class TestF1Parse:
    def test_reads_remain_eq_and_dt(self, tmp_db, frozen_now) -> None:
        row = ep.F1Live.parse({"remainEq": 10.5, "dt": "2026-10-06 11:00:00"})
        assert row["remain"] == 10.5
        assert row["read_time"] == "2026-10-06 11:00:00"
        assert row["ts"] == "2026-10-06 12:00:00"

    def test_missing_remain_is_none_not_zero(self) -> None:
        """「不知道」与「没电了」是两回事 —— 卡片配色依赖这个区别。"""
        assert ep.F1Live.parse({})["remain"] is None

    def test_accepts_camel_case_fallback(self) -> None:
        assert ep.F1Live.parse({"remain": 3.0})["remain"] == 3.0

    def test_garbage_remain_is_none(self) -> None:
        assert ep.F1Live.parse({"remainEq": "n/a"})["remain"] is None


class TestF2Parse:
    def test_maps_fields(self) -> None:
        rows = ep.F2Daily.parse(
            [{"dt": "2026-10-06", "esbm": 1.0, "eebm": 30.83, "total_eq": 9.9}],
            "room-1",
        )
        assert rows == [{
            "roomId": "room-1", "dt": "2026-10-06", "esbm": 1.0,
            "eebm": 30.83, "total_eq": 9.9, "zong_eq": 30.83,
        }]

    def test_zong_prefers_eebm_over_useEq(self) -> None:
        """🔑 **Round 33c 的核心**：F2 的 eebm 与 F1 的 useEq 同名不同义。

        取错会让 ``used_today`` 算出 6.70 而不是 30.83。
        """
        rows = ep.F2Daily.parse(
            [{"dt": "2026-10-06", "eebm": 30.83, "useEq": 6.70}], "r"
        )
        assert rows[0]["zong_eq"] == 30.83

    def test_zong_falls_back_to_explicit_then_useEq(self) -> None:
        for raw, expected in (
            ({"dt": "d", "zong_eq": 5.0}, 5.0),
            ({"dt": "d", "zongEq": 6.0}, 6.0),
            ({"dt": "d", "useEq": 7.0}, 7.0),
        ):
            assert ep.F2Daily.parse([raw], "r")[0]["zong_eq"] == expected

    def test_skips_rows_without_dt(self) -> None:
        assert ep.F2Daily.parse([{"esbm": 1.0}, {"dt": ""}], "r") == []


class TestF3Parse:
    def test_maps_fields(self) -> None:
        rows = ep.F3Violation.parse(
            [{"dt": "2026-10-06 10:00:00", "wg_reason": "大功率", "wg_power": 1.2}],
            "room-1",
        )
        assert rows[0]["wg_reason"] == "大功率"
        assert rows[0]["wg_power"] == 1.2

    def test_accepts_label_fallbacks(self) -> None:
        rows = ep.F3Violation.parse(
            [{"dt": "d", "wgReasonLabel": "超载", "wgPower": 2.0}], "r"
        )
        assert rows[0]["wg_reason"] == "超载" and rows[0]["wg_power"] == 2.0

    def test_skips_rows_missing_primary_key_parts(self) -> None:
        """主键是 (roomId, dt, wg_reason) —— 缺一不可。"""
        assert ep.F3Violation.parse([{"dt": "d"}, {"wg_reason": "x"}], "r") == []


class TestF5Parse:
    def test_maps_fields(self) -> None:
        rows = ep.F5Pay.parse(
            [{"dt": "2026-09-07", "pay_type": "充值", "fee_type": "电费",
              "money": 50.0}], "room-1",
        )
        assert rows[0]["money"] == 50.0 and rows[0]["pay_type"] == "充值"

    def test_accepts_label_fallbacks(self) -> None:
        rows = ep.F5Pay.parse(
            [{"dt": "d", "payTypeLabel": "退款", "feeTypeLabel": "电费"}], "r"
        )
        assert rows[0]["pay_type"] == "退款"

    def test_skips_rows_missing_any_key_part(self) -> None:
        assert ep.F5Pay.parse([{"dt": "d", "pay_type": "x"}], "r") == []
        assert ep.F5Pay.parse([{"dt": "d", "fee_type": "y"}], "r") == []

    def test_fee_type_defaults_to_all(self) -> None:
        """📌 legacy 默认 -1 是「只查退费」，把每条充值都过滤掉了。"""
        assert ep.F5Pay().fee_type == 0


class TestIsMeterOnline:
    def test_online_values(self) -> None:
        for value in ("在线", "正常", "通讯正常"):
            assert ep.is_meter_online({"runStatus": value}) is True, value

    def test_offline_for_unknown_value(self) -> None:
        assert ep.is_meter_online({"runStatus": "离线"}) is False

    def test_missing_field_counts_as_offline(self) -> None:
        """📌 B6：缺字段也算离线 —— 宁可误报也不能静默失败。"""
        assert ep.is_meter_online({}) is False
        assert ep.is_meter_online(None) is False

    def test_accepts_snake_case(self) -> None:
        assert ep.is_meter_online({"run_status": "在线"}) is True


class TestEndpointRegistry:
    def test_five_endpoints_in_order(self) -> None:
        assert [e.key for e in ep.ENDPOINTS] == ["F1", "F4", "F2", "F3", "F5"]

    def test_throttles_match_the_contract(self) -> None:
        by_key = {e.key: e.interval_sec for e in ep.ENDPOINTS}
        assert by_key["F1"] is None  # 每次抓取
        assert by_key["F4"] is None
        assert by_key["F2"] == 24 * 3600
        assert by_key["F3"] == 3600
        assert by_key["F5"] == 24 * 3600

    def test_paths_are_the_real_ones(self) -> None:
        """路径是从 legacy 提取的 —— 写错就抓不到任何数据。"""
        for endpoint in ep.ENDPOINTS:
            assert endpoint.path.startswith("/campus/webchat/"), endpoint.key

    def test_satisfies_endpoint_protocol(self) -> None:
        for endpoint in ep.ENDPOINTS:
            for attr in ("key", "interval_sec", "path"):
                assert hasattr(endpoint, attr), endpoint.key
            for method in ("fetch", "persist"):
                assert callable(getattr(endpoint, method)), endpoint.key


class TestPersist:
    def test_f1_writes_a_record(self, tmp_db, frozen_now) -> None:
        from starwatt.db.repositories import RecordRepo

        ep.F1Live().persist([{
            "ts": "2026-10-06 12:00:00", "read_time": "2026-10-06 11:59:00",
            "remain": 42.0,
        }])
        latest = RecordRepo.latest()
        assert latest is not None and latest.remain == 42.0

    def test_f2_writes_daily_elec(self, tmp_db) -> None:
        from starwatt.db.repositories import DailyElecRepo

        rows = ep.F2Daily.parse([{"dt": "2026-10-06", "eebm": 30.83}], "room-1")
        ep.F2Daily().persist(rows)
        assert DailyElecRepo.recent("room-1", days=365)[0].zong_eq == 30.83

    def test_f3_writes_violation(self, tmp_db) -> None:
        from starwatt.db.repositories import ViolationRepo

        rows = ep.F3Violation.parse(
            [{"dt": "2026-10-06", "wg_reason": "大功率", "wg_power": 1.2}], "room-1"
        )
        ep.F3Violation().persist(rows)
        assert ViolationRepo.recent("room-1", days=365)[0].wg_reason == "大功率"

    def test_f5_writes_pay(self, tmp_db) -> None:
        from starwatt.db.repositories import PayRepo

        rows = ep.F5Pay.parse(
            [{"dt": "2026-09-07", "pay_type": "充值", "fee_type": "电费",
              "money": 50.0}], "room-1",
        )
        ep.F5Pay().persist(rows)
        assert PayRepo.recent("room-1", days=365)[0].money == 50.0

    def test_f4_writes_run_status(self, tmp_db) -> None:
        from starwatt.db.repositories import RunStatusRepo

        ep.F4RunStatus().persist(
            [{"roomId": "room-1", "data": {"vol": 220.1, "runStatus": "在线"}}]
        )
        assert RunStatusRepo.get("room-1").run_status == "在线"

    def test_f4_update_dt_falls_back_to_dt(self, tmp_db) -> None:
        """学校 F4 **只返回 ``dt``** —— ``update_dt`` 必须回退到它。

        否则 ``/api/live`` 里的 ``run_status.update_dt`` 恒为 ``None``，
        电表页「最后上报」、飞书离线卡、``/dorm status`` 三处一起显示「—」。
        """
        from starwatt.db.repositories import RunStatusRepo

        ep.F4RunStatus().persist(
            [
                {
                    "roomId": "room-1",
                    "data": {
                        "dt": "2026-10-09 17:59:02",
                        "runStatus": "通讯正常",
                        "vol": 228.6,
                    },
                }
            ]
        )
        row = RunStatusRepo.get("room-1")
        assert row.dt == "2026-10-09 17:59:02"
        assert row.update_dt == "2026-10-09 17:59:02"

    def test_persist_ignores_empty_room(self, tmp_db) -> None:
        """空房间号不能写入（否则会污染一个无名房间的数据）。"""
        ep.F2Daily().persist([{"roomId": "", "dt": "2026-10-06"}])
        ep.F3Violation().persist([{"roomId": "", "dt": "d", "wg_reason": "x"}])
