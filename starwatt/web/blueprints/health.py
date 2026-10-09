"""``/healthz`` —— 探活端点（容器 HEALTHCHECK 与 systemd 用）。

铁律：**探活端点自己不能变成故障源**
====================================

* 不碰数据库（``meta`` 表还没建时也要能回 200）
* 不碰网络（不查学校接口、不推飞书）
* 不要求认证（Docker / systemd / nginx 都要能匿名探）

所以它只回答「进程活着吗 + 版本是什么 + 现在几点」。
"""
from __future__ import annotations

from flask import Blueprint, jsonify

from starwatt import version_string
from starwatt.timeutil import stamp

bp = Blueprint("health", __name__)

__all__ = ["bp"]

#: 版本串（与 ``starwatt.__version__`` 同源）
APP_VERSION = version_string()


@bp.get("/healthz")
def healthz():
    """无认证、无 IO 的探活响应。"""
    return jsonify(status="ok", version=APP_VERSION, time=stamp())
