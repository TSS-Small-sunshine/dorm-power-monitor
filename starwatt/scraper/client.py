"""HTTP 客户端 —— 实现 :class:`starwatt.protocols.FetchContext`。

三件事
======

**1. 请求伪装（A9）**
学校接口只对**微信内置浏览器**放行，所以必须带：

* ``User-Agent`` —— 微信 iOS 的 UA（可配，见 ``scrape_user_agent``）
* ``Referer`` —— 指向 ``/finduser``
* ``X-Requested-With: XMLHttpRequest``

这三项少任何一个都可能被 WAF 拦掉。

**2. 重试与退避（A10）**
**区分**「传输层错误」与「HTTP 状态码错误」：

=======================================  ==================================
情况                                     处理
=======================================  ==================================
连接失败 / 超时 / DNS 失败                **重试**（指数退避）
HTTP 5xx                                 **重试**（服务端可能只是抖了一下）
HTTP 4xx                                 **不重试**（客户端错了，重试没用）
=======================================  ==================================

旧代码对两者一视同仁地重试 3 次 —— 401 也会白等 3 轮。

**3. SSRF 第 4 层（L4）**
**手动处理重定向**，每一跳都重新过一遍 ``assert_public_url``。
这样 ``https://public.example.com`` 302 到 ``http://169.254.169.254/``
会被拦在第 2 跳。

同时限制响应体积 —— 防止对方返回一个超大流把内存吃光。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from starwatt.scraper.ssrf import SsrfError, assert_public_url

logger = logging.getLogger("starwatt.scrape")

__all__ = [
    "DEFAULT_TIMEOUT_SEC",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "HttpResult",
    "ScrapeError",
    "SchoolClient",
    "same_host",
]

#: 默认超时（秒）—— 会被 ``scrape_timeout_sec`` 覆盖
DEFAULT_TIMEOUT_SEC = 15

#: 最大重定向跳数（L4）
MAX_REDIRECTS = 3

#: 响应体上限（字节）—— 电量接口的响应只有几 KB，2 MB 已非常宽松
MAX_BYTES = 2 * 1024 * 1024

#: 触发重试的状态码
_RETRY_STATUS = frozenset({500, 502, 503, 504})

#: 视为重定向的状态码
_REDIRECT_STATUS = frozenset({301, 302, 303, 307, 308})

#: ``finduser`` 的完整路径 —— **Referer 必须指向它**，否则 WAF 会拒绝。
#: 这里是唯一真相：``endpoints.py`` 的 A1 房间发现也复用这个常量。
FINDUSER_PATH = "/campus/webchat/dormEmRealRead/finduser"


class ScrapeError(RuntimeError):
    """抓取失败（网络 / 状态码 / 解析）。消息可直接进日志与卡片。"""


@dataclass(frozen=True, slots=True)
class HttpResult:
    """一次成功请求的结果。"""

    url: str
    status: int
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        """解析为 JSON。

        Raises:
            ValueError: 不是合法 JSON。
        """
        import json as _json

        return _json.loads(self.text)


def same_host(a: str, b: str) -> bool:
    """两个 URL 是否同主机。"""
    return urlparse(a).hostname == urlparse(b).hostname


class SchoolClient:
    """学校接口客户端（``FetchContext`` 的实现）。

    刻意**不继承** ``requests.Session`` —— 组合而非继承，测试里可以注入
    一个假 session，完全不碰真实网络（``no_network`` fixture）。
    """

    def __init__(
        self,
        base_url: str,
        *,
        user_agent: str = "",
        timeout_sec: int = DEFAULT_TIMEOUT_SEC,
        retry_count: int = 3,
        allow_private_hosts: bool = False,
        session: requests.Session | None = None,
        sleeper=time.sleep,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.user_agent = user_agent
        self.timeout_sec = timeout_sec
        self.retry_count = max(0, retry_count)
        self.allow_private_hosts = allow_private_hosts
        self._session = session or requests.Session()
        self._sleep = sleeper  # 测试注入，避免真的睡

    # -- 对外 API（FetchContext 协议）------------------------------------
    def get(self, path: str, **kwargs: Any) -> str:
        """GET 并返回响应文本。"""
        return self.request("GET", path, **kwargs).text

    def post_form(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        """POST 表单并返回 JSON（学校接口全部返回 JSON）。"""
        result = self.request("POST", path, data=data)
        try:
            payload = result.json()
        except ValueError as exc:
            raise ScrapeError(
                f"{path} 返回的不是 JSON（前 120 字符：{result.text[:120]!r}）"
            ) from exc
        if not isinstance(payload, dict):
            raise ScrapeError(f"{path} 返回的 JSON 不是对象")
        return payload

    # -- 内部 -------------------------------------------------------------
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """A9：微信 UA + Referer + X-Requested-With。

        ``Referer`` 必须指向**完整的** finduser 路径 —— 学校 WAF 会校验它，
        少了 ``/campus/webchat`` 前缀会被直接拒绝。
        """
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base_url}{FINDUSER_PATH}",
            # 学校接口还校验 Origin（legacy ``_fetch_data`` 也发了它）
            "Origin": self.base_url,
        }
        if self.user_agent:
            headers["User-Agent"] = self.user_agent
        if extra:
            headers.update(extra)
        return headers

    def request(
        self,
        method: str,
        path: str,
        *,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResult:
        """发一次请求，带重试、退避、重定向复检、体积上限。

        Raises:
            ScrapeError: 重试耗尽或遇到不可重试的错误。
            SsrfError: SSRF 防护拒绝（**不重试** —— 换几次都一样）。
        """
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        # 每次请求前重新校验（DNS 记录可能已经变了）
        assert_public_url(url, allow_private=self.allow_private_hosts)

        attempt = 0
        last_error = "未知错误"
        while attempt <= self.retry_count:
            if attempt:
                backoff = min(2 ** (attempt - 1), 8)  # 1s, 2s, 4s, 8s 封顶
                logger.debug("第 %d 次重试 %s，等待 %ds", attempt, path, backoff)
                self._sleep(backoff)
            attempt += 1

            try:
                result = self._request_once(method, url, data, headers)
            except SsrfError:
                raise  # 安全拒绝：重试没有意义
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    "抓取 %s 传输层失败（第 %d/%d 次）：%s",
                    path, attempt, self.retry_count + 1, last_error,
                )
                continue

            if result.status in _RETRY_STATUS:
                last_error = f"HTTP {result.status}"
                logger.warning(
                    "抓取 %s 返回 %d（第 %d/%d 次）",
                    path, result.status, attempt, self.retry_count + 1,
                )
                continue

            if not result.ok:
                # 4xx：客户端错误，重试无意义 —— 立刻失败
                raise ScrapeError(f"{path} 返回 HTTP {result.status}")

            return result

        raise ScrapeError(
            f"{path} 重试 {self.retry_count + 1} 次后仍失败：{last_error}"
        )

    def _request_once(
        self,
        method: str,
        url: str,
        data: dict[str, Any] | None,
        headers: dict[str, str] | None,
    ) -> HttpResult:
        """发一次请求（**手动跟随重定向**，每跳复检 SSRF）。"""
        current = url
        current_method, current_data = method, data

        for hop in range(MAX_REDIRECTS + 1):
            response = self._session.request(
                current_method,
                current,
                data=current_data,
                headers=self._headers(headers),
                timeout=self.timeout_sec,
                allow_redirects=False,  # L4：自己跟，逐跳复检
                stream=True,  # 便于限制读取体积
            )
            try:
                if response.status_code in _REDIRECT_STATUS:
                    location = response.headers.get("Location")
                    if not location:
                        raise ScrapeError(
                            f"{current} 返回重定向但没有 Location 头"
                        )
                    if hop >= MAX_REDIRECTS:
                        raise ScrapeError(f"重定向超过 {MAX_REDIRECTS} 跳，已放弃")

                    target = urljoin(current, location)
                    # 🔑 每一跳都重新过 SSRF —— 这是 L4 的全部意义
                    assert_public_url(
                        target, allow_private=self.allow_private_hosts
                    )
                    logger.debug("重定向 %s → %s", current, target)
                    current = target
                    if response.status_code == 303:  # 语义上要改成 GET
                        current_method, current_data = "GET", None
                    continue

                return HttpResult(
                    url=current,
                    status=response.status_code,
                    text=self._read_capped(response, current),
                )
            finally:
                response.close()

        raise ScrapeError(f"{url} 重定向处理异常")

    @staticmethod
    def _read_capped(response: requests.Response, url: str) -> str:
        """按块读取，超过 :data:`MAX_BYTES` 立即中止。

        为什么要流式读：``response.text`` 会把整个响应体读进内存，
        对方返回一个超大流就能把进程撑爆。
        """
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            total += len(chunk)
            if total > MAX_BYTES:
                raise ScrapeError(
                    f"{url} 响应体超过 {MAX_BYTES // 1024 // 1024} MB，已中止"
                )
            chunks.append(chunk)

        raw = b"".join(chunks)
        encoding = response.encoding or "utf-8"
        try:
            return raw.decode(encoding, errors="replace")
        except LookupError:
            return raw.decode("utf-8", errors="replace")

    def close(self) -> None:
        """关闭底层会话。"""
        try:
            self._session.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不该影响主流程
            pass
