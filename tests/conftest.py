"""M0 阶段的 conftest —— 只为跑通契约快照生成器。

M1 引入 starwatt/timeutil.py 后，本文件会重写为正式 fixture。
现在必须与旧代码（config.py / db.py / web.py / dorm_power.py / auth.py）兼容。

设计说明
========

* ``tmp_db``     —— 把 ``config.DB_PATH`` 指向临时文件，隔离每个用例。
* ``frozen_now`` —— 冻结时间到 ``FIXED_NOW``。
  旧代码用 ``from datetime import datetime`` 后直接 ``datetime.now()``，
  所以逐个模块把它们的 ``datetime`` 符号替换为冻结子类（而不是 patch 全局）。
* ``no_network`` —— 禁止真实网络调用，保证快照生成离线可重现。
"""
from __future__ import annotations

import datetime as _dt
import importlib
import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[1]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

# 朴素 CST 的固定时刻（与项目「全栈朴素本地时间」约定一致）
FIXED_NOW = _dt.datetime(2026, 10, 6, 12, 0, 0)


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """把 DB_PATH 指向临时文件，隔离每个用例。"""
    db_file = tmp_path / "test.db"
    import config

    monkeypatch.setattr(config, "DB_PATH", str(db_file), raising=False)

    import db as dbmod

    importlib.reload(dbmod)          # 让 db 重新读 DB_PATH
    dbmod.init()
    yield db_file
    dbmod.init()                     # 幂等收尾


@pytest.fixture
def frozen_now(monkeypatch):
    """冻结时间，逐个模块替换其 ``datetime`` / ``date`` 符号。"""

    class _FrozenDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):       # noqa: ARG003
            return FIXED_NOW

        @classmethod
        def today(cls):
            return FIXED_NOW

    class _FrozenDate(_dt.date):
        @classmethod
        def today(cls):
            return FIXED_NOW.date()

    for name in ("dorm_power", "auth", "web", "db"):
        try:
            mod = importlib.import_module(name)
        except Exception:            # noqa: BLE001 — 模块可能尚未存在
            continue
        if getattr(mod, "datetime", None) is _dt.datetime:
            monkeypatch.setattr(mod, "datetime", _FrozenDateTime)
        if getattr(mod, "date", None) is _dt.date:
            monkeypatch.setattr(mod, "date", _FrozenDate)
    yield FIXED_NOW


@pytest.fixture
def no_network(monkeypatch):
    """禁止真实网络调用（快照生成必须离线可重现）。"""
    import requests

    def _boom(*_args, **_kwargs):
        raise RuntimeError("network disabled in tests")

    monkeypatch.setattr(requests.Session, "get", _boom, raising=False)
    monkeypatch.setattr(requests.Session, "post", _boom, raising=False)
    yield
