"""配置 / 引导 / 自助配置 API（M4 §4.2 / §4.4 / §4.5 / §4.6）。

覆盖四块
========

=====================  =====================================================
被测对象                要点
=====================  =====================================================
``admin_api``          七端点齐全 + 仅管理员 + CSRF + secret 脱敏
``auth_api``           登录 / 登出 / 改密 + 路由守卫用的 ``/me``
``oobe_api``           5 步（B4 删掉建号步）+ 端点 10 → 2
``setup_api``          SSRF 4 层防护 + 不落库的 verify
``portable``           导出导入（脱敏值跳过、未知键跳过、STATE 不可导入）
=====================  =====================================================
"""
from __future__ import annotations

import json

import pytest

from starwatt.config_registry import get_float, get_str, set_many, set_state
from starwatt.config_registry.portable import export_config, import_config
from starwatt.services import setup_service

GOOD_PASSWORD = "Str0ng-Pass!"


def _login_as(web_app, username: str, password: str = GOOD_PASSWORD, *, role=None, must_change=False):
    """建号 + 真实登录，返回 ``(client, token)``（CSRF 头已就位）。

    与 conftest 的 ``admin_client`` 同一做法：走真实登录端点，顺带覆盖登录链路。
    """
    from starwatt.auth import csrf as csrf_mod
    from starwatt.auth import service as auth
    from starwatt.auth.constants import ROLE_ADMIN

    auth.create_user(
        username,
        password,
        role=role or ROLE_ADMIN,
        must_change_password=must_change,
    )
    client = web_app.test_client()
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf_mod.issue(token)
    return client, token


@pytest.fixture
def public_dns(monkeypatch):
    """把 SSRF 的 DNS 解析替换成固定公网 IP。

    测试机（CI / 无网环境）不能真做 DNS 查询 —— 这是 ``test_scraper.py``
    已确立的做法（``monkeypatch.setattr(ssrf, "resolve_ips", ...)``）。
    """
    from starwatt.scraper import ssrf

    monkeypatch.setattr(ssrf, "resolve_ips", lambda _host: ["93.184.216.34"])
    yield


# ===========================================================================
# 配置 API（Q15 七端点）
# ===========================================================================
class TestConfigApiAccess:
    def test_anonymous_is_rejected(self, web_app) -> None:
        client = web_app.test_client()
        for path in (
            "/api/admin/config/schema",
            "/api/admin/config",
            "/api/admin/config/export",
            "/api/admin/users",
            "/api/admin/audit",
        ):
            response = client.get(path)
            assert response.status_code == 401, path
            assert response.get_json()["error"] == "unauthorized"

    def test_viewer_is_forbidden(self, viewer_client) -> None:
        """普通用户不能读配置（配置里有 secret 元数据与运行时开关）。"""
        response = viewer_client.get("/api/admin/config")
        assert response.status_code == 403
        assert response.get_json()["error"] == "forbidden"

    def test_write_requires_csrf(self, admin_client) -> None:
        """管理员也要过 CSRF —— 少了它就是「第三方页面能改你的配置」。"""
        response = admin_client.put(
            "/api/admin/config",
            json={"eqprice": 0.6},
            headers={"X-CSRF-Token": ""},
        )
        assert response.status_code == 403
        assert response.get_json()["error"] == "csrf_failed"


class TestConfigSchema:
    def test_schema_covers_every_group(self, admin_client) -> None:
        """计划书要求 ≥8 个分组（M3 新增 QQ 分组后为 9 个）。"""
        payload = admin_client.get("/api/admin/config/schema").get_json()
        keys = [group["key"] for group in payload["groups"]]
        assert len(keys) >= 8
        assert len(keys) == len(set(keys))
        assert payload["total"] == sum(len(g["settings"]) for g in payload["groups"])

    def test_every_setting_has_frontend_metadata(self, admin_client) -> None:
        """前端**只**靠这份元数据渲染表单（Q15：所有配置 WebUI 可改）。"""
        payload = admin_client.get("/api/admin/config/schema").get_json()
        for group in payload["groups"]:
            for setting in group["settings"]:
                assert {"key", "label", "type", "default", "kind"} <= set(setting)


class TestConfigValues:
    def test_secrets_are_masked_by_default(self, admin_client) -> None:
        admin_client.put("/api/admin/config", json={"dorm_openid": "secret-openid-123"})
        payload = admin_client.get("/api/admin/config").get_json()
        value = payload["values"]["dorm_openid"]
        assert value.startswith("••••")
        assert "secret-openid-123" not in json.dumps(payload)

    def test_include_secrets_flag_reveals_plaintext(self, admin_client) -> None:
        """导出 / 迁移场景才需要明文 —— 必须显式请求。"""
        admin_client.put("/api/admin/config", json={"dorm_openid": "secret-openid-123"})
        payload = admin_client.get("/api/admin/config?include_secrets=1").get_json()
        assert payload["values"]["dorm_openid"] == "secret-openid-123"


