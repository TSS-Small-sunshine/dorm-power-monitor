"""SSRF 防护（Q17）—— **4 层**，逐层收紧。

威胁模型
========

``dorm_base_url`` 是**用户在 WebUI 里填的**，而服务端会去请求它。
没有防护的话，攻击者（或一次填错的配置）可以：

::

    http://169.254.169.254/latest/meta-data/iam/security-credentials/
        → 读云厂商元数据，拿到实例的临时凭据
    http://127.0.0.1:6379/        → 探测内网端口
    http://192.168.1.1/admin      → 访问同网段路由器

4 层防护
========

=== ==========================================================
L1  **scheme 白名单** —— 只允许 http / https
L2  **主机名形态检查** —— 禁止裸 IP、``localhost``、``.internal`` 等
L3  **DNS 解析后逐 IP 检查** —— 防 DNS rebinding；命中私有/保留网段即拒
L4  **重定向逐跳复检 + 响应体积上限** —— 防「公网 URL 302 到内网」
=== ==========================================================

L4 在 ``client.py`` 实现（需要真实 HTTP 会话），本模块负责 L1–L3。

L3 为什么必须在**解析之后**
==========================

``http://evil.com`` 的 A 记录可以指向 ``127.0.0.1``。
只检查字符串是拦不住的 —— 必须真的解析出 IP 再判断。

（严格的 DNS rebinding 防护还要「用解析出的 IP 连接、但带原 Host 头」，
那需要自建连接池；本项目的威胁模型是「用户填错 / 被诱导填了内网地址」，
L1–L4 已覆盖。这里如实记录这个边界，而不是假装已经做到。）

逃生舱
======

自建学校代理跑在 LAN 里时，L2/L3 会误伤。
配置项 ``allow_private_hosts``（默认 **false**）可跳过 L2/L3 ——
默认关是**故意**的：需要它的人知道自己在做什么，会主动去打开。
"""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlparse

logger = logging.getLogger("starwatt.scrape")

__all__ = [
    "ALLOWED_SCHEMES",
    "BLOCKED_NETWORKS",
    "SsrfError",
    "assert_public_url",
    "check_host_shape",
    "check_scheme",
    "is_blocked_ip",
    "resolve_ips",
]

#: 禁止访问的网段（私有 / 回环 / 链路本地 / 保留 / 组播 / CGNAT）
BLOCKED_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),  # 本网络
    ipaddress.ip_network("10.0.0.0/8"),  # 私有
    ipaddress.ip_network("100.64.0.0/10"),  # CGNAT
    ipaddress.ip_network("127.0.0.0/8"),  # 回环
    ipaddress.ip_network("169.254.0.0/16"),  # 链路本地（云元数据！）
    ipaddress.ip_network("172.16.0.0/12"),  # 私有
    ipaddress.ip_network("192.0.0.0/24"),  # IETF 协议分配
    ipaddress.ip_network("192.0.2.0/24"),  # TEST-NET-1
    ipaddress.ip_network("192.168.0.0/16"),  # 私有
    ipaddress.ip_network("198.18.0.0/15"),  # 基准测试
    ipaddress.ip_network("198.51.100.0/24"),  # TEST-NET-2
    ipaddress.ip_network("203.0.113.0/24"),  # TEST-NET-3
    ipaddress.ip_network("224.0.0.0/4"),  # 组播
    ipaddress.ip_network("240.0.0.0/4"),  # 保留
    ipaddress.ip_network("255.255.255.255/32"),  # 广播
    ipaddress.ip_network("::/128"),  # 未指定
    ipaddress.ip_network("::1/128"),  # 回环
    ipaddress.ip_network("fc00::/7"),  # 唯一本地
    ipaddress.ip_network("fe80::/10"),  # 链路本地
    ipaddress.ip_network("ff00::/8"),  # 组播
    ipaddress.ip_network("2001:db8::/32"),  # 文档用
)

