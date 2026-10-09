"""``starwatt.auth`` —— 认证与授权。

模块职责
========

=========================  ==================================================
``password.py``            stdlib ``scrypt`` 散列（**D1**：替代 Rust 版 bcrypt）
``constants.py``           角色、默认策略、env 键名
``secret.py``              策略读取 + ``FLASK_SECRET_KEY`` 自举
``session.py``             服务端会话（DB 只存 sha256，**L17/L19**）
``service.py``             用户 CRUD / 登录 / 失败锁定 / bootstrap 建号
``csrf.py``                无状态 HMAC CSRF token
``decorators.py``          Flask 视图装饰器（**需要 Flask，按需导入**）
=========================  ==================================================

本 ``__init__`` **不导入 Flask** —— 这样 CLI / 调度器 / 测试可以只依赖
``password`` / ``service`` 而不拉起 Web 栈。需要装饰器时显式
``from starwatt.auth import decorators``。
"""
from __future__ import annotations

from starwatt.auth.constants import (
    DEFAULT_LOCKOUT_MINUTES,
    DEFAULT_LOCKOUT_THRESHOLD,
    DEFAULT_SESSION_HOURS,
    ROLE_ADMIN,
    ROLE_VIEWER,
    SESSION_COOKIE_NAME,
    VALID_ROLES,
)
from starwatt.auth.password import (
    hash_password,
    needs_rehash,
    validate_strength,
    verify_password,
)
from starwatt.auth.service import (
    AuthResult,
    authenticate,
    change_password,
    create_user,
    delete_user,
    ensure_bootstrap_admin,
    is_locked,
    set_disabled,
    set_role,
    write_audit,
)

__all__ = [
    "DEFAULT_LOCKOUT_MINUTES",
    "DEFAULT_LOCKOUT_THRESHOLD",
    "DEFAULT_SESSION_HOURS",
    "ROLE_ADMIN",
    "ROLE_VIEWER",
    "SESSION_COOKIE_NAME",
    "VALID_ROLES",
    "AuthResult",
    "authenticate",
    "change_password",
    "create_user",
    "delete_user",
    "ensure_bootstrap_admin",
    "hash_password",
    "is_locked",
    "needs_rehash",
    "set_disabled",
    "set_role",
    "validate_strength",
    "verify_password",
    "write_audit",
]
