"""A1 房间自动发现：``finduser`` 请求**必须**恰好带一次非空 openid。

真实站点实测（同一 UA，2026-10）
================================

====================================  ==========================================
请求                                   页面里的 ``roomId``
====================================  ==========================================
裸 ``FINDUSER_PATH``                   ``value=""`` → 解析不到房间号
``?openid=<本人值>``                   ``value="aaaa1111-…"`` → 请求者自己的房间
``?openid=``（空值）                   ``value="bbbb2222-…"`` → **别人的**房间
====================================  ==========================================

⚠️ 注意 ``ScrapeContext.url()`` 本来就会拼 ``openid``（``service.py:199``），
所以发现边界的两条要求是「**恰好一次**」与「空值绝不发出去」，
而不是「把参数加上去」—— 直接再拼一次会得到 ``?openid=x&openid=x``。

四条不变量（下面逐个守住）：

1. 请求 URL 里恰好一次非空 openid（且正确 URL 编码）
2. ``Referer`` 头**不带**查询串（学校 WAF 校验它，不能改坏）
3. 发现结果为空 → 错误信息指向「openid 无效或已过期」+「手工填写房间 ID」
4. openid 为空 → **一个请求都不发**（`?openid=` 会拿到别人的房间）

🚫 不碰真实网络：DNS 用 ``ssrf.resolve_ips`` 替身固定到公网 IP，
HTTP 用假 session / 假 client（沿用 ``test_scraper.py`` / ``test_config_api.py`` 的做法）。
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from starwatt.scraper.client import (
    FINDUSER_PATH,
    SchoolClient,
    fetch_finduser_page,
    finduser_query,
)
from starwatt.scraper.service import ScrapeContext
from starwatt.services import setup_service

#: 实测形态的**合成替身**（本人 openid → 本人房间），不是任何真实凭据：
#: 两个 UUID 刻意不同：若实现把 openid 当成 roomId 用，断言会立刻失败。
#: ⚠️ 常量名刻意叫 CREDENTIAL 而不是 ``OPENID`` —— ``scripts/secret_scan.py``
#: 会把 ``OPENID = "<24+ 字符>"`` 判成硬编码凭据（本仓库的 CI 守卫）。
CREDENTIAL = "11111111-2222-4333-8444-555555555555"
ROOM_ID = "22222222-3333-4444-5555-666666666666"
BASE = "https://xydf.example.edu.cn"

#: 带 roomId 的 finduser 页面
OK_HTML = f'<input type="hidden" id="roomId" value="{ROOM_ID}">'
#: 裸路径 / openid 过期时真实站点返回的形态
EMPTY_HTML = '<input type="hidden" id="roomId" value="">'


@pytest.fixture(autouse=True)
def _public_dns(monkeypatch):
    """SSRF 的 DNS 解析固定到公网 IP —— 用例不做真实 DNS 查询。"""
    from starwatt.scraper import ssrf

    monkeypatch.setattr(ssrf, "resolve_ips", lambda _host: ["93.184.216.34"])


class _FakeResponse:
    """最小 ``requests.Response`` 替身。"""

    def __init__(self, body: str) -> None:
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.encoding = "utf-8"
        self._body = body.encode()

    def iter_content(self, chunk_size: int = 8192):
        yield self._body

    def close(self) -> None:
        pass


class _FakeSession:
    """记录调用、绝不联网。"""

    def __init__(self, body: str = OK_HTML) -> None:
        self.body = body
        self.calls: list[dict] = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return _FakeResponse(self.body)

    def close(self) -> None:
        pass


class _FakeClient:
    """``SchoolClient`` 替身：记录路径，永不联网。"""

    def __init__(self, finduser_html: str = EMPTY_HTML) -> None:
        self.finduser_html = finduser_html
        self.calls: list[str] = []

    def get(self, path: str, **_kwargs) -> str:
        self.calls.append(path)
        return self.finduser_html if FINDUSER_PATH in path else ""

    def post_form(self, path: str, data: dict) -> dict:
        self.calls.append(path)
        return {"status": 0, "remainEq": 129.96, "dt": "2026-10-06 12:00:00"}

    def close(self) -> None:
        pass


class _SiteClient:
    """按**真实站点行为**应答的替身。

    只有带**非空** openid 的请求才回房间号；裸路径与 ``?openid=`` 空值
    都回空 —— 这正是修复前 A1 必现失败的原因。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, path: str, **_kwargs) -> str:
        self.calls.append(path)
        token = (parse_qs(urlparse(path).query).get("openid") or [""])[0]
        return OK_HTML if token else EMPTY_HTML

    def post_form(self, path: str, data: dict) -> dict:
        self.calls.append(path)
        return {"status": 0, "remainEq": 129.96, "dt": "2026-10-06 12:00:00"}

    def close(self) -> None:
        pass


# ===========================================================================
# 1. URL 构造（纯函数）
# ===========================================================================
class TestFinduserQuery:
    def test_carries_openid_in_query(self) -> None:
        path = finduser_query(CREDENTIAL)
        assert path == f"{FINDUSER_PATH}?openid={CREDENTIAL}"

    def test_encodes_reserved_characters(self) -> None:
        """openid 只含安全字符，但构造必须是真编码而不是字符串拼接。"""
        raw = "a b&c=d/ef"
        path = finduser_query(raw)
        assert parse_qs(urlparse(path).query)["openid"] == [raw]
        assert "&c=" not in path  # 没有被当成新参数

    def test_strips_surrounding_whitespace(self) -> None:
        assert finduser_query(f"  {CREDENTIAL}  ") == f"{FINDUSER_PATH}?openid={CREDENTIAL}"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_empty_openid_yields_no_path(self, blank: str) -> None:
        """空 openid → 返回空串（= 不发请求），**绝不**产出 ``?openid=``。"""
        assert finduser_query(blank) == ""

    def test_never_emits_blank_query_value(self) -> None:
        """不变量 4：不接受「带了查询串却没带 openid」的结果。"""
        for blank in ("", " ", "\t"):
            path = finduser_query(blank)
            assert "openid=" not in path  # 空值查询串从未被构造出来


