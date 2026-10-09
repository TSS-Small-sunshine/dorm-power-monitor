"""认证常量 —— 角色、默认参数、环境变量键名。

所有可调项都有默认值，并可由环境变量覆盖（``.env`` 或 systemd
``EnvironmentFile=``）。这些是**认证策略**参数，与「配置中心可改的运行时
配置」（Q15）不同 —— 它们必须在**登录之前**就生效，因此留在 env 层。
"""
from __future__ import annotations

__all__ = [
    "DEFAULT_LOCKOUT_MINUTES",
    "DEFAULT_LOCKOUT_THRESHOLD",
    "DEFAULT_SESSION_HOURS",
    "ENV_INITIAL_ADMIN_PASSWORD",
    "ENV_INITIAL_ADMIN_USERNAME",
    "ENV_LOCKOUT_MINUTES",
    "ENV_LOCKOUT_THRESHOLD",
    "ENV_SECRET_KEY",
    "ENV_SESSION_HOURS",
    "ROLE_ADMIN",
    "ROLE_VIEWER",
    "SESSION_COOKIE_NAME",
    "VALID_ROLES",
]

# ---------------------------------------------------------------------------
# 角色
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
VALID_ROLES = (ROLE_ADMIN, ROLE_VIEWER)

# ---------------------------------------------------------------------------
# 默认策略
# ---------------------------------------------------------------------------
DEFAULT_SESSION_HOURS = 24
DEFAULT_LOCKOUT_MINUTES = 15
DEFAULT_LOCKOUT_THRESHOLD = 5

# ---------------------------------------------------------------------------
# 会话 cookie 名（与 Flask 默认 ``session`` 区分，避免键空间冲突）
# ---------------------------------------------------------------------------
SESSION_COOKIE_NAME = "dorm_session"

# ---------------------------------------------------------------------------
# 环境变量键名
# ---------------------------------------------------------------------------
ENV_SECRET_KEY = "FLASK_SECRET_KEY"
ENV_SESSION_HOURS = "AUTH_SESSION_HOURS"
ENV_LOCKOUT_MINUTES = "AUTH_LOCKOUT_MINUTES"
ENV_LOCKOUT_THRESHOLD = "AUTH_LOCKOUT_THRESHOLD"
ENV_INITIAL_ADMIN_USERNAME = "AUTH_INITIAL_ADMIN_USERNAME"
ENV_INITIAL_ADMIN_PASSWORD = "AUTH_INITIAL_ADMIN_PASSWORD"
