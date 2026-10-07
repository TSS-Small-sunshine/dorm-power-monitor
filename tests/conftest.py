"""StarWatt 测试公共 fixture。

M1 起本文件针对**新架构**（``starwatt/``）重写 —— 不再 import 已删除的旧模块。

提供的 fixture
==============

* ``settings_override`` —— 把全局 ``Settings`` 指向临时目录（隔离 DB / 数据目录）
* ``tmp_db``           —— 在临时目录上 ``init()`` 一个空库，用完自动清理
* ``frozen_now``       —— 冻结 ``starwatt.timeutil.now_cst()``，让时间相关断言确定
* ``no_network``       —— 禁止真实网络调用（任何用例都不得联网）
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


@pytest.fixture
def settings_override(monkeypatch, tmp_path: Path):
    """把全局 Settings 指到 ``tmp_path``，并把 bootstrap 环境变量清空。

    这样任何用例都不会碰到真实的 ``records.db`` 或真实 ``.env`` 里的密钥。
    """
    from starwatt import config as cfg

    for key in cfg.BOOTSTRAP_KEYS:
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
    cfg.override_settings(overridden)
    yield overridden
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
