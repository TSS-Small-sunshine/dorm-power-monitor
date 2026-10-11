"""``/healthz/deep`` 就绪端点（审计缺陷 5 / F-15）。

要点：

* ``/healthz`` **保持原样** —— 不看 DB、不看调度器、永远 200（探活用）
* ``/healthz/deep`` 汇总「DB 可写 / 调度器在跑 / 上次抓取」，必需项失败 → 503
* 抓取状态只是**通报**，不因为「没抓过 / 上次失败」把端点打成 503
* 失败时逐项列出，便于直接定位
"""

from __future__ import annotations

import pytest

from starwatt.web.blueprints import health


@pytest.fixture
def migrated_db(tmp_db):
    """``tmp_db`` 只给路径；检查项被直接调用时 schema 得先建出来。"""
    from starwatt.db.connection import init as db_init

    db_init()
    return tmp_db


@pytest.fixture
def isolated_app(tmp_db):
    """直接 ``factory.create_app()`` 造的实例 —— **没有**调度器接线点。

    用来验证「未接线 ⇒ 报失败」这条边界（``web.py`` 才会注册探针）。
    """
    from starwatt.web import factory

    return factory.create_app()


@pytest.fixture
def running_scheduler(monkeypatch):
    """让 ``scheduler_running()`` 返回 True。

    为什么必需：``web_app`` fixture 会把 ``web._scheduler`` 清成 ``None``
    （保证用例之间不串），而调度器在生产里由 gunicorn ``post_fork`` 在
    **每个 worker 进程**内启动（``web.py:108`` 只禁止在 arbiter 里启动）。
    所以「没在跑」是测试夹具造成的，不是就绪路径的常态。
    """
    import web

    class _FakeScheduler:
        running = True

    monkeypatch.setattr(web, "_scheduler", _FakeScheduler())
    return web._scheduler


class TestShallowHealthzUnchanged:
    """探活端点不能被这次改动带上任何 IO。"""

    def test_returns_ok(self, web_app) -> None:
        response = web_app.test_client().get("/healthz")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["status"] == "ok"
        assert payload["version"] == health.APP_VERSION

    def test_still_anonymous(self, web_app) -> None:
        """不带任何 Cookie / token 也要能探（Docker / systemd 用）。"""
        assert web_app.test_client().get("/healthz").status_code == 200

    def test_does_not_report_checks(self, admin_client) -> None:
        """浅端点**不**暴露 checks 结构 —— 它只是探活。"""
        payload = admin_client.get("/healthz").get_json()
        assert "checks" not in payload


class TestDeepHealthy:
    """全绿路径（调度器用夹具模拟为「在跑」）。"""

    def test_returns_200(self, web_app, running_scheduler) -> None:
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 200

    def test_body_shape(self, web_app, running_scheduler) -> None:
        payload = web_app.test_client().get("/healthz/deep").get_json()
        assert payload["status"] == "ok"
        assert payload["failures"] == []
        assert payload["version"] == health.APP_VERSION
        assert set(payload["checks"]) >= {"db", "scheduler", "version", "scrape"}
        assert payload["checks"]["db"]["ok"] is True
        assert payload["checks"]["scheduler"]["ok"] is True

    def test_is_anonymous(self, web_app, running_scheduler) -> None:
        assert web_app.test_client().get("/healthz/deep").status_code == 200

    def test_reports_last_scrape_fields(self, web_app, running_scheduler) -> None:
        """抓取字段始终存在（从未抓取时为 None），但**不影响**状态码。"""
        payload = web_app.test_client().get("/healthz/deep").get_json()
        scrape = payload["checks"]["scrape"]
        assert "last_scrape_at" in scrape
        assert "last_scrape_status" in scrape
        assert payload["status"] == "ok"

    def test_repeated_calls_are_cheap(self, web_app, running_scheduler) -> None:
        """高频调用不能积累副作用 —— DB 探针走事务回滚，不留残留。"""
        from starwatt.db.connection import connect

        client = web_app.test_client()
        for _ in range(5):
            assert client.get("/healthz/deep").status_code == 200
        with connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '_healthz%'"
            ).fetchall()
        assert list(rows) == []