class TestConfigPut:
    def test_valid_update_is_applied(self, admin_client) -> None:
        response = admin_client.put("/api/admin/config", json={"eqprice": 0.62})
        assert response.status_code == 200
        assert response.get_json()["applied"] == ["eqprice"]
        assert get_float("eqprice") == pytest.approx(0.62)

    def test_invalid_value_is_rejected_with_message(self, admin_client) -> None:
        response = admin_client.put("/api/admin/config", json={"eqprice": 999})
        assert response.status_code == 207
        assert "eqprice" in response.get_json()["errors"]

    def test_partial_success_does_not_block_other_keys(self, admin_client) -> None:
        """逐键独立：一个非法值不该让整份表单白填。"""
        response = admin_client.put(
            "/api/admin/config", json={"eqprice": 999, "quiet_hours_start": "23:30"}
        )
        assert response.status_code == 207
        body = response.get_json()
        assert body["applied"] == ["quiet_hours_start"]
        assert get_str("quiet_hours_start") == "23:30"

    def test_state_keys_are_not_writable(self, admin_client) -> None:
        """``STATE``（游标 / 时间戳）不能被用户改 —— 否则抓取历史会错乱。"""
        response = admin_client.put(
            "/api/admin/config", json={"last_scrape_at": "2030-01-01"}
        )
        assert response.status_code == 207
        assert "last_scrape_at" in response.get_json()["errors"]

    def test_update_writes_audit(self, admin_client) -> None:
        admin_client.put("/api/admin/config", json={"eqprice": 0.7})
        entries = admin_client.get("/api/admin/audit?action=config.update").get_json()
        assert entries["entries"]
        assert entries["entries"][0]["action"] == "config.update"

    def test_audit_never_contains_secret(self, admin_client) -> None:
        """Q20 铁律：审计里不得出现 secret。"""
        admin_client.put("/api/admin/config", json={"dorm_openid": "top-secret-openid"})
        body = admin_client.get("/api/admin/audit").get_json()
        assert "top-secret-openid" not in json.dumps(body)


class TestConfigTest:
    def test_unknown_target_is_rejected(self, admin_client) -> None:
        response = admin_client.post("/api/admin/config/test", json={"target": "telepathy"})
        assert response.status_code == 400
        assert "未知的测试目标" in response.get_json()["error"]

    def test_feishu_without_webhook_reports_missing_config(self, admin_client) -> None:
        """没配 webhook 时说清楚「缺什么」，而不是笼统的「失败」。"""
        response = admin_client.post(
            "/api/admin/config/test", json={"target": "feishu_group"}
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "未配置 webhook 地址"


class TestConfigExportImportReset:
    def test_export_has_format_and_no_secrets(self, admin_client) -> None:
        admin_client.put("/api/admin/config", json={"dorm_openid": "abc-secret"})
        payload = admin_client.get("/api/admin/config/export").get_json()
        assert payload["format"] == "starwatt-config"
        assert payload["include_secrets"] is False
        assert payload["config"]["dorm_openid"].startswith("••••")

    def test_import_roundtrip_applies_values(self, admin_client) -> None:
        admin_client.put("/api/admin/config", json={"quiet_hours_start": "22:45"})
        exported = admin_client.get("/api/admin/config/export").get_json()
        admin_client.post("/api/admin/config/reset")
        assert get_str("quiet_hours_start") != "22:45"

        response = admin_client.post(
            "/api/admin/config/import", json={"payload": exported}
        )
        assert response.status_code == 200
        assert "quiet_hours_start" in response.get_json()["applied"]
        assert get_str("quiet_hours_start") == "22:45"

    def test_reset_restores_defaults_but_keeps_state(self, admin_client) -> None:
        """重置配置**不该**让抓取历史「回到过去」。"""
        admin_client.put("/api/admin/config", json={"eqprice": 0.99})
        set_state("last_scrape_at", "2026-10-06T12:00:00+08:00")
        body = admin_client.post("/api/admin/config/reset").get_json()
        assert body["reset"] >= 1
        assert get_float("eqprice") == pytest.approx(0.5)
        assert get_str("last_scrape_at") == "2026-10-06T12:00:00+08:00"

    def test_import_rejects_foreign_json(self, admin_client) -> None:
        response = admin_client.post(
            "/api/admin/config/import",
            json={"payload": {"format": "someone-elses", "config": {}}},
        )
        assert response.status_code == 207
        assert response.get_json()["errors"]


class TestPortable:
    def test_export_excludes_state_and_cache(self, tmp_db) -> None:
        payload = export_config()
        assert "last_scrape_at" not in payload["config"]
        assert "oobe_step" not in payload["config"]

    def test_masked_secret_is_skipped_not_written_back(self, tmp_db) -> None:
        """最容易犯的错：把 ``••••1234`` 当成真凭据写回去。"""
        set_many({"dorm_openid": "real-openid"})
        payload = export_config()  # 默认脱敏
        set_many({"dorm_openid": "changed-openid"})
        result = import_config(payload)
        assert "dorm_openid" in result["skipped"]
        assert get_str("dorm_openid") == "changed-openid"

    def test_plaintext_secret_requires_opt_in(self, tmp_db) -> None:
        set_many({"dorm_openid": "real-openid"})
        payload = export_config(include_secrets=True)
        set_many({"dorm_openid": "changed-openid"})

        denied = import_config(payload)
        assert "dorm_openid" in denied["skipped"]
        assert get_str("dorm_openid") == "changed-openid"

        allowed = import_config(payload, allow_secrets=True)
        assert "dorm_openid" in allowed["applied"]
        assert get_str("dorm_openid") == "real-openid"

    def test_unknown_keys_are_skipped_without_breaking_the_rest(self, tmp_db) -> None:
        result = import_config({"config": {"eqprice": 0.55, "ancient_key": 1}})
        assert result["applied"] == ["eqprice"]
        assert result["skipped"] == ["ancient_key"]
        assert get_float("eqprice") == pytest.approx(0.55)

    def test_non_config_kind_is_skipped(self, tmp_db) -> None:
        result = import_config({"config": {"oobe_step": 3}})
        assert result["skipped"] == ["oobe_step"]

    def test_bad_payload_shapes_are_reported(self) -> None:
        assert import_config("not-a-dict")["errors"]
        assert import_config({"config": []})["errors"]

    def test_invalid_value_goes_to_errors(self, tmp_db) -> None:
        result = import_config({"config": {"eqprice": "not-a-number"}})
        assert "eqprice" in result["errors"]


# ===========================================================================
# 认证 API（§4.2）
# ===========================================================================
class TestAuthApi:
    def test_me_is_anonymous_friendly(self, web_app) -> None:
        """路由守卫靠它判断登录态，所以**不能**返回 401。"""
        body = web_app.test_client().get("/api/auth/me").get_json()
        assert body == {"ok": False, "authenticated": False, "user": None}

    def test_me_returns_user_and_csrf_token(self, admin_client) -> None:
        body = admin_client.get("/api/auth/me").get_json()
        assert body["authenticated"] is True
        assert body["user"]["username"] == "admin"
        assert body["user"]["role"] == "admin"
        assert body["csrf_token"]

    def test_login_sets_httponly_cookie(self, web_app) -> None:
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN, SESSION_COOKIE_NAME

        auth.create_user("admin", GOOD_PASSWORD, role=ROLE_ADMIN)
        response = web_app.test_client().post(
            "/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD}
        )
        assert response.status_code == 200
        cookie = response.headers["Set-Cookie"]
        assert SESSION_COOKIE_NAME in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Lax" in cookie
        assert response.get_json()["user"]["username"] == "admin"

    def test_wrong_password_is_401_and_does_not_leak_existence(self, web_app) -> None:
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user("admin", GOOD_PASSWORD, role=ROLE_ADMIN)
        client = web_app.test_client()
        for username in ("admin", "nobody"):
            response = client.post(
                "/api/auth/login", json={"username": username, "password": "Wrong-1!"}
            )
            assert response.status_code == 401
            assert response.get_json()["error"] == "用户名或密码不正确"

    def test_must_change_password_is_flagged(self, web_app) -> None:
        """B4：bootstrap 建的号首登必须改密 —— 前端据此跳改密页。"""
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user(
            "admin", GOOD_PASSWORD, role=ROLE_ADMIN, must_change_password=True
        )
        body = (
            web_app.test_client()
            .post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})
            .get_json()
        )
        assert body["must_change_password"] is True
        assert body["user"]["must_change_password"] is True

    def test_logout_revokes_session(self, admin_client) -> None:
        assert admin_client.post("/api/auth/logout").status_code == 200
        assert admin_client.get("/api/admin/config").status_code == 401

    def test_change_password_requires_correct_old_password(self, admin_client) -> None:
        response = admin_client.post(
            "/api/auth/password",
            json={"old_password": "Wrong-1!", "new_password": "N3w-Pass!"},
        )
        assert response.status_code == 403
        assert response.get_json()["error"] == "当前密码不正确"

    def test_change_password_rejects_weak_password(self, admin_client) -> None:
        response = admin_client.post(
            "/api/auth/password",
            json={"old_password": GOOD_PASSWORD, "new_password": "weak"},
        )
        assert response.status_code == 400
        assert "at least" in response.get_json()["error"]

    def test_change_password_issues_new_session(self, admin_client) -> None:
        """改密会踢掉全部会话，但当前设备要能继续用（重签 token）。"""
        response = admin_client.post(
            "/api/auth/password",
            json={"old_password": GOOD_PASSWORD, "new_password": "N3w-Pass!"},
        )
        assert response.status_code == 200
        assert response.get_json()["token"]
        assert admin_client.get("/api/auth/me").get_json()["authenticated"] is True

    def test_users_list_hides_password_hash(self, admin_client) -> None:
        body = admin_client.get("/api/admin/users").get_json()
        assert body["users"]
        assert "password_hash" not in json.dumps(body)


