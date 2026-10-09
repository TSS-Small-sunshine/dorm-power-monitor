"""认证用例层 —— 用户 CRUD / 登录 / 失败锁定 / bootstrap 建号。

锁定策略
========

* 窗口内失败 **≥5 次**（``AUTH_LOCKOUT_THRESHOLD``）→ 锁定 **15 分钟**
  （``AUTH_LOCKOUT_MINUTES``），按 ``(username, ip)`` 维度计数
* 登录成功 → 清空该 ``(username, ip)`` 的失败记录
* 窗口与阈值都可经 env 覆盖，非法值回落默认（认证路径不抛异常）

Bootstrap 建号（Q19 / B4）
=========================

``install.sh`` 生成随机密码写入 ``.env`` 的 ``BOOTSTRAP_ADMIN_PASSWORD``；
**首次启动**时若 ``users`` 为空则用它建 admin（``must_change_password=1``），
然后**立即从 ``.env`` 抹掉**该行（用后即焚，不留后门）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import timedelta

from starwatt import timeutil
from starwatt.auth import password as pwd
from starwatt.auth import session as session_mod
from starwatt.auth.constants import ROLE_ADMIN, ROLE_VIEWER, VALID_ROLES
from starwatt.auth.secret import lockout_minutes, lockout_threshold
from starwatt.db.models import User
from starwatt.db.repositories import AuditRepo, FailedAttemptRepo, UserRepo
from starwatt.timeutil import to_stamp

logger = logging.getLogger("starwatt.auth.service")

__all__ = [
    "ENV_BOOTSTRAP_PASSWORD",
    "AuthResult",
    "authenticate",
    "change_password",
    "clear_failed",
    "create_user",
    "delete_user",
    "ensure_bootstrap_admin",
    "is_locked",
    "record_failed",
    "set_disabled",
    "set_role",
    "write_audit",
]

#: install.sh 写入的 bootstrap 密码键名（用后即焚）
ENV_BOOTSTRAP_PASSWORD = "BOOTSTRAP_ADMIN_PASSWORD"
_ENV_BOOTSTRAP_USERNAME = "BOOTSTRAP_ADMIN_USERNAME"


@dataclass(frozen=True)
class AuthResult:
    """登录结果。

    ``error`` 取值：``None``（成功）/ ``"invalid"`` / ``"locked"`` /
    ``"disabled"`` / ``"must_change_password"``（登录成功但需先改密，此时
    ``user`` 与 ``token`` 仍然有效，前端应跳转改密页）。
    """

    ok: bool
    user: User | None = None
    token: str | None = None
    error: str | None = None

    @property
    def must_change_password(self) -> bool:
        return self.error == "must_change_password"


# ---------------------------------------------------------------------------
# 审计（唯一入口，便于统一脱敏）
# ---------------------------------------------------------------------------
def write_audit(
    action: str,
    user_id: int | None = None,
    target: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    details: dict | None = None,
) -> None:
    """写审计日志。

    ⚠️ ``details`` **绝不可**包含密码 / 密码哈希 / token / openid（Q20 铁律）。
    """
    AuditRepo.append(
        action=action,
        user_id=user_id,
        target=target,
        ip=ip,
        user_agent=user_agent,
        details=details,
    )


# ---------------------------------------------------------------------------
# 失败锁定
# ---------------------------------------------------------------------------
def _window_start() -> str:
    return to_stamp(timeutil.now_cst() - timedelta(minutes=lockout_minutes()))


def is_locked(username: str, ip: str | None = None) -> bool:
    """窗口内失败次数是否已达阈值。"""
    if not username:
        return False
    return (
        FailedAttemptRepo.count_since(username, _window_start(), ip)
        >= lockout_threshold()
    )


def record_failed(username: str, ip: str | None = None) -> int:
    """记一次失败，返回窗口内累计次数。"""
    FailedAttemptRepo.record(username, ip)
    return FailedAttemptRepo.count_since(username, _window_start(), ip)


def clear_failed(username: str, ip: str | None = None) -> int:
    """清空失败记录（登录成功时调用）。"""
    return FailedAttemptRepo.clear(username, ip)


# ---------------------------------------------------------------------------
# 用户 CRUD
# ---------------------------------------------------------------------------
def create_user(
    username: str,
    password: str,
    role: str = ROLE_VIEWER,
    must_change_password: bool = False,
) -> User:
    """创建用户。

    Raises:
        ValueError: 用户名空 / 角色非法 / 密码强度不合格。
        sqlite3.IntegrityError: 用户名已存在（调用方转 409）。
    """
    username = (username or "").strip()
    if not username:
        raise ValueError("username must be non-empty")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES!r}, got {role!r}")

    problem = pwd.validate_strength(password)
    if problem:
        raise ValueError(problem)

    return UserRepo.create(
        username=username,
        password_hash=pwd.hash_password(password),
        role=role,
        must_change_password=must_change_password,
    )


def change_password(user_id: int, new_password: str) -> None:
    """改密：校验强度 → 更新哈希 → 清除强制改密标志 → **踢掉全部会话**。"""
    problem = pwd.validate_strength(new_password)
    if problem:
        raise ValueError(problem)
    UserRepo.update_password(
        user_id, pwd.hash_password(new_password), must_change=False
    )
    session_mod.revoke_all_for_user(user_id)


def reset_password(user_id: int, new_password: str, *, must_change: bool = True) -> None:
    """**管理员重置**他人密码。

    与 :func:`change_password`（本人改密）只差 ``must_change``：
    临时密码由管理员口头/纸条转达，必须由用户自己再改一次才算「本人持有」
    （Q19 的强制首登改密，见 :mod:`starwatt.auth.decorators` 的拦截）。
    """
    problem = pwd.validate_strength(new_password)
    if problem:
        raise ValueError(problem)
    UserRepo.update_password(
        user_id, pwd.hash_password(new_password), must_change=must_change
    )
    session_mod.revoke_all_for_user(user_id)


def set_role(user_id: int, role: str) -> None:
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES!r}, got {role!r}")
    UserRepo.update_role(user_id, role)


def set_disabled(user_id: int, disabled: bool) -> None:
    """禁用/启用用户。禁用时**立即踢掉其全部会话**。"""
    UserRepo.set_disabled(user_id, disabled)
    if disabled:
        session_mod.revoke_all_for_user(user_id)


def delete_user(user_id: int) -> None:
    """删除用户。外键 ``ON DELETE CASCADE`` 会一并删除其会话。"""
    session_mod.revoke_all_for_user(user_id)
    UserRepo.delete(user_id)


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def authenticate(
    username: str,
    password: str,
    ip: str | None = None,
    user_agent: str | None = None,
) -> AuthResult:
    """校验凭据，成功时创建会话。

    顺序很重要：**先查锁定，再验密码** —— 否则攻击者可以通过响应时间
    区分「被锁定」与「密码错误」。
    """
    username = (username or "").strip()
    if not username or not password:
        return AuthResult(ok=False, error="invalid")

    if is_locked(username, ip):
        write_audit("login.locked", target=username, ip=ip, user_agent=user_agent)
        return AuthResult(ok=False, error="locked")

    user = UserRepo.get_by_username(username)
    if user is None or not pwd.verify_password(password, user.password_hash):
        record_failed(username, ip)
        write_audit("login.failed", target=username, ip=ip, user_agent=user_agent)
        return AuthResult(ok=False, error="invalid")

    if not user.is_active:
        write_audit("login.disabled", user_id=user.id, target=username, ip=ip)
        return AuthResult(ok=False, error="disabled")

    # 惰性重散列（scrypt 参数升级后自动迁移旧哈希）
    if pwd.needs_rehash(user.password_hash):
        UserRepo.update_password(
            user.id,
            pwd.hash_password(password),
            must_change=user.needs_password_change,
        )
        refreshed = UserRepo.get_by_id(user.id)
        if refreshed is not None:
            user = refreshed

    clear_failed(username, ip)
    UserRepo.mark_login(user.id)
    token = session_mod.create_session(user.id, ip=ip, user_agent=user_agent)
    write_audit(
        "login.success",
        user_id=user.id,
        target=username,
        ip=ip,
        user_agent=user_agent,
        details={"role": user.role},
    )
    return AuthResult(
        ok=True,
        user=user,
        token=token,
        error="must_change_password" if user.needs_password_change else None,
    )


# ---------------------------------------------------------------------------
# Bootstrap（Q19 / B4）
# ---------------------------------------------------------------------------
def _wipe_env_key(key: str) -> bool:
    """从 ``.env`` 中删除 ``key=`` 那一行（用后即焚）。"""
    from starwatt.config import ENV_FILE

    try:
        if not ENV_FILE.exists():
            return False
        lines = [
            line
            for line in ENV_FILE.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{key}=")
        ]
        ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning("无法从 .env 抹掉 %s：%s", key, exc)
        return False


def ensure_bootstrap_admin() -> User | None:
    """首次启动建号（幂等）。

    触发条件（**三者同时满足**）：

    1. ``users`` 表为空
    2. ``.env`` 里有 ``BOOTSTRAP_ADMIN_PASSWORD``
    3. 该密码通过强度校验

    建号后立即把该行从 ``.env`` 抹掉，并设 ``must_change_password=1``。
    """
    if UserRepo.count() > 0:
        return None

    raw = (os.environ.get(ENV_BOOTSTRAP_PASSWORD) or "").strip()
    if not raw:
        logger.info(
            "未发现 %s，跳过 bootstrap 建号（请走 OOBE 向导）",
            ENV_BOOTSTRAP_PASSWORD,
        )
        return None

    problem = pwd.validate_strength(raw)
    if problem:
        logger.warning("bootstrap 密码强度不合格（%s），跳过建号", problem)
        return None

    username = (os.environ.get(_ENV_BOOTSTRAP_USERNAME) or ROLE_ADMIN).strip()
    user = UserRepo.create(
        username=username,
        password_hash=pwd.hash_password(raw),
        role=ROLE_ADMIN,
        must_change_password=True,
    )
    os.environ.pop(ENV_BOOTSTRAP_PASSWORD, None)
    _wipe_env_key(ENV_BOOTSTRAP_PASSWORD)
    write_audit(
        "bootstrap.admin_created",
        user_id=user.id,
        target=username,
        details={"source": "env"},
    )
    logger.warning(
        "bootstrap: 已创建管理员 %r（强制首登改密），并已从 .env 抹掉该密码",
        username,
    )
    return user

