"""只读数据 API + 访问控制（M4 §4.2 / §4.3）。

契约来自 ``fixtures/api.json``；访问控制来自 Q11：

============================  =========================  ==========
场景                           请求                        结果
============================  =========================  ==========
``public_readonly=0``（默认）   匿名 GET ``/api/data``      401
``public_readonly=1``          匿名 GET ``/api/data``      200
两种情况                       已登录 GET                  200
============================  =========================  ==========
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from starwatt.config_registry import set_many, set_state
from starwatt.db.models import Record
from starwatt.db.repositories import RecordRepo
from starwatt.web.blueprints import ALL_BLUEPRINTS
from starwatt.web.blueprints import dashboard as dashboard_bp

FIXTURES = Path(__file__).resolve().parents[1] / "regression" / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def client(web_app):
    """已隔离 DB 的测试客户端。"""
    return web_app.test_client()


# ===========================================================================
# 蓝图注册
# ===========================================================================
class TestBlueprintRegistry:
    def test_expected_blueprints_are_registered(self) -> None:
        """``REWRITE_PLAN`` §1.2 的七个蓝图全部落地（``feishu`` 兼管 QQ 回调）。"""
        assert [bp.name for bp in ALL_BLUEPRINTS] == [
            "health",
            "auth_api",
            "dashboard_api",
            "admin_api",
            "oobe_api",
            "setup_api",
            "feishu",
        ]

    def test_bot_callbacks_are_served_by_feishu_blueprint(self, web_app) -> None:
        """M4 §4.2：平台回调不再由 ``web/bots.py`` 单独注册（该模块已删除）。"""
        assert web_app.view_functions["feishu.qq_events"]
        assert web_app.view_functions["feishu.feishu_events"]
        rules = {rule.rule for rule in web_app.url_map.iter_rules()}
        assert {"/qq/events", "/feishu/event"} <= rules

    def test_routes_are_served(self, web_app) -> None:
        rules = {rule.rule for rule in web_app.url_map.iter_rules()}
        assert {
            "/healthz",
            "/api/data",
            "/api/live",
            "/api/auth/login",
            "/api/oobe/state",
            "/api/setup/parse-url",
        } <= rules

    def test_config_seven_endpoints_exist(self, web_app) -> None:
        """Q15：配置 API 七个端点一个都不能少（M4 §4.6）。"""
        rules = {rule.rule for rule in web_app.url_map.iter_rules()}
        assert {
            "/api/admin/config/schema",
            "/api/admin/config",
            "/api/admin/config/test",
            "/api/admin/config/export",
            "/api/admin/config/import",
            "/api/admin/config/reset",
        } <= rules


class TestParseHours:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(None, 24), ("", 24), ("48", 48), ("1", 1), ("720", 720),
         ("0", 24), ("-5", 24), ("9999", 24), ("abc", 24), ("3.5", 24)],
    )
    def test_parsing(self, raw, expected) -> None:
        """非法 / 越界一律回落默认值 —— legacy 的行为是「怎么填都能出图」。"""
        assert dashboard_bp.parse_hours(raw) == expected


# ===========================================================================
# 访问控制（Q11）
# ===========================================================================
class TestAccessControl:
    def test_anonymous_read_denied_by_default(self, client) -> None:
        response = client.get("/api/data")
        assert response.status_code == 401
        assert response.get_json() == {"ok": False, "error": "unauthorized"}

    def test_anonymous_read_allowed_when_public_readonly(self, client) -> None:
        set_many({"public_readonly": True})
        assert client.get("/api/data").status_code == 200
        assert client.get("/api/live").status_code == 200

    def test_live_denied_by_default(self, client) -> None:
        assert client.get("/api/live").status_code == 401

    def test_healthz_is_always_anonymous(self, client) -> None:
        """探活端点不能要求认证（Docker / systemd 要能匿名探）。"""
        assert client.get("/healthz").status_code == 200

    def test_unknown_path_returns_json_404(self, client) -> None:
        response = client.get("/api/nope")
        assert response.status_code == 404
        assert response.get_json()["error"] == "not_found"


# ===========================================================================
# /api/data
# ===========================================================================
class TestApiData:
    def test_shape_matches_fixture(self, client) -> None:
        set_many({"public_readonly": True})
        expected = _fixture("api")["/api/data?hours=24"]
        payload = client.get("/api/data?hours=24").get_json()

        assert sorted(payload) == sorted(expected["keys"])
        assert payload["hours"] == 24
        assert sorted(payload["stats"]) == sorted(expected["stats_keys"])

    def test_rows_match_fixture(self, client, frozen_now) -> None:
        set_many({"public_readonly": True})
        expected = _fixture("api")["/api/data?hours=24"]
        RecordRepo.insert(
            Record(ts="2026-10-06 11:00:00", read_time="2026-10-06 10:59:00", remain=42.5)
        )
        row = client.get("/api/data?hours=24").get_json()["rows"][0]
        assert sorted(row) == sorted(expected["row_keys"])

    def test_bad_hours_falls_back(self, client) -> None:
        set_many({"public_readonly": True})
        assert client.get("/api/data?hours=abc").get_json()["hours"] == 24

    def test_explicit_range(self, client) -> None:
        set_many({"public_readonly": True})
        RecordRepo.insert(Record(ts="2026-10-01 00:00:00", read_time=None, remain=10.0))
        payload = client.get(
            "/api/data?start=2026-09-30 00:00:00&end=2026-10-02 00:00:00"
        ).get_json()
        assert len(payload["rows"]) == 1


# ===========================================================================
# /api/live
# ===========================================================================
class TestApiLive:
    def test_shape_matches_fixture(self, client) -> None:
        set_many({"public_readonly": True})
        expected = _fixture("api")["/api/live"]
        payload = client.get("/api/live").get_json()

        assert sorted(payload) == sorted(expected["keys"])
        assert sorted(payload["monthly_breakdown"]) == sorted(expected["monthly_keys"])
        assert sorted(payload["stats"]) == sorted(expected["stats_keys"])

    def test_empty_state(self, client) -> None:
        set_many({"public_readonly": True})
        payload = client.get("/api/live").get_json()
        assert payload["latest_ts"] is None
        assert payload["eqprice"] == 0.5
        assert payload["stale"] is False

    def test_run_status_present(self, client) -> None:
        from starwatt.db.repositories import RunStatusRepo

        set_many({"public_readonly": True})
        set_state("last_room_id", "room-1")
        RunStatusRepo.upsert("room-1", {"runStatus": "在线", "vol": 220.1})
        payload = client.get("/api/live").get_json()
        assert sorted(payload["run_status"]) == sorted(
            _fixture("api")["/api/live"]["run_status_keys"]
        )


# ===========================================================================
# 内部 token（Q17）
# ===========================================================================
class TestInternalToken:
    def test_rejects_when_unconfigured(self, tmp_db) -> None:
        """未配置 token 时**一律拒绝** —— 空 token 不等于「不校验」。"""
        from starwatt.web.security import internal_token_ok

        assert internal_token_ok("anything") is False

    def test_matches_configured_token(self, tmp_db) -> None:
        from starwatt.web.security import internal_token_ok

        set_many({"api_internal_token": "s3cr3t-token"})
        assert internal_token_ok("s3cr3t-token") is True
        assert internal_token_ok("wrong") is False
        assert internal_token_ok(None) is False