# ===========================================================================
# OOBE（§4.4，B4 / B5）
# ===========================================================================
class TestOobe:
    def test_exactly_five_steps(self, admin_client) -> None:
        """B4：删掉「建号」步（6 → 5），建号只在安装阶段发生。"""
        body = admin_client.get("/api/oobe/state").get_json()
        assert body["total"] == 5
        assert [s["key"] for s in body["steps"]] == [
            "welcome",
            "school",
            "feishu_group",
            "feishu_bot",
            "preferences",
        ]

    def test_no_step_creates_an_account(self, admin_client) -> None:
        """OOBE 里不得出现任何账号相关字段（B4 的核心要求）。"""
        body = admin_client.get("/api/oobe/state").get_json()
        for step in body["steps"]:
            assert "password" not in step["key"]
            assert "admin" not in step["key"]
        assert not any("password" in key for key in body["values"])

    def test_step_reuses_registry_metadata(self, admin_client) -> None:
        """B5：OOBE 只是配置中心的引导子集 —— 元数据必须来自注册表。"""
        body = admin_client.get("/api/oobe/state").get_json()
        school = body["steps"][1]
        assert school["key"] == "school"
        current = body["current"]
        assert current["key"] == "welcome"
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        current = admin_client.get("/api/oobe/state").get_json()["current"]
        keys = {s["key"] for s in current["settings"]}
        assert {"dorm_openid", "dorm_base_url", "eqprice"} <= keys

    def test_state_keys_are_not_writable(self, admin_client) -> None:
        """``last_room_id`` 是 STATE —— 展示但不许 OOBE 写。"""
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        current = admin_client.get("/api/oobe/state").get_json()["current"]
        assert "last_room_id" in current["keys"]
        assert "last_room_id" not in current["writable"]

    def test_advance_saves_values_with_same_validation(self, admin_client) -> None:
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        response = admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "values": {"dorm_openid": "oobe-openid"}},
        )
        assert response.status_code == 200
        assert response.get_json()["saved"]["applied"] == ["dorm_openid"]
        assert get_str("dorm_openid") == "oobe-openid"

    def test_advance_ignores_keys_outside_the_step(self, admin_client) -> None:
        """OOBE 只能写本步的键 —— 否则「引导」就成了绕过配置中心的旁路。"""
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        response = admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "values": {"quiet_hours_start": "01:00"}},
        )
        assert response.get_json()["saved"]["applied"] == []
        assert get_str("quiet_hours_start") != "01:00"

    # -----------------------------------------------------------------------
    # 校验失败必须能被看见（B5：错误留在本步，不许「假装前进」）
    # -----------------------------------------------------------------------
    def test_invalid_value_does_not_advance(self, admin_client) -> None:
        """填了非法值 → 207 + **停在本步**，错误带中文原因。"""
        admin_client.post("/api/oobe/advance", json={"direction": "next"})  # → school
        response = admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "values": {"dorm_base_url": "not-a-url"}},
        )
        assert response.status_code == 207
        body = response.get_json()
        assert body["saved"]["errors"], body["saved"]
        assert body["current"]["key"] == "school"  # 没有前进
        assert body["step"] == 1

    def test_invalid_value_is_not_saved(self, admin_client) -> None:
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "values": {"dorm_base_url": "not-a-url"}},
        )
        assert get_str("dorm_base_url") != "not-a-url"

    def test_cannot_complete_with_a_failing_step(self, admin_client) -> None:
        """🔑 从倒数第二步前进时校验失败 → **不得**标记完成。

        否则用户会以为配好了，而实际关键项根本没写进去 —— 抓取永远起不来，
        界面上却没有任何异常。
        """
        for _ in range(3):  # 0→1→2→3（走到倒数第二步）
            admin_client.post("/api/oobe/advance", json={"direction": "next", "skip": True})

        before = admin_client.get("/api/oobe/state").get_json()
        assert before["step"] == 3 and before["completed"] is False

        response = admin_client.post(
            "/api/oobe/advance",
            # 这一步唯一的强校验项：feishu_app_id 必须 ≤ 64 字符
            json={"direction": "next", "values": {"feishu_app_id": "x" * 100}},
        )

        assert response.status_code == 207
        body = response.get_json()
        assert body["step"] == 3  # 没前进
        assert body["completed"] is False  # 也没被标记完成
        assert body["saved"]["errors"]

    def test_backward_still_works_after_an_error(self, admin_client) -> None:
        """有错误时也要能后退（回去改前面的步骤）。"""
        admin_client.post("/api/oobe/advance", json={"direction": "next"})  # → school
        admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "values": {"dorm_base_url": "not-a-url"}},
        )
        body = admin_client.post(
            "/api/oobe/advance", json={"direction": "prev", "skip": True}
        ).get_json()
        assert body["step"] == 0

    def test_skip_does_not_save(self, admin_client) -> None:
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        response = admin_client.post(
            "/api/oobe/advance",
            json={"direction": "next", "skip": True, "values": {"dorm_openid": "x"}},
        )
        assert response.get_json()["saved"]["applied"] == []
        assert get_str("dorm_openid") != "x"

    def test_prev_moves_back_without_completing(self, admin_client) -> None:
        admin_client.post("/api/oobe/advance", json={"direction": "next"})
        body = admin_client.post(
            "/api/oobe/advance", json={"direction": "prev"}
        ).get_json()
        assert body["step"] == 0
        assert body["completed"] is False

    def test_reaching_last_step_completes(self, admin_client) -> None:
        """B5：``/complete`` 合并进 ``advance``。"""
        for _ in range(5):
            body = admin_client.post(
                "/api/oobe/advance", json={"direction": "next"}
            ).get_json()
        assert body["step"] == 4
        assert body["completed"] is True

    def test_oobe_requires_admin(self, viewer_client) -> None:
        assert viewer_client.get("/api/oobe/state").status_code == 403

    def test_advance_requires_csrf(self, admin_client) -> None:
        response = admin_client.post(
            "/api/oobe/advance", json={"direction": "next"}, headers={"X-CSRF-Token": ""}
        )
        assert response.status_code == 403

    def test_only_two_endpoints_exist(self, web_app) -> None:
        """B5 §5.4：10 → 2（legacy 的 save-state/next/prev/skip/validate/complete 全没了）。"""
        oobe_rules = {
            rule.rule for rule in web_app.url_map.iter_rules() if "/oobe" in rule.rule
        }
        assert oobe_rules == {"/api/oobe/state", "/api/oobe/advance"}


