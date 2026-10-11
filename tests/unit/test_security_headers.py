"""安全响应头 + 会话 Cookie 属性（审计缺陷 1 / 缺陷 2）。

覆盖：

* ``after_request`` 补齐 nosniff / DENY / Referrer-Policy / Permissions-Policy
* HSTS **只在 HTTPS 请求**上出现（HTTP 部署不能发，否则浏览器被钉到 https）
* 已有同名头不被覆盖、不重复添加
* ``session_cookie_secure`` 默认 false；为 true 时 ``Set-Cookie`` 带 ``Secure``
* ``trust_proxy`` 默认 false ⇒ 不启用 ``ProxyFix``；打开才包、且幂等
"""

from __future__ import annotations

import pytest

from starwatt.web import factory

#: 每个响应都必须有的头（值固定，见 factory.SECURITY_HEADERS）
EXPECTED_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "geolocation=(), camera=(), microphone=()",
}

HSTS_NAME = "Strict-Transport-Security"

#: 测试用密码（与 ``tests/conftest.py`` 的 ``GOOD_PASSWORD`` 同值）
TEST_PASSWORD = "Str0ng-Pass!"


def _login_set_cookie(client, username: str = "admin") -> str:
    """登录并取回 ``Set-Cookie`` 原文（Werkzeug 3 的 ``FlaskClient`` 没有 cookie_jar）。"""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200, response.get_json()
    return response.headers.get("Set-Cookie", "")


@pytest.fixture
def isolated_app(tmp_db):
    """一个**独占**的 app —— 供需要改 ``app.config`` / 包 ``wsgi_app`` 的用例用。

    共享的 ``web_app`` 是会话级 fixture，在它身上动 config 或 wsgi_app 会
    串到别的用例上（顺序相关失败）。这里每次新建一个干净实例。
    """
    app = factory.create_app()
    yield app
    app.wsgi_app = getattr(app.wsgi_app, "__wrapped__", app.wsgi_app)



class TestSecurityHeadersPresent:
    """普通 HTTP 请求上的安全头集合。"""

    @pytest.mark.parametrize("path", ["/healthz", "/api/admin/config"])
    def test_headers_on_response(self, admin_client, path: str) -> None:
        response = admin_client.get(path)
        for name, value in EXPECTED_HEADERS.items():
            assert response.headers.get(name) == value, name

    def test_no_duplicate_headers(self, admin_client) -> None:
        """每个头只能出现一次（重复头会让部分代理做错误合并）。"""
        response = admin_client.get("/healthz")
        for name in EXPECTED_HEADERS:
            values = response.headers.getlist(name)
            assert len(values) == 1, (name, values)

    def test_404_also_gets_headers(self, admin_client) -> None:
        """错误响应同样补头 —— after_request 对 4xx/5xx 也生效。"""
        response = admin_client.get("/api/definitely-not-here")
        assert response.status_code == 404
        for name, value in EXPECTED_HEADERS.items():
            assert response.headers.get(name) == value, name

    def test_existing_header_is_not_overwritten(self) -> None:
        """视图自己设过的同名头必须保留原值（只补缺失项）。

        用**独立的最小 Flask app** 而不是共享的 ``web_app`` —— 往共享 app 上
        ``add_url_rule`` 会污染同一测试会话里的其它用例（顺序相关失败）。
        """
        from flask import Flask, make_response

        app = Flask(__name__)
        factory._register_security_headers(app)

        @app.route("/__custom")
        def _custom_header():
            response = make_response("ok")
            response.headers["X-Content-Type-Options"] = "custom-value"
            return response

        response = app.test_client().get("/__custom")
        assert response.headers.get("X-Content-Type-Options") == "custom-value"
        assert response.headers.getlist("X-Content-Type-Options") == ["custom-value"]
        # 其余头仍然被补上（只跳过冲突的那一个）
        assert response.headers.get("X-Frame-Options") == "DENY"

    def test_no_csp_header(self) -> None:
        """本轮刻意不加 CSP（Vue SPA 的内联样式 / modulepreload 会被打坏）。"""
        assert not hasattr(factory, "CSP_HEADER")
        assert "Content-Security-Policy" not in str(factory.SECURITY_HEADERS)


class TestHstsOnlyOnHttps:
    """HSTS 的存在与否必须跟随 ``request.is_secure``。"""

    def test_absent_on_plain_http(self, admin_client) -> None:
        response = admin_client.get("/healthz")
        assert HSTS_NAME not in response.headers

    def test_present_on_https(self, admin_client) -> None:
        response = admin_client.get("/healthz", base_url="https://localhost")
        assert HSTS_NAME in response.headers
        assert "max-age=" in response.headers[HSTS_NAME]

    def test_https_still_has_the_other_headers(self, admin_client) -> None:
        response = admin_client.get("/healthz", base_url="https://localhost")
        for name, value in EXPECTED_HEADERS.items():
            assert response.headers.get(name) == value, name


