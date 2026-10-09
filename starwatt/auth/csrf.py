"""CSRF 防护 —— **无状态** HMAC 方案，不占 DB、不占 cookie。

方案
====

``csrf = HMAC-SHA256(flask_secret_key, session_token)``

* **无状态**：不写 DB、不加 cookie，纯函数计算
* **绑定会话**：token 换一个会话就变，登出即失效
* **常量时间比较**：``hmac.compare_digest``
* 登录后**无需手动轮换** —— 会话 token 本身已换

校验范围（Q20 铁律）
====================

对 ``POST`` / ``PUT`` / ``PATCH`` / ``DELETE`` 一律要求 CSRF 头；
``GET`` / ``HEAD`` / ``OPTIONS`` 放行（幂等，且被 Q11 的只读模式依赖）。
"""
from __future__ import annotations

import hmac
from hashlib import sha256

from starwatt.auth.secret import flask_secret_key

__all__ = [
    "CSRF_FORM_FIELD",
    "CSRF_HEADER",
    "SAFE_METHODS",
    "issue",
    "validate",
]

#: 前端应在这些位置回传 token（任一即可）
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"

#: 幂等方法（不需要 CSRF）
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def issue(session_token: str) -> str:
    """为某个会话签发 CSRF token。

    ``session_token`` 为空时返回空串（未登录 → 无 CSRF token）。
    """
    if not session_token:
        return ""
    return hmac.new(
        flask_secret_key().encode("utf-8"),
        session_token.encode("utf-8"),
        sha256,
    ).hexdigest()


def validate(session_token: str | None, submitted: str | None) -> bool:
    """常量时间校验。任一侧缺失 → ``False``。"""
    if not session_token or not submitted:
        return False
    return hmac.compare_digest(issue(session_token), submitted)