# ===========================================================================
# 自助配置（§4.5，N4）
# ===========================================================================
class TestParseUrl:
    def test_extracts_credentials_and_room(self, tmp_db, public_dns) -> None:
        parsed = setup_service.parse_url(
            "https://xydf.example.edu.cn/campus/webchat/index"
            "?openid=abc-123&roomId=room-uuid&roomNo=101&EqPrice=0.56"
        )
        assert parsed["openid"] == "abc-123"
        assert parsed["room_id"] == "room-uuid"
        assert parsed["room_no"] == "101"
        assert parsed["eqprice"] == pytest.approx(0.56)
        assert parsed["base_url"] == "https://xydf.example.edu.cn"

    def test_accepts_url_without_scheme(self, tmp_db, public_dns) -> None:
        """用户经常只粘 ``host/path?...``。"""
        parsed = setup_service.parse_url("xydf.example.edu.cn/a?openid=o&roomid=r")
        assert parsed["base_url"] == "https://xydf.example.edu.cn"
        assert parsed["room_id"] == "r"

    def test_param_names_are_case_insensitive(self, tmp_db, public_dns) -> None:
        parsed = setup_service.parse_url(
            "https://xydf.example.edu.cn/a?OpenId=o&ROOMID=r&eqprice=0.5"
        )
        assert parsed["openid"] == "o"
        assert parsed["room_id"] == "r"

    @pytest.mark.parametrize(
        "url",
        [
            "",
            "ftp://xydf.example.edu.cn/a?openid=o",
            "https://127.0.0.1/a?openid=o",
            "https://192.168.1.10/a?openid=o",
            "https://[::1]/a?openid=o",
            "https://169.254.169.254/latest/meta-data",
        ],
    )
    def test_ssrf_protection_rejects_bad_urls(self, tmp_db, url) -> None:
        """R37 B5 的四层防护**不得简化**（scheme / 形态 / IP 字面量 / DNS）。"""
        with pytest.raises(ValueError):
            setup_service.parse_url(url)

    def test_host_must_match_configured_school(self, tmp_db) -> None:
        """钓鱼防护：不能靠粘贴 URL 把凭据发到别的服务器。"""
        set_many({"dorm_base_url": "https://xydf.example.edu.cn"})
        with pytest.raises(ValueError, match="不同源|当前配置的学校地址"):
            setup_service.parse_url("https://evil.example.com/a?openid=o")

    def test_placeholder_host_is_adopted_on_first_setup(self, tmp_db, public_dns) -> None:
        """出厂占位值阶段（首次 OOBE）允许采纳粘贴 URL 的 host。"""
        assert get_str("dorm_base_url") == setup_service.DEFAULT_BASE_URL
        parsed = setup_service.parse_url("https://real.example.edu.cn/a?openid=o")
        assert parsed["base_url"] == "https://real.example.edu.cn"


