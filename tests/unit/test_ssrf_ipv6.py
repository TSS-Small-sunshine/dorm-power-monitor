"""SSRF L3 — IPv4-mapped / NAT64 绕过面（审计 F-08）。

背景：``ipaddress`` 的跨版本包含判断恒为 False，所以
``is_blocked_ip("::ffff:127.0.0.1")`` 曾经返回 ``False``。攻击者只要让
域名 AAAA 记录解析出 ``::ffff:7f00:1`` 就能绕过 L3 打到 ``127.0.0.1``
（URL 字面量写 ``[::ffff:127.0.0.1]`` 会被 L2 的 ``":" in host`` 拦下）。

修法：命中映射前缀时不整段拉黑，而是**归一化成内嵌 IPv4 再判**——
所以「私有 IPv4 的映射写法」必须拦，「公网 IPv4 的映射写法」必须放行。
"""

from __future__ import annotations

import ipaddress

import pytest

from starwatt.scraper import ssrf


class TestIpv4MappedPrivateBlocked:
    """映射写法里的私有/保留 IPv4 必须被判为 blocked。"""

    @pytest.mark.parametrize(
        "ip",
        [
            "::ffff:127.0.0.1",  # 回环（权威复现用例）
            "::ffff:10.0.0.1",  # RFC1918
            "::ffff:169.254.169.254",  # 云元数据
            "::ffff:192.168.1.1",
            "::ffff:172.16.0.1",
            "::ffff:100.64.0.1",  # CGNAT
            "::ffff:0.0.0.0",
            "::ffff:7f00:1",  # 十六进制写法 = 127.0.0.1
            "::ffff:a00:1",  # = 10.0.0.1
        ],
    )
    def test_ipv4_mapped_private_is_blocked(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is True, ip


class TestNat64Blocked:
    """NAT64 前缀 ``64:ff9b::/96`` 内嵌 IPv4，按内嵌值判定。

    ``ipaddress`` 对 NAT64 地址**不填** ``ipv4_mapped``（只有
    ``::ffff:0:0/96`` 才有），所以这里单独覆盖低 32 位提取逻辑。
    """

    @pytest.mark.parametrize(
        "ip",
        [
            "64:ff9b::127.0.0.1",
            "64:ff9b::10.0.0.1",
            "64:ff9b::169.254.169.254",
            "64:ff9b::7f00:1",  # 十六进制写法 = 127.0.0.1
            "64:ff9b::a00:1",  # = 10.0.0.1
        ],
    )
    def test_nat64_private_is_blocked(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is True, ip

    def test_nat64_public_is_allowed(self) -> None:
        """NAT64 同样承载公网 IPv4，不能整段拉黑。"""
        assert ssrf.is_blocked_ip("64:ff9b::808:808") is False  # = 8.8.8.8


class TestMappedPublicAllowed:
    """公网 IPv4 的映射写法必须放行 —— 整段拉黑 ``::ffff:0:0/96`` 会误伤。"""

    @pytest.mark.parametrize("ip", ["::ffff:8.8.8.8", "::ffff:1.1.1.1"])
    def test_ipv4_mapped_public_allowed(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is False, ip

    @pytest.mark.parametrize("ip", ["2606:4700::1111", "2001:4860:4860::8888"])
    def test_plain_public_ipv6_allowed(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is False, ip


class TestNormalizationHelpers:
    """归一化本身的行为，便于日后重写判定时的定位。"""

    def test_ipv4_mapped_uses_ipaddress_property(self) -> None:
        addr = ipaddress.ip_address("::ffff:127.0.0.1")
        assert addr.ipv4_mapped == ipaddress.ip_address("127.0.0.1")
        assert ssrf._normalize_mapped(addr) == ipaddress.ip_address("127.0.0.1")

    def test_nat64_has_no_ipv4_mapped_property(self) -> None:
        """记录这个坑：NAT64 地址的 ``ipv4_mapped`` 是 ``None``。"""
        addr = ipaddress.ip_address("64:ff9b::7f00:1")
        assert addr.ipv4_mapped is None
        assert ssrf._normalize_mapped(addr) == ipaddress.ip_address("127.0.0.1")

    def test_normalize_passes_through_non_mapped(self) -> None:
        assert ssrf._normalize_mapped(
            ipaddress.ip_address("2606:4700::1111")
        ) == ipaddress.ip_address("2606:4700::1111")
        assert ssrf._normalize_mapped(
            ipaddress.ip_address("8.8.8.8")
        ) == ipaddress.ip_address("8.8.8.8")

    def test_prefixes_are_defined(self) -> None:
        assert ssrf.IPV4_MAPPED_PREFIX == ipaddress.ip_network("::ffff:0:0/96")
        assert ssrf.NAT64_PREFIX == ipaddress.ip_network("64:ff9b::/96")
        assert ssrf.IPV4_MAPPED_PREFIX in ssrf.MAPPED_PREFIXES
        assert ssrf.NAT64_PREFIX in ssrf.MAPPED_PREFIXES


class TestExistingSemanticsUnchanged:
    """回归：普通 IPv4 / 纯 IPv6 的既有判定不能被这次改动带跑偏。"""

    @pytest.mark.parametrize(
        "ip",
        ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1",
         "169.254.169.254", "100.64.0.1", "224.0.0.1", "0.0.0.0",
         "::1", "fe80::1", "fc00::1", "::"],
    )
    def test_private_still_blocked(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is True, ip

    @pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "223.5.5.5"])
    def test_public_still_allowed(self, ip: str) -> None:
        assert ssrf.is_blocked_ip(ip) is False, ip

    def test_unparseable_still_fail_closed(self) -> None:
        assert ssrf.is_blocked_ip("not-an-ip") is True
        assert ssrf.is_blocked_ip("999.999.999.999") is True


class TestAllowPrivateHostsSemanticsUnchanged:
    """``allow_private_hosts`` 的逃生舱语义**不**因本次修复收紧（审计 F-07 另议）。"""

    def test_allow_private_still_skips_l3(self, monkeypatch) -> None:
        monkeypatch.setattr(ssrf, "resolve_ips", lambda host: ["::ffff:127.0.0.1"])
        assert ssrf.assert_public_url(
            "http://proxy.example.com/", allow_private=True
        ) == "proxy.example.com"

    def test_default_still_rejects_mapped_loopback(self, monkeypatch) -> None:
        monkeypatch.setattr(ssrf, "resolve_ips", lambda host: ["::ffff:127.0.0.1"])
        with pytest.raises(ssrf.SsrfError):
            ssrf.assert_public_url("http://evil.example.com/")

    def test_l2_host_shape_unchanged(self) -> None:
        """L2 的域名形态判定不在本次改动范围内，回归确认没被带跑偏。"""
        for host in ("localhost", "x.internal", "foo.local", "127.0.0.1"):
            with pytest.raises(ssrf.SsrfError):
                ssrf.check_host_shape(host)
        ssrf.check_host_shape("example.com")  # 不应抛