class TestDeepUnhealthy:
    """必需项失败 → 503。"""

    def test_503_when_scheduler_not_running(self, web_app, monkeypatch) -> None:
        import web

        monkeypatch.setattr(web, "_scheduler", None)
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 503
        payload = response.get_json()
        assert payload["status"] == "unavailable"
        assert "scheduler" in payload["failures"]
        assert payload["checks"]["db"]["ok"] is True  # 其它项仍然报了

    def test_503_when_db_check_fails(self, web_app, running_scheduler, monkeypatch) -> None:
        monkeypatch.setitem(
            health.CHECKS, "db", lambda: {"ok": False, "error": "disk I/O error"}
        )
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 503
        payload = response.get_json()
        assert payload["failures"] == ["db"]
        assert "disk I/O error" in str(payload["checks"]["db"])

    def test_503_when_db_really_unwritable(
        self, web_app, running_scheduler, monkeypatch
    ) -> None:
        """不替换检查函数，只让真实的写探针失败（走 ``_check_db`` 的异常翻译）。"""

        def _boom():
            raise RuntimeError("attempt to write a readonly database")

        monkeypatch.setattr(health, "_probe_db_writable", _boom)
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 503
        payload = response.get_json()
        assert "db" in payload["failures"]
        assert "readonly" in str(payload["checks"]["db"])

    def test_503_when_a_check_raises(
        self, web_app, running_scheduler, monkeypatch
    ) -> None:
        """检查函数自己崩了也算失败（不能把 500 漏出去）。"""

        def _boom():
            raise ValueError("kaboom")

        monkeypatch.setitem(health.CHECKS, "version", _boom)
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 503
        assert "version" in response.get_json()["failures"]

    def test_scrape_failure_does_not_break_ready(
        self, web_app, running_scheduler, monkeypatch
    ) -> None:
        """非必需项（抓取状态）失败时**仍然 200** —— 那是业务问题，不是未就绪。"""
        monkeypatch.setitem(
            health.CHECKS, "scrape", lambda: {"ok": False, "error": "boom"}
        )
        response = web_app.test_client().get("/healthz/deep")
        assert response.status_code == 200
        assert response.get_json()["status"] == "ok"


class TestDeepChecks:
    """检查项本身的单元行为。"""

    def test_db_probe_succeeds_with_healthy_db(self, migrated_db) -> None:
        result = health._check_db()
        assert result["ok"] is True

    def test_db_probe_leaves_no_residue(self, migrated_db) -> None:
        from starwatt.db.connection import connect

        health._check_db()
        with connect() as conn:
            names = [
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE name LIKE '_healthz%'"
                )
            ]
        assert names == []

    def test_scheduler_check_reflects_web_state(self, web_app, monkeypatch) -> None:
        import web

        # ``_check_scheduler`` 读 ``current_app.extensions`` → 需要应用上下文
        with web_app.app_context():
            monkeypatch.setattr(web, "_scheduler", None)
            assert health._check_scheduler()["ok"] is False

            class _Fake:
                running = True

            monkeypatch.setattr(web, "_scheduler", _Fake())
            result = health._check_scheduler()
        assert result["ok"] is True
        assert result["running"] is True

    def test_probe_is_wired_by_importing_web(self, web_app) -> None:
        """``web.py`` 必须把自己的探针挂进 ``app.extensions``（AST R4 要求的接线方式）。"""
        assert callable(web_app.extensions[health.SCHEDULER_PROBE_KEY])

    def test_missing_wiring_is_reported(self, isolated_app) -> None:
        """没接线的实例（直接 ``factory.create_app()``）算失败 —— 那种实例不抓数据。"""
        response = isolated_app.test_client().get("/healthz/deep")
        assert response.status_code == 503
        payload = response.get_json()
        assert "scheduler" in payload["failures"]
        assert "未接入" in str(payload["checks"]["scheduler"])

    def test_scrape_check_reads_state_keys(self, migrated_db) -> None:
        result = health._check_scrape()
        assert "last_scrape_at" in result
        assert "last_scrape_status" in result

    def test_required_checks_list(self) -> None:
        assert health.REQUIRED_CHECKS == ("db", "scheduler", "version")
        for name in health.REQUIRED_CHECKS:
            assert name in health.CHECKS


class TestDeepRouteNotSwallowedBySpa:
    """deep 端点必须回 JSON，不能被 SPA 兜底接管成 HTML。"""

    def test_unknown_path_under_healthz_is_json_404(self, web_app) -> None:
        response = web_app.test_client().get("/healthz/nope")
        assert response.status_code == 404
        assert response.is_json