class TestSetupApi:
    def test_parse_url_endpoint(self, admin_client, public_dns) -> None:
        response = admin_client.post(
            "/api/setup/parse-url",
            json={"url": "https://xydf.example.edu.cn/a?openid=o1&roomId=r1"},
        )
        assert response.status_code == 200
        assert response.get_json()["room_id"] == "r1"

    def test_parse_url_never_echoes_openid_in_audit(self, admin_client, public_dns) -> None:
        """Q20：openid 是 secret，审计与响应都不得泄露。"""
        admin_client.post(
            "/api/setup/parse-url",
            json={"url": "https://xydf.example.edu.cn/a?openid=very-secret-openid"},
        )
        entries = admin_client.get("/api/admin/audit?action=setup.parsed").get_json()
        assert "very-secret-openid" not in json.dumps(entries)

    def test_bad_url_returns_400(self, admin_client) -> None:
        response = admin_client.post(
            "/api/setup/parse-url", json={"url": "https://127.0.0.1/a"}
        )
        assert response.status_code == 400
        assert response.get_json()["ok"] is False

    def test_verify_without_credentials_reports_missing(self, admin_client) -> None:
        response = admin_client.post("/api/setup/verify", json={})
        assert response.status_code == 400
        assert response.get_json()["error"] == "缺少登录凭据 openid"

    def test_commit_writes_config(self, admin_client) -> None:
        response = admin_client.post(
            "/api/setup/commit",
            json={
                "openid": "committed-openid",
                "room_id": "room-9",
                "eqprice": 0.58,
                "base_url": "https://xydf.example.edu.cn",
            },
        )
        assert response.status_code == 200
        assert get_str("dorm_openid") == "committed-openid"
        assert get_str("dorm_room_id") == "room-9"
        assert get_float("eqprice") == pytest.approx(0.58)
        assert get_str("last_room_id") == "room-9"

    def test_commit_rejects_non_numeric_price(self, admin_client) -> None:
        response = admin_client.post(
            "/api/setup/commit", json={"openid": "o", "eqprice": "free"}
        )
        assert response.status_code == 400

    def test_setup_requires_admin(self, viewer_client) -> None:
        response = viewer_client.post("/api/setup/parse-url", json={"url": "https://a.b"})
        assert response.status_code == 403


class TestVerify:
    def test_verify_reports_scrape_failure_without_writing_config(
        self, tmp_db, public_dns
    ) -> None:
        """``verify`` **不落库** —— 验证失败不该留下半截状态。"""

        class _FailingClient:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("boom")

            def post_form(self, *_args, **_kwargs):
                raise RuntimeError("boom")

            def close(self):
                pass

        result = setup_service.verify(
            openid="o",
            room_id="r",
            base_url="https://xydf.example.edu.cn",
            client=_FailingClient(),
        )
        assert result["ok"] is False
        assert get_str("dorm_openid") == ""

    def test_verify_sanitizes_openid_from_errors(self, tmp_db, public_dns) -> None:
        class _LeakyClient:
            def get(self, *_args, **_kwargs):
                raise RuntimeError("bad credential super-secret-openid")

            def post_form(self, *_args, **_kwargs):
                raise RuntimeError("bad credential super-secret-openid")

            def close(self):
                pass

        result = setup_service.verify(
            openid="super-secret-openid",
            room_id="r",
            base_url="https://xydf.example.edu.cn",
            client=_LeakyClient(),
        )
        assert "super-secret-openid" not in json.dumps(result)


