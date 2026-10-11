"""明文导出密钥必须留审计（审计缺陷 3 / F-14 / F-17）。

背景：``GET /api/admin/config?include_secrets=1`` 与 ``/api/admin/config/export``
一次就能吐出 ``dorm_openid`` / ``feishu_app_secret`` / ``qq_*_secret`` /
``api_internal_token`` 全部明文。功能本身保留（导出 / 迁移需要），但**每次**
明文读取都必须留一条审计 —— 含来源 IP、管理员用户名、是否含明文。

另外守两条边界：

* 脱敏读取**不该**留「读了明文」的日志（日志噪音会淹没真正的那条）
* 响应必须 ``Cache-Control: no-store``，否则凭据会落进中间缓存 / 磁盘
"""

from __future__ import annotations

import json

from starwatt.db import AuditRepo

#: 明文读取的审计动作名（与 admin.py 里的一致）
READ_SECRETS_ACTION = "config.read_secrets"
EXPORT_ACTION = "config.export"


def _actions(action: str | None = None):
    return AuditRepo.recent(action=action, limit=100)


class TestConfigReadSecretsAudited:
    """缺陷 3 主路径。"""

    def test_include_secrets_writes_audit_row(self, admin_client) -> None:
        admin_client.get("/api/admin/config?include_secrets=1")
        rows = _actions(READ_SECRETS_ACTION)
        assert len(rows) == 1

    def test_audit_contains_ip_and_username(self, admin_client) -> None:
        admin_client.get("/api/admin/config?include_secrets=1")
        row = _actions(READ_SECRETS_ACTION)[0]
        details = json.loads(row.details) if isinstance(row.details, str) else row.details
        assert details["include_secrets"] is True
        assert details["username"] == "admin"
        assert details["ip"] == "127.0.0.1"
        assert row.target == "admin"

    def test_plaintext_is_actually_returned(self, admin_client) -> None:
        """留审计**不等于**降级功能 —— 明文仍然要拿到（既有导出流程）。"""
        admin_client.put(
            "/api/admin/config", json={"dorm_openid": "secret-openid-123"}
        )
        payload = admin_client.get("/api/admin/config?include_secrets=1").get_json()
        assert payload["values"]["dorm_openid"] == "secret-openid-123"

    def test_response_is_no_store(self, admin_client) -> None:
        response = admin_client.get("/api/admin/config?include_secrets=1")
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_masked_read_writes_no_secret_audit(self, admin_client) -> None:
        """脱敏读取不写这条日志（噪声会淹没真正的明文读取）。"""
        admin_client.get("/api/admin/config")
        assert _actions(READ_SECRETS_ACTION) == []

    def test_each_request_is_audited_separately(self, admin_client) -> None:
        """每次都要写 —— 不能只写一次。"""
        for _ in range(3):
            admin_client.get("/api/admin/config?include_secrets=1")
        assert len(_actions(READ_SECRETS_ACTION)) == 3

    def test_flag_variants_are_recognised(self, admin_client) -> None:
        """``include_secrets=1`` 与 ``=true`` 都应算明文请求（沿用既有解析）。"""
        for value in ("1", "true"):
            admin_client.get(f"/api/admin/config?include_secrets={value}")
        assert len(_actions(READ_SECRETS_ACTION)) == 2


class TestConfigExportAudited:
    """导出路径的留痕（缺陷 3 第 2 点：区分明文 / 脱敏）。"""

    def test_export_with_secrets_is_audited(self, admin_client) -> None:
        admin_client.get("/api/admin/config/export?include_secrets=1")
        rows = _actions(EXPORT_ACTION)
        assert len(rows) == 1
        details = json.loads(rows[0].details)
        assert details["include_secrets"] is True
        assert details["ip"] == "127.0.0.1"

    def test_export_action_differs_from_read_action(self, admin_client) -> None:
        """两个动作名必须能区分，否则审计里分不清是「看」还是「导出」。"""
        assert READ_SECRETS_ACTION != EXPORT_ACTION
        admin_client.get("/api/admin/config/export?include_secrets=1")
        assert len(_actions(READ_SECRETS_ACTION)) == 0
        assert len(_actions(EXPORT_ACTION)) == 1

    def test_export_response_is_no_store(self, admin_client) -> None:
        response = admin_client.get("/api/admin/config/export?include_secrets=1")
        assert "no-store" in response.headers.get("Cache-Control", "")

    def test_masked_export_records_false(self, admin_client) -> None:
        """脱敏导出也留痕，但 ``include_secrets`` 是 false（可区分）。"""
        admin_client.get("/api/admin/config/export")
        rows = _actions(EXPORT_ACTION)
        assert len(rows) == 1
        assert json.loads(rows[0].details)["include_secrets"] is False


class TestNonAdminCannotReachSecrets:
    """边界：普通用户拿不到，也不该因此产生管理员审计行。"""

    def test_viewer_is_forbidden(self, viewer_client) -> None:
        response = viewer_client.get("/api/admin/config?include_secrets=1")
        assert response.status_code == 403

    def test_anonymous_is_rejected(self, web_app) -> None:
        response = web_app.test_client().get("/api/admin/config?include_secrets=1")
        assert response.status_code in (401, 403)