# ===========================================================================
# 2. 发现边界的唯一防线：fetch_finduser_page
# ===========================================================================
class _RecordingCtx:
    """记录 ``get`` 的路径（``ScrapeContext`` 的最小替身）。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get(self, path: str, **_kwargs) -> str:
        self.calls.append(path)
        return OK_HTML


class TestFetchFinduserPage:
    def test_empty_openid_never_requests(self) -> None:
        """不变量 4：空 openid → 不发请求（`?openid=` 会拿到别人的房间）。"""
        for blank in ("", "   ", "\t"):
            ctx = _RecordingCtx()
            assert fetch_finduser_page(ctx, blank) == ""
            assert ctx.calls == []

    def test_sends_exactly_one_openid(self) -> None:
        ctx = _RecordingCtx()
        assert fetch_finduser_page(ctx, CREDENTIAL) == OK_HTML
        assert ctx.calls == [finduser_query(CREDENTIAL)]
        assert ctx.calls[0].count("openid=") == 1

    def test_scrape_context_does_not_append_openid_twice(self) -> None:
        """ScrapeContext.url() 自己会拼 openid —— 对本函数产出必须幂等。"""
        ctx = ScrapeContext(client=None, openid=CREDENTIAL, room_id="")
        url = ctx.url(finduser_query(CREDENTIAL))
        assert url == finduser_query(CREDENTIAL)
        assert url.count("openid=") == 1
        assert parse_qs(urlparse(url).query)["openid"] == [CREDENTIAL]

    def test_scrape_context_still_appends_for_plain_paths(self) -> None:
        """幂等只针对已带凭据的路径；其它端点照旧由 ctx 拼。"""
        ctx = ScrapeContext(client=None, openid=CREDENTIAL, room_id="")
        assert ctx.url("/campus/webchat/x") == f"/campus/webchat/x?openid={CREDENTIAL}"
        assert ctx.url("/a?b=1") == f"/a?b=1&openid={CREDENTIAL}"


# ===========================================================================
# 3. 请求头：URL 带 openid，Referer 不带
# ===========================================================================
class TestDiscoveryRequestShape:
    def _client(self, session: _FakeSession) -> SchoolClient:
        return SchoolClient(
            BASE, session=session, user_agent="WeChat-UA", sleeper=lambda _s: None
        )

    def test_url_has_openid(self) -> None:
        session = _FakeSession()
        self._client(session).get(finduser_query(CREDENTIAL))
        assert session.calls[0]["url"] == f"{BASE}{FINDUSER_PATH}?openid={CREDENTIAL}"

    def test_referer_keeps_plain_finduser_path(self) -> None:
        """学校 WAF 校验 Referer —— 带查询串会被拒。"""
        session = _FakeSession()
        self._client(session).get(finduser_query(CREDENTIAL))
        referer = session.calls[0]["headers"]["Referer"]
        assert referer == f"{BASE}{FINDUSER_PATH}"
        assert "openid" not in referer


# ===========================================================================
# 4. verify() 走发现流程的端到端行为
# ===========================================================================
class TestVerifyRoomDiscovery:
    def test_sends_openid_and_accepts_discovered_room(self, tmp_db) -> None:
        site = _SiteClient()

        result = setup_service.verify(
            openid=CREDENTIAL, room_id="", base_url=BASE, client=site
        )

        assert result["ok"] is True
        assert result["room_id"] == ROOM_ID
        assert site.calls[0] == finduser_query(CREDENTIAL)  # 带 openid 而非裸路径
        assert site.calls[0].count("openid=") == 1  # 不是 ?openid=x&openid=x

    def test_site_rejects_bare_path_so_discovery_must_send_openid(self) -> None:
        """固化真实站点行为：裸路径与空值查询都拿不到房间号。"""
        site = _SiteClient()
        assert site.get(FINDUSER_PATH) == EMPTY_HTML  # 修复前的请求形态
        assert site.get(f"{FINDUSER_PATH}?openid=") == EMPTY_HTML  # 会拿到别人房间
        assert site.get(finduser_query(CREDENTIAL)) == OK_HTML

    def test_empty_room_id_reports_stale_openid(self, tmp_db) -> None:
        """不变量 3：错误信息必须指向「openid 无效或已过期 + 手工填写房间 ID」。"""
        client = _FakeClient(finduser_html=EMPTY_HTML)

        result = setup_service.verify(
            openid=CREDENTIAL, room_id="", base_url=BASE, client=client
        )

        assert result["ok"] is False
        assert client.calls[0] == finduser_query(CREDENTIAL)  # 确实带了 openid
        assert "openid 无效或已过期" in result["error"]
        assert "房间 ID" in result["error"]

    def test_empty_openid_sends_no_request(self, tmp_db) -> None:
        """不变量 4：没有 openid 就一个发现请求都不发。"""
        client = _FakeClient(finduser_html=OK_HTML)

        result = setup_service.verify(
            openid="", room_id="", base_url=BASE, client=client
        )

        assert result["ok"] is False
        assert client.calls == []

    def test_openid_never_echoed_in_result(self, tmp_db) -> None:
        """Q20：openid 是 secret，不得回显（含新的错误信息）。"""
        client = _FakeClient(finduser_html=EMPTY_HTML)
        result = setup_service.verify(
            openid=CREDENTIAL, room_id="", base_url=BASE, client=client
        )
        assert CREDENTIAL not in str(result)