class TestSessionCookieSecureDefault:
    """缺陷 1 的核心：``session_cookie_secure`` 默认 **false**。"""

    def test_secure_disabled_by_default(self, web_app) -> None:
        assert web_app.config["SESSION_COOKIE_SECURE"] is False

    def test_httponly_and_samesite_are_set(self, web_app) -> None:
        assert web_app.config["SESSION_COOKIE_HTTPONLY"] is True
        assert web_app.config["SESSION_COOKIE_SAMESITE"] == "Lax"

    def test_plain_http_cookie_has_no_secure_attribute(self, web_app) -> None:
        """HTTP 部署下带 Secure 会让登录直接失效 —— 这里守住默认行为。"""
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user("admin", TEST_PASSWORD, role=ROLE_ADMIN)
        header = _login_set_cookie(web_app.test_client())
        assert "dorm_session=" in header
        assert "Secure" not in header
        assert "HttpOnly" in header
        assert "SameSite=Lax" in header


class TestSessionCookieSecureEnabled:
    """把配置打开后，登录下发的 Cookie 必须带 Secure。"""

    def test_cookie_carries_secure_when_enabled(self, web_app, monkeypatch) -> None:
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user("admin", TEST_PASSWORD, role=ROLE_ADMIN)
        # ``web_app`` 是会话级 fixture，改 config 必须经 monkeypatch 复原，
        # 否则会污染同一会话里断言「默认 false」的用例。
        monkeypatch.setitem(web_app.config, "SESSION_COOKIE_SECURE", True)

        header = _login_set_cookie(web_app.test_client())
        assert "dorm_session=" in header
        assert "Secure" in header

    def test_cookie_has_no_secure_when_disabled(self, web_app) -> None:
        """对照组：同一套登录链路，配置关闭时**不能**出现 Secure。"""
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user("admin", TEST_PASSWORD, role=ROLE_ADMIN)
        assert web_app.config["SESSION_COOKIE_SECURE"] is False

        header = _login_set_cookie(web_app.test_client())
        assert "dorm_session=" in header
        assert "Secure" not in header

    def test_create_app_reads_secure_from_config(self, web_app) -> None:
        """``create_app(config=...)`` 能覆盖（测试注入路径）。"""
        app = factory.create_app(config={"SESSION_COOKIE_SECURE": True})
        assert app.config["SESSION_COOKIE_SECURE"] is True

    def test_default_comes_from_registry(self, web_app) -> None:
        """不注入时必须来自配置中心，且默认值是 false。"""
        from starwatt.config_registry import get_bool
        from starwatt.config_registry.registry import REGISTRY

        setting = REGISTRY["session_cookie_secure"]
        assert setting.default is False
        assert get_bool("session_cookie_secure", False) is False


class TestTrustProxyOffByDefault:
    """``trust_proxy`` 默认 false ⇒ **不**启用 ProxyFix（否则来源 IP 可伪造）。"""

    def test_disabled_by_default(self, web_app) -> None:
        assert web_app.config["TRUST_PROXY"] is False
        assert not getattr(web_app.wsgi_app, "_starwatt_proxy_fix", False)

    def test_forwarded_for_is_ignored_by_default(self, web_app) -> None:
        client = web_app.test_client()
        response = client.get(
            "/healthz", headers={"X-Forwarded-For": "1.2.3.4"}
        )
        assert response.status_code == 200

    def test_init_proxy_fix_wraps_when_enabled(self, isolated_app) -> None:
        isolated_app.config["TRUST_PROXY"] = True
        assert factory.init_proxy_fix(isolated_app) is True
        assert getattr(isolated_app.wsgi_app, "_starwatt_proxy_fix", False)

    def test_proxy_fix_is_idempotent(self, isolated_app) -> None:
        isolated_app.config["TRUST_PROXY"] = True
        factory.init_proxy_fix(isolated_app)
        wrapped = isolated_app.wsgi_app
        assert factory.init_proxy_fix(isolated_app) is True
        assert isolated_app.wsgi_app is wrapped  # 不叠加第二层

    def test_returns_false_when_disabled(self, isolated_app) -> None:
        isolated_app.config["TRUST_PROXY"] = False
        assert factory.init_proxy_fix(isolated_app) is False

    def test_create_app_enables_proxy_fix_when_configured(self, tmp_db) -> None:
        """``create_app()`` 必须在**不依赖**任何已存在的库的前提下可用。

        这里显式要 ``tmp_db``：本用例原本裸调 ``create_app()``，于是读配置时
        用的是默认库路径 —— 开发机上有 ``records.db`` 所以本地永远绿，CI 上
        （干净检出）当场 ``no such table: meta``。
        """
        app = factory.create_app(config={"TRUST_PROXY": True})
        assert getattr(app.wsgi_app, "_starwatt_proxy_fix", False)

    def test_registry_defaults_are_false(self) -> None:
        from starwatt.config_registry.registry import REGISTRY

        assert REGISTRY["trust_proxy"].default is False
