"""服务端会话 —— token 只存 sha256（**L17 修复**）。

安全设计
========

| 项 | 做法 |
|---|---|
| token 生成 | ``secrets.token_urlsafe(32)``（256 bit 熵） |
| **DB 存储** | **``sha256(token)`` 的十六进制串** —— DB 泄露无法直接盗用会话（L17） |
| 滑动过期 | 每次校验成功前推 ``session_hours()``（默认 24h） |
| **绝对过期** | 从 ``created_at`` 起 **30 天**硬上限（**L19 修复**） |
| 失效条件 | 绝对过期 / 空闲过期 / 用户被删 / 用户被禁用 |

L19 修复：滑动过期不能无限
==========================

旧代码每次校验都把 ``expires_at`` 前推 24 小时，**只要用户持续访问，会话
永不失效**。重写加了 30 天的**绝对上限**（由 ``created_at`` 推算，无需新增列）。
"""
from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from starwatt import timeutil
from starwatt.auth.secret import session_hours
from starwatt.db.models import Session, User
from starwatt.db.repositories import SessionRepo, UserRepo, hash_token
from starwatt.timeutil import parse_stamp, to_stamp

logger = logging.getLogger("starwatt.auth.session")

__all__ = [
    "ABSOLUTE_MAX_HOURS",
    "create_session",
    "purge_expired",
    "revoke_all_for_user",
    "revoke_session",
    "validate_session",
]

#: 会话绝对寿命上限（小时）—— 防止滑动过期导致永不失效（L19）
ABSOLUTE_MAX_HOURS = 24 * 30


def create_session(
    user_id: int,
    ip: str | None = None,
    user_agent: str | None = None,
) -> str:
    """创建会话，返回**原始 token**。

    ⚠️ 原始 token 只在此刻存在于返回值中 —— DB 里只有它的 sha256。
    """
    raw = secrets.token_urlsafe(32)
    expires_at = to_stamp(timeutil.now_cst() + timedelta(hours=session_hours()))
    SessionRepo.create(
        user_id=user_id,
        token_hash=hash_token(raw),
        expires_at=expires_at,
        ip=ip,
        user_agent=user_agent,
    )
    return raw


def _is_expired(session: Session, now) -> bool:
    """空闲过期 或 绝对过期。"""
    expires = parse_stamp(session.expires_at)
    if expires is None or now > expires:
        return True
    created = parse_stamp(session.created_at)
    if created is not None and now > created + timedelta(hours=ABSOLUTE_MAX_HOURS):
        return True
    return False


def validate_session(token: str | None) -> User | None:
    """校验 token 并**滑动续期**（不超绝对上限）。返回 ``User`` 或 ``None``。

    失效时**立即删除**该会话行（避免表膨胀）。
    """
    if not token or not isinstance(token, str):
        return None
    digest = hash_token(token)
    session = SessionRepo.get_by_token_hash(digest)
    if session is None:
        return None

    now = timeutil.now_cst()
    if _is_expired(session, now):
        SessionRepo.delete(digest)
        return None

    user = UserRepo.get_by_id(session.user_id)
    if user is None or not user.is_active:
        # 用户被删 / 被禁用 → 立刻失效所有会话
        SessionRepo.delete(digest)
        return None

    # 滑动续期，但不超过 created_at + ABSOLUTE_MAX_HOURS
    created = parse_stamp(session.created_at)
    new_expires = now + timedelta(hours=session_hours())
    if created is not None:
        hard_cap = created + timedelta(hours=ABSOLUTE_MAX_HOURS)
        if new_expires > hard_cap:
            new_expires = hard_cap
    SessionRepo.touch(digest, to_stamp(new_expires))
    return user


def revoke_session(token: str | None) -> None:
    """登出：删除该会话行。"""
    if not token:
        return
    SessionRepo.delete(hash_token(token))


def revoke_all_for_user(user_id: int) -> int:
    """踢掉某用户的全部会话（改密 / 禁用 / 删除时调用）。"""
    return SessionRepo.delete_by_user(user_id)


def purge_expired() -> int:
    """清理已过期会话，返回删除数（供调度器定期调用）。"""
    return SessionRepo.purge_expired()