# ===========================================================================
# 启动维护 / bootstrap 建号（§4.8，B4）
# ===========================================================================
class TestBootstrap:
    def test_creates_admin_from_env_password(self, tmp_db, monkeypatch) -> None:
        """B4：安装脚本写进 ``.env`` 的随机密码 → 首启建号 → 强制改密。"""
        from starwatt.services import admin_service

        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Boot-Str0ng!")
        report = admin_service.bootstrap_admin()
        assert report == {"created": True, "username": "admin"}

        from starwatt.db.repositories import UserRepo

        user = UserRepo.get_by_username("admin")
        assert user is not None
        assert user.is_admin
        assert user.needs_password_change is True

    def test_is_idempotent(self, tmp_db, monkeypatch) -> None:
        from starwatt.services import admin_service

        monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "Boot-Str0ng!")
        admin_service.bootstrap_admin()
        assert admin_service.bootstrap_admin()["created"] is False

    def test_skipped_without_env_password(self, tmp_db) -> None:
        from starwatt.services import admin_service

        assert admin_service.bootstrap_admin() == {"created": False, "username": None}

    def test_first_run_maintenance_is_idempotent(self, tmp_db) -> None:
        from starwatt.services import admin_service

        first = admin_service.first_run_maintenance()
        second = admin_service.first_run_maintenance()
        assert set(first) == {"purged", "defaults", "bootstrap"}
        assert second["defaults"] == 0  # 第二次没有新键要写

    def test_maintenance_removes_legacy_plaintext_password(self, tmp_db) -> None:
        """L20：``admin_password`` 是明文后门，必须删掉。"""
        from starwatt.db.repositories import MetaRepo
        from starwatt.services import admin_service

        MetaRepo.set("admin_password", "legacy-plaintext")
        report = admin_service.first_run_maintenance()
        assert report["purged"] >= 1
        assert MetaRepo.get("admin_password") is None


# ===========================================================================
# 强制首登改密（B4 / Q19，M4 §4.8）
# ===========================================================================
class TestMustChangePasswordGate:
    """``must_change_password=1`` → 放进来但**只许改密**。

    为什么不能直接拒绝登录：拒绝 = 没有会话 = 永远改不了密码，
    用户会被彻底锁在门外。所以是「403 拦截其它接口」，不是「401 拒绝登录」。
    """

    PROTECTED = (
        "/api/data",
        "/api/admin/config",
        "/api/admin/users",
        "/api/admin/audit",
    )

    def test_other_endpoints_are_blocked(self, web_app) -> None:
        client, _ = _login_as(web_app, "bootstrap", must_change=True)
        for path in self.PROTECTED:
            response = client.get(path)
            assert response.status_code == 403, path
            assert response.get_json()["error"] == "must_change_password"

    def test_me_still_answers_so_the_ui_can_redirect(self, web_app) -> None:
        """前端靠 ``/me`` 得知「必须先改密」—— 这条不能也挡掉。"""
        client, _ = _login_as(web_app, "bootstrap", must_change=True)
        response = client.get("/api/auth/me")
        assert response.status_code == 200
        assert response.get_json()["user"]["must_change_password"] is True

    def test_logout_still_works(self, web_app) -> None:
        client, _ = _login_as(web_app, "bootstrap", must_change=True)
        assert client.post("/api/auth/logout").status_code == 200

    def test_changing_password_lifts_the_gate(self, web_app) -> None:
        from starwatt.auth import csrf as csrf_mod

        client, _ = _login_as(web_app, "bootstrap", must_change=True)
        assert client.get("/api/data").status_code == 403

        changed = client.post(
            "/api/auth/password",
            json={"old_password": GOOD_PASSWORD, "new_password": "N3w-Pass!x"},
        )
        assert changed.status_code == 200, changed.get_json()

        # 改密会换新会话 → CSRF token 也换，重新取一次再继续断言
        client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf_mod.issue(changed.get_json()["token"])

        assert client.get("/api/auth/me").get_json()["user"]["must_change_password"] is False
        assert client.get("/api/data").status_code == 200

    def test_normal_user_is_not_affected(self, web_app) -> None:
        client, _ = _login_as(web_app, "normal", must_change=False)
        assert client.get("/api/data").status_code == 200

    def test_anonymous_is_still_401_not_403(self, web_app) -> None:
        """未登录是 401 —— 别把「没登录」误报成「要改密」。"""
        response = web_app.test_client().get("/api/data")
        assert response.status_code == 401
        assert response.get_json()["error"] == "unauthorized"

    def test_allowed_paths_are_exactly_three(self) -> None:
        """白名单多一条就是安全缺口 —— 用断言钉住它的规模。"""
        from starwatt.auth.decorators import MUST_CHANGE_ALLOWED_PATHS

        assert MUST_CHANGE_ALLOWED_PATHS == {
            "/api/auth/password",
            "/api/auth/logout",
            "/api/auth/me",
        }


