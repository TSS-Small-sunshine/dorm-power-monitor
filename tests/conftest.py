"""StarWatt 测试公共 fixture。

M1 起本文件针对**新架构**（``starwatt/``）重写 —— 不再 import 已删除的旧模块。

提供的 fixture
==============

* ``settings_override`` —— 把全局 ``Settings`` 指向临时目录（隔离 DB / 数据目录）
* ``tmp_db``           —— 在临时目录上 ``init()`` 一个空库，用完自动清理
* ``frozen_now``       —— 冻结 ``starwatt.timeutil.now_cst()``，让时间相关断言确定
* ``no_network``       —— 禁止真实网络调用（任何用例都不得联网）
* ``web_app``          —— Flask 应用（隔离 DB）
* ``admin_client``     —— 已登录**管理员**的测试客户端（含 CSRF 头）
* ``viewer_client``    —— 已登录**普通用户**的测试客户端（验证 403）
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

#: 所有时间相关断言的基准时刻（朴素 CST）
FIXED_NOW = datetime(2026, 10, 6, 12, 0, 0)

#: 认证策略环境变量 —— 测试里一律清空以保证确定性
AUTH_ENV_KEYS: tuple[str, ...] = (
    "AUTH_SESSION_HOURS",
    "AUTH_LOCKOUT_MINUTES",
    "AUTH_LOCKOUT_THRESHOLD",
    "AUTH_INITIAL_ADMIN_USERNAME",
    "AUTH_INITIAL_ADMIN_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "BOOTSTRAP_ADMIN_USERNAME",
)


@pytest.fixture
def settings_override(monkeypatch, tmp_path: Path):
    """把全局 Settings 指到 ``tmp_path``，并把 bootstrap 环境变量清空。

    这样任何用例都不会碰到真实的 ``records.db`` 或真实 ``.env`` 里的密钥。
    """
    from starwatt import config as cfg

    for key in cfg.BOOTSTRAP_KEYS:
        monkeypatch.delenv(key, raising=False)
    # 认证策略变量也清掉 —— 否则宿主机 .env 里的值会让断言不确定
    for key in AUTH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    overridden = cfg.load_settings(
        {
            "DORM_DATA_DIR": str(data_dir),
            "DB_PATH": str(data_dir / "test.db"),
            "FLASK_PORT": "5099",
            "FLASK_SECRET_KEY": "test-secret-key-not-for-production",
        }
    )
    # ⚠️ 必须**同时**写回 os.environ：starwatt.auth.secret 读的是进程环境，
    # 不写的话它会以为密钥缺失，进而往真实 .env 追加一行（污染开发机）。
    monkeypatch.setenv("FLASK_SECRET_KEY", "test-secret-key-not-for-production")
    cfg.override_settings(overridden)

    # ⚠️ 配置缓存是**进程级全局**的。换库时必须清掉，否则上一个用例读到的
    # 值（例如 log_overrides={"db":"DEBUG"}）会静默泄漏到下一个用例 ——
    # 这类跨用例污染会让测试结果变得不可信。
    from starwatt.config_registry import store as cfg_store

    cfg_store.invalidate()

    yield overridden
    cfg_store.invalidate()
    cfg.override_settings(None)


@pytest.fixture
def tmp_db(settings_override) -> Iterator[Path]:
    """在临时目录上初始化一个空库。

    依赖 ``settings_override``，所以调用本 fixture 即自动隔离。
    """
    from starwatt import db

    db.init()
    yield settings_override.db_path
    db.init()  # 幂等收尾


@pytest.fixture
def frozen_now(monkeypatch) -> datetime:
    """冻结 ``starwatt.timeutil.now_cst()`` 到 ``FIXED_NOW``。

    只 patch 这一个函数 —— 其余模块都应通过它取时间（AST 守卫 R7 强制），
    所以不需要逐模块替换 ``datetime`` 符号。
    """
    from starwatt import timeutil

    monkeypatch.setattr(timeutil, "now_cst", lambda: FIXED_NOW)
    return FIXED_NOW


@pytest.fixture
def no_network(monkeypatch):
    """禁止真实网络调用。"""
    import requests

    def _boom(*_args, **_kwargs):
        raise requests.RequestException("network disabled in tests")

    monkeypatch.setattr(requests.Session, "get", _boom, raising=False)
    monkeypatch.setattr(requests.Session, "post", _boom, raising=False)
    monkeypatch.setattr(requests, "get", _boom, raising=False)
    monkeypatch.setattr(requests, "post", _boom, raising=False)
    yield


@pytest.fixture
def web_app(settings_override, tmp_db):
    """Flask 应用（隔离 DB）+ 调度器单例复位。

    ``import web`` 会执行 ``setup_logging()`` 与 ``create_app()``；
    模块只导入一次，但 DB 路径是**每次请求**从 ``get_settings()`` 读的，
    所以 ``tmp_db`` 的隔离仍然生效。

    📌 放在 conftest 而不是某个测试文件里：``test_web_entry`` 与
    ``test_web_api`` 都要用它。
    """
    import web

    web._scheduler = None
    yield web.app
    web._scheduler = None


#: 测试用密码（满足强度策略：≥8 位 + 大写 + 数字 + 特殊字符）
GOOD_PASSWORD = "Str0ng-Pass!"


@pytest.fixture
def admin_client(web_app):
    """**已登录管理员**的测试客户端（Cookie + CSRF 头都已就位）。

    走真实登录端点而不是伪造会话 —— 这样测试顺带覆盖了登录链路，
    也保证「配置 API 只能管理员访问」的断言是真的在测访问控制。

    Returns:
        已带 ``dorm_session`` Cookie 与 ``X-CSRF-Token`` 头的 test client。
    """
    from starwatt.auth import csrf as csrf_mod
    from starwatt.auth import service as auth
    from starwatt.auth.constants import ROLE_ADMIN

    auth.create_user("admin", GOOD_PASSWORD, role=ROLE_ADMIN)
    client = web_app.test_client()
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf_mod.issue(token)
    return client


@pytest.fixture
def viewer_client(web_app):
    """**已登录普通用户**的测试客户端（用于验证 403）。"""
    from starwatt.auth import csrf as csrf_mod
    from starwatt.auth import service as auth
    from starwatt.auth.constants import ROLE_VIEWER

    auth.create_user("viewer", GOOD_PASSWORD, role=ROLE_VIEWER)
    client = web_app.test_client()
    response = client.post(
        "/api/auth/login", json={"username": "viewer", "password": GOOD_PASSWORD}
    )
    assert response.status_code == 200, response.get_json()
    token = response.get_json()["token"]
    client.environ_base["HTTP_X_CSRF_TOKEN"] = csrf_mod.issue(token)
    return client
