"""密码散列 —— 用 Python **标准库** ``hashlib.scrypt`` 替代 bcrypt（D1 / B1）。

为什么换掉 bcrypt
=================

``bcrypt`` 4.x/5.x 是 **Rust** 实现，**没有 armv7 wheel**，而 Q3 要求覆盖
全架构（见 ``docs/BLOCKER_FIXES.md §1``）。``hashlib.scrypt`` 是 stdlib，
**零第三方依赖**，且安全性不弱于 bcrypt：

| 维度 | bcrypt (rounds=12) | **scrypt (N=2^14)** |
|---|---|---|
| 内存消耗 | ~4 KB | **16 MB** |
| 抗 GPU | 中 | **强**（内存硬） |
| 抗 ASIC | 中 | **强** |
| 耗时 | ~250 ms | ~100 ms（可调） |
| OWASP 推荐 | ✅ | ✅ |
| 第三方依赖 | ❌ 需要 Rust | ✅ **stdlib** |

存储格式
========

::

    scrypt$<N>$<r>$<p>$<salt_b64>$<dk_b64>

自描述格式 —— 将来调参（例如 N 翻倍）后，**旧哈希仍可验证**，
并在用户下次登录时通过 :func:`needs_rehash` 惰性升级。

升级注意（D1 的代价）
=====================

旧库的 ``$2b$...`` bcrypt 哈希**无法验证**（没有 bcrypt 依赖）。
:func:`verify_password` 对它一律返回 ``False``，用户需走
「bootstrap 随机密码」流程重设一次（与 Q19 一致）。

密码强度规则与旧代码**逐字一致**（见 ``tests/regression/fixtures/behaviors.json``
的 ``_validate_password_strength``），避免前端提示文案漂移。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

__all__ = [
    "ALGO",
    "MAX_LENGTH",
    "MIN_LENGTH",
    "SCRYPT_N",
    "SCRYPT_P",
    "SCRYPT_R",
    "hash_password",
    "needs_rehash",
    "validate_strength",
    "verify_password",
]

ALGO = "scrypt"

#: scrypt 参数（内存 128*N*r = 16 MiB，耗时 ~100ms）
SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1
_DKLEN = 32
_SALT_LEN = 16

#: 密码长度边界
MIN_LENGTH = 8
MAX_LENGTH = 128

#: 特殊字符集合（与旧代码 ``_validate_password_strength`` 一致）
_SPECIALS = set("!@#$%^&*()-_=+[]{};:'\",.<>/?\\|`~")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def _derive(plain: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        plain.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN
    )


def hash_password(plain: str) -> str:
    """散列一个密码，返回自描述的 ``scrypt$...`` 字符串。"""
    if not plain:
        raise ValueError("password must be non-empty")
    salt = secrets.token_bytes(_SALT_LEN)
    dk = _derive(plain, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"{ALGO}${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(dk)}"


def verify_password(plain: str, stored: str) -> bool:
    """常量时间校验。

    对以下情况一律返回 ``False``（不抛异常）：

    * ``stored`` 为空 / 不是 ``scrypt$`` 格式（含旧的 bcrypt ``$2b$`` 哈希）
    * 参数不可解析
    * 密码不匹配
    """
    if not plain or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != ALGO:
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = _unb64(parts[4])
        expected = _unb64(parts[5])
    except (ValueError, TypeError):
        return False

    try:
        actual = _derive(plain, salt, n, r, p)
    except (ValueError, OverflowError):
        # 参数越界（例如 N 不是 2 的幂）—— 视为校验失败而不是崩溃
        return False
    return hmac.compare_digest(actual, expected)


def needs_rehash(stored: str) -> bool:
    """``stored`` 是否用**过时参数**散列（需在下次登录时惰性升级）。"""
    parts = (stored or "").split("$")
    if len(parts) != 6 or parts[0] != ALGO:
        return True  # 未知格式 → 需要重建
    try:
        return (int(parts[1]), int(parts[2]), int(parts[3])) != (
            SCRYPT_N,
            SCRYPT_R,
            SCRYPT_P,
        )
    except (ValueError, TypeError):
        return True


def validate_strength(password: str) -> str | None:
    """返回 ``None`` 表示合格，否则返回**中文/英文错误提示**。

    提示文案与旧代码逐字一致（``behaviors.json`` 已固化），避免前端 toast 漂移。
    """
    if not password:
        return "password required"
    if len(password) < MIN_LENGTH:
        return f"password must be at least {MIN_LENGTH} characters"
    if len(password) > MAX_LENGTH:
        return f"password must be {MAX_LENGTH} characters or less"
    if not any(ch.isupper() for ch in password):
        return "password must contain an uppercase letter"
    if not any(ch.isdigit() for ch in password):
        return "password must contain a digit"
    if not any(ch in _SPECIALS for ch in password):
        return "password must contain a special character"
    return None