# ===========================================================================
# 用户管理（F2：列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除）
# ===========================================================================
class TestUserManagement:
    @staticmethod
    def _by_name(client, username: str) -> dict:
        users = client.get("/api/admin/users").get_json()["users"]
        return next(u for u in users if u["username"] == username)

    @staticmethod
    def _create(client, username: str, **extra) -> dict:
        payload = {"username": username, "password": GOOD_PASSWORD, **extra}
        response = client.post("/api/admin/users", json=payload)
        assert response.status_code == 200, response.get_json()
        return response.get_json()["user"]

    def test_create_and_list(self, admin_client) -> None:
        created = self._create(admin_client, "roommate", role="viewer")
        assert created["username"] == "roommate" and created["role"] == "viewer"
        # 绝不回显密码 / 哈希（``must_change_password`` 是布尔标志，不算）
        assert "password_hash" not in json.dumps(created)
        assert not any("hash" in key for key in created)
        assert self._by_name(admin_client, "roommate")["id"] == created["id"]

    def test_duplicate_username_is_409(self, admin_client) -> None:
        self._create(admin_client, "dup")
        response = admin_client.post(
            "/api/admin/users", json={"username": "dup", "password": GOOD_PASSWORD}
        )
        assert response.status_code == 409
        assert "已存在" in response.get_json()["error"]

    def test_weak_password_is_rejected(self, admin_client) -> None:
        response = admin_client.post(
            "/api/admin/users", json={"username": "weak", "password": "123"}
        )
        assert response.status_code == 400

    def test_role_change(self, admin_client) -> None:
        created = self._create(admin_client, "promo")
        response = admin_client.patch(
            f"/api/admin/users/{created['id']}", json={"role": "admin"}
        )
        assert response.status_code == 200
        assert self._by_name(admin_client, "promo")["role"] == "admin"

    def test_disable_blocks_login(self, admin_client, web_app) -> None:
        created = self._create(admin_client, "gone")
        response = admin_client.patch(
            f"/api/admin/users/{created['id']}", json={"disabled": True}
        )
        assert response.status_code == 200

        login = web_app.test_client().post(
            "/api/auth/login", json={"username": "gone", "password": GOOD_PASSWORD}
        )
        assert login.status_code == 403
        assert login.get_json()["error"] == "该账号已被禁用"

    def test_delete_removes_user(self, admin_client) -> None:
        created = self._create(admin_client, "temp")
        assert admin_client.delete(f"/api/admin/users/{created['id']}").status_code == 200
        names = [
            u["username"] for u in admin_client.get("/api/admin/users").get_json()["users"]
        ]
        assert "temp" not in names

    def test_reset_password_forces_change_and_kicks_sessions(
        self, admin_client, web_app
    ) -> None:
        """管理员重置密码 → 目标用户被踢下线，且**首登必须再改一次**。"""
        created = self._create(admin_client, "resetme")

        victim = web_app.test_client()
        victim.post(
            "/api/auth/login", json={"username": "resetme", "password": GOOD_PASSWORD}
        )
        assert victim.get("/api/data").status_code == 200

        response = admin_client.post(
            f"/api/admin/users/{created['id']}/password", json={"password": "R3set-Pass!"}
        )
        assert response.status_code == 200, response.get_json()

        # 旧会话已被 revoke → 旧客户端立刻掉线
        assert victim.get("/api/data").status_code == 401

        # 新密码能登录，但被强制改密（临时密码 ≠ 本人持有）
        fresh = web_app.test_client()
        body = fresh.post(
            "/api/auth/login", json={"username": "resetme", "password": "R3set-Pass!"}
        ).get_json()
        assert body["must_change_password"] is True
        assert fresh.get("/api/data").status_code == 403

    def test_cannot_demote_or_disable_or_delete_last_admin(self, admin_client) -> None:
        """三条护栏：降级 / 禁用 / 删除最后一个管理员都会把所有人锁在门外。"""
        me = self._by_name(admin_client, "admin")["id"]
        assert (
            admin_client.patch(f"/api/admin/users/{me}", json={"role": "viewer"}).status_code
            == 400
        )
        assert (
            admin_client.patch(f"/api/admin/users/{me}", json={"disabled": True}).status_code
            == 400
        )
        assert admin_client.delete(f"/api/admin/users/{me}").status_code == 400

    def test_cannot_disable_or_delete_self(self, admin_client) -> None:
        """有第二个管理员时「最后一个」护栏不再生效 —— 所以「不能动自己」必须独立存在。"""
        self._create(admin_client, "admin2", role="admin")
        me = self._by_name(admin_client, "admin")["id"]

        response = admin_client.patch(f"/api/admin/users/{me}", json={"disabled": True})
        assert response.status_code == 400
        assert response.get_json()["error"] == "不能禁用当前登录的账号"

        response = admin_client.delete(f"/api/admin/users/{me}")
        assert response.status_code == 400
        assert response.get_json()["error"] == "不能删除当前登录的账号"

    def test_demoting_other_admin_is_allowed(self, admin_client) -> None:
        """护栏只保护「最后一个」—— 有两个管理员时降级另一个是正常操作。"""
        other = self._create(admin_client, "admin2", role="admin")
        response = admin_client.patch(
            f"/api/admin/users/{other['id']}", json={"role": "viewer"}
        )
        assert response.status_code == 200
        assert self._by_name(admin_client, "admin2")["role"] == "viewer"

    def test_unknown_user_is_404(self, admin_client) -> None:
        assert (
            admin_client.patch("/api/admin/users/9999", json={"role": "admin"}).status_code
            == 404
        )
        assert admin_client.delete("/api/admin/users/9999").status_code == 404
        assert (
            admin_client.post(
                "/api/admin/users/9999/password", json={"password": GOOD_PASSWORD}
            ).status_code
            == 404
        )

    def test_patch_requires_a_field(self, admin_client) -> None:
        me = self._by_name(admin_client, "admin")["id"]
        response = admin_client.patch(f"/api/admin/users/{me}", json={})
        assert response.status_code == 400
        assert "role" in response.get_json()["error"]

    def test_viewer_cannot_manage_users(self, viewer_client) -> None:
        assert viewer_client.get("/api/admin/users").status_code == 403
        assert (
            viewer_client.post(
                "/api/admin/users", json={"username": "x", "password": GOOD_PASSWORD}
            ).status_code
            == 403
        )

    def test_writes_require_csrf(self, web_app) -> None:
        from starwatt.auth import service as auth
        from starwatt.auth.constants import ROLE_ADMIN

        auth.create_user("admin", GOOD_PASSWORD, role=ROLE_ADMIN)
        client = web_app.test_client()
        client.post("/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD})

        response = client.post(  # 故意不带 X-CSRF-Token
            "/api/admin/users", json={"username": "nocsrf", "password": GOOD_PASSWORD}
        )
        assert response.status_code == 403

    def test_actions_are_audited(self, admin_client) -> None:
        created = self._create(admin_client, "audited")
        admin_client.delete(f"/api/admin/users/{created['id']}")

        entries = admin_client.get("/api/admin/audit").get_json()["entries"]
        actions = {entry["action"] for entry in entries}
        assert {"user.create", "user.delete"} <= actions


# ===========================================================================
# 功能开关状态（5.7）—— 前端「功能开关」页的全部数据来源
# ===========================================================================
class TestFlagsState:
    """开关的父子语义与「此刻为什么没推」只有后端算得出来。"""

    def test_lists_all_eighteen_switches(self, admin_client) -> None:
        from starwatt.flags import FLAGS

        payload = admin_client.get("/api/admin/flags").get_json()
        assert {flag["key"] for flag in payload["flags"]} == set(FLAGS)
        assert len(payload["flags"]) == 18

    def test_three_groups_in_order(self, admin_client) -> None:
        payload = admin_client.get("/api/admin/flags").get_json()
        assert [group["kind"] for group in payload["groups"]] == ["master", "layer", "command"]
        kinds = {flag["kind"] for flag in payload["flags"]}
        assert kinds == {"master", "layer", "command"}
        assert sum(1 for f in payload["flags"] if f["kind"] == "master") == 3
        assert sum(1 for f in payload["flags"] if f["kind"] == "layer") == 8
        assert sum(1 for f in payload["flags"] if f["kind"] == "command") == 7

    def test_parents_are_exposed(self, admin_client) -> None:
        payload = admin_client.get("/api/admin/flags").get_json()
        by_key = {flag["key"]: flag for flag in payload["flags"]}
        assert by_key["push_l1_enable"]["parent"] == "push_group_enabled"
        assert by_key["cmd_help_enabled"]["parent"] == "push_bot_enabled"
        assert by_key["push_group_enabled"]["parent"] is None

    def test_bot_masters_default_off(self, admin_client) -> None:
        """两个机器人总开关默认关闭（新装不该刷「未配置」错误）。"""
        payload = admin_client.get("/api/admin/flags").get_json()
        by_key = {flag["key"]: flag for flag in payload["flags"]}
        assert by_key["push_group_enabled"]["enabled"] is True
        assert by_key["push_bot_enabled"]["enabled"] is False
        assert by_key["qq_bot_enabled"]["enabled"] is False

    def test_suppression_reasons_explain_why(self, admin_client) -> None:
        """关掉总开关 → 8 个层都带「为什么没推」的中文原因。"""
        set_many({"push_group_enabled": False})
        payload = admin_client.get("/api/admin/flags").get_json()
        assert len(payload["suppressed"]) == 8
        assert all("总开关" in item["reason"] for item in payload["suppressed"])

    def test_suppression_is_empty_when_all_on(self, admin_client) -> None:
        set_many({"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"})
        payload = admin_client.get("/api/admin/flags").get_json()
        assert payload["suppressed"] == []
        assert payload["quiet_hours"] is False

    def test_layer_off_reason_names_the_layer(self, admin_client) -> None:
        set_many({"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"})
        set_many({"push_l2_enable": False})
        payload = admin_client.get("/api/admin/flags").get_json()
        reasons = {item["layer"]: item["reason"] for item in payload["suppressed"]}
        assert "push_l2_enable" in reasons["l2"]

    def test_viewer_cannot_read(self, viewer_client) -> None:
        assert viewer_client.get("/api/admin/flags").status_code == 403


# ===========================================================================
# 日志状态（5.8）
# ===========================================================================
class TestLoggingState:
    def test_levels_and_categories(self, admin_client) -> None:
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["levels"] == [
            "TRACE",
            "DEBUG",
            "INFO",
            "NOTICE",
            "WARNING",
            "ERROR",
            "CRITICAL",
        ]
        assert len(payload["categories"]) == 8

    def test_effective_level_follows_global(self, admin_client) -> None:
        set_many({"log_level": "WARNING"})
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["global_level"] == "WARNING"
        assert set(payload["effective"].values()) == {"WARNING"}

    def test_module_override_wins(self, admin_client) -> None:
        """🔑 模块级覆盖只影响那一个类别 —— 这是 Q20 的核心能力。"""
        set_many({"log_level": "WARNING", "log_overrides": {"scrape": "DEBUG"}})
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["effective"]["scrape"] == "DEBUG"
        assert payload["effective"]["push"] == "WARNING"

    def test_unknown_category_in_overrides_is_hidden(self, admin_client) -> None:
        set_many({"log_overrides": {"scrape": "DEBUG", "typo": "TRACE"}})
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["overrides"] == {"scrape": "DEBUG"}

    def test_file_state(self, admin_client) -> None:
        set_many({"log_file_enabled": True, "log_file_retention_days": 14})
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["file"]["enabled"] is True
        assert payload["file"]["path"].endswith("starwatt.log")
        assert payload["file"]["retention_days"] == 14

    def test_reports_masked_secret_count(self, admin_client) -> None:
        """脱敏在生效的证据（Q20 铁律：secret 不得进日志）。"""
        payload = admin_client.get("/api/admin/logging").get_json()
        assert payload["masked_secrets"] >= 0
        assert isinstance(payload["masked_secrets"], int)

    def test_viewer_cannot_read(self, viewer_client) -> None:
        assert viewer_client.get("/api/admin/logging").status_code == 403
