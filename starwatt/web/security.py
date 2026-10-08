"""访问控制装配（Q11）—— 认证 / CSRF / 内部 token。

三层防线
========

============  ==========================================================
层             做什么
============  ==========================================================
``read_access``  读接口：默认**要求登录**；``public_readonly=1`` 时允许匿名
``admin_access`` 管理接口：仅管理员（``require_admin``）
``internal_token`` 内部 API：``api_internal_token`` 常量时间比对（Q17）
============  ==========================================================

为什么还要包一层
================

``starwatt.auth.decorators`` 里的 ``require_auth(allow_anonymous_read=True)``
是「**无条件**放行安全方法」；而 Q11 要的是「**由 ``public_readonly`` 配置决定**
是否放行」。两者差一个开关，所以这里把它包成 :func:`read_access`：

::

    public_readonly=0（默认）→ 未登录读接口也返回 401
    public_readonly=1         → 未登录可以只读浏览（家用场景友好）

📌 即便匿名放行，也**先加载身份**（``g.current_user``）—— 前端据此渲染
「登录 / 已登录」两种状态，而不是靠猜。
"""
from __future__ import annotations

import functools
import hmac
import logging

from flask import request

from starwatt.auth.decorators import require_admin, require_auth
from starwatt.config_registry import get_bool, get_str

logger = logging.getLogger("starwatt.web")

__all__ = [
    "INTERNAL_TOKEN_HEADER",
    "admin_access",
    "internal_token_ok",
    "read_access",
]

#: 内部调用方（调度器 / 脚本）携带 token 的头
INTERNAL_TOKEN_HEADER = "X-Internal-Token"


def read_access(view):
    """读接口守卫（Q11）。"""
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if get_bool("public_readonly", False):
            # 匿名放行，但仍加载身份（视图可据此渲染登录态）
            return require_auth(allow_anonymous_read=True)(view)(*args, **kwargs)
        return require_auth()(view)(*args, **kwargs)

    return wrapper


def admin_access(view):
    """管理接口守卫：仅管理员。"""
    return require_admin(view)


def internal_token_ok(token: str | None = None) -> bool:
    """校验内部 API token（常量时间比对，避免时序侧信道）。

    未配置 ``api_internal_token`` 时**一律拒绝** —— 空 token 不等于「不校验」。

    ``token`` 为 ``None`` 时从请求头读；**在请求上下文之外**（调度器调用）
    直接返回 ``False`` 而不是抛 ``RuntimeError``。
    """
    expected = get_str("api_internal_token", "")
    if not expected:
        return False

    candidate = token
    if candidate is None:
        try:
            candidate = request.headers.get(INTERNAL_TOKEN_HEADER, "")
        except RuntimeError:  # 无请求上下文（后台线程 / 调度器）
            return False
    return hmac.compare_digest(str(candidate or ""), expected)