#: 明确禁止的主机名（小写精确匹配）
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "metadata",  # 某些云的内网别名
    "metadata.google.internal",
})

#: 禁止的主机名后缀
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")

#: 裸 IPv4 字面量
_IPV4_LITERAL = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")

#: 允许的 scheme
ALLOWED_SCHEMES = frozenset({"http", "https"})


class SsrfError(ValueError):
    """URL 被 SSRF 防护拒绝。消息可直接展示给用户（中文）。"""


# ---------------------------------------------------------------------------
# L1 —— scheme
# ---------------------------------------------------------------------------
def check_scheme(url: str) -> str:
    """L1：校验 scheme，返回 hostname。

    Raises:
        SsrfError: scheme 不在白名单，或 URL 缺少主机名。
    """
    if not isinstance(url, str) or not url.strip():
        raise SsrfError("地址不能为空")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise SsrfError(
            f"只允许 http / https 协议，收到 {parsed.scheme or '（空）'}"
        )
    if not parsed.hostname:
        raise SsrfError("地址缺少主机名")
    return parsed.hostname


# ---------------------------------------------------------------------------
# L2 —— 主机名形态
# ---------------------------------------------------------------------------
def check_host_shape(hostname: str) -> None:
    """L2：主机名不得是裸 IP / localhost / 内网域名后缀。

    这一层不需要 DNS，所以**离线可用**（测试友好），也能拦住
    ``http://127.0.0.1`` 这种最直白的探测。

    Raises:
        SsrfError: 命中禁止名单。
    """
    host = (hostname or "").strip().rstrip(".").lower()
    if not host:
        raise SsrfError("地址缺少主机名")

    if host in _BLOCKED_HOSTNAMES:
        raise SsrfError(f"不允许访问 {host}")

    for suffix in _BLOCKED_SUFFIXES:
        if host.endswith(suffix):
            raise SsrfError(f"不允许访问内网域名 {host}")

    if _IPV4_LITERAL.match(host):
        raise SsrfError("请填写域名而不是 IP 地址")

    if ":" in host:  # 裸 IPv6 字面量
        raise SsrfError("请填写域名而不是 IP 地址")


# ---------------------------------------------------------------------------
# L3 —— 解析后的 IP
# ---------------------------------------------------------------------------
def is_blocked_ip(ip: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """该 IP 是否落在禁止网段内（解析不了 → 视为不安全）。"""
    try:
        addr = ipaddress.ip_address(ip) if isinstance(ip, str) else ip
    except ValueError:
        return True
    return any(addr in net for net in BLOCKED_NETWORKS)


def resolve_ips(hostname: str) -> list[str]:
    """解析主机名到 IP 列表（去重、保序）。

    Raises:
        SsrfError: DNS 解析失败或没有可用地址。
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SsrfError(f"域名 {hostname} 无法解析（DNS 查询失败）") from exc

    seen: dict[str, None] = {}
    for info in infos:
        seen.setdefault(info[4][0], None)
    if not seen:
        raise SsrfError(f"域名 {hostname} 没有可用的 IP 地址")
    return list(seen)


def assert_public_url(url: str, *, allow_private: bool = False) -> str:
    """L1 + L2 + L3 一次跑完；返回 hostname。

    Args:
        url: 待校验地址。
        allow_private: 自建 LAN 代理场景下跳过 L2/L3（默认 **false**）。

    Raises:
        SsrfError: 任一层拒绝。
    """
    hostname = check_scheme(url)  # L1

    if allow_private:
        logger.warning(
            "allow_private_hosts 已开启 —— 跳过内网地址检查（%s）", hostname
        )
        return hostname

    check_host_shape(hostname)  # L2

    for ip in resolve_ips(hostname):  # L3
        if is_blocked_ip(ip):
            raise SsrfError(f"域名 {hostname} 解析到内网地址 {ip}，已拒绝访问")
    return hostname
