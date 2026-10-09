"""``secret=True`` 配置项的加解密 —— AES-256-GCM（Q15 + Q17）。

为什么是 GCM 而不是 CBC
=======================

GCM 是**认证加密**：密文被篡改会在解密时**立即失败**，而不是吐出一段
看似正常的垃圾。配置项被改坏时，「报错」远比「静默用错值」安全
（fail-closed，Q17）。

密钥派生
========

``FLASK_SECRET_KEY``（64 位十六进制）→ ``HKDF-SHA256`` → 32 字节 AES 密钥。

用 HKDF 而不是直接截断：``FLASK_SECRET_KEY`` 同时用于 Flask 会话签名，
两个用途共享同一份密钥材料是密码学上的坏习惯；HKDF 用不同的 ``info``
标签派生出**用途隔离**的子密钥。

存储格式
========

::

    enc:v1:<nonce_b64>:<tag_b64>:<ciphertext_b64>

``enc:v1:`` 前缀让**明文与密文可区分** —— 迁移期旧库里可能存着明文
secret（旧代码就这么干的），读到没有前缀的值时按明文处理并**在下次写入时
自动升级为密文**。

密钥丢失的后果（RK14）
=====================

``FLASK_SECRET_KEY`` 丢了 → 所有密文**永久无法解密**。
:func:`decrypt` 会抛出明确的 :class:`SecretDecryptError`，
而不是静默返回空串（否则用户会以为配置丢了，实际是密钥换了）。
"""
from __future__ import annotations

import base64
import logging

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import HKDF
from Crypto.Random import get_random_bytes

from starwatt.auth.secret import flask_secret_key

logger = logging.getLogger("starwatt.config")

__all__ = [
    "PREFIX",
    "SecretDecryptError",
    "decrypt",
    "encrypt",
    "is_encrypted",
    "mask",
]

#: 密文前缀（用于区分明文与密文）
PREFIX = "enc:v1:"

#: HKDF 的用途标签 —— 与 Flask 会话签名隔离
_INFO = b"starwatt.config.secret.v1"

#: GCM nonce 长度（96 bit，GCM 推荐值）
_NONCE_LEN = 12

_AES_KEY_LEN = 32  # AES-256


class SecretDecryptError(RuntimeError):
    """密文无法解密。

    最常见原因：``FLASK_SECRET_KEY`` 变了（重装 / 换了 .env）。
    此时**不能**静默降级 —— 必须让用户知道配置没丢、是密钥换了。
    """


def _aes_key() -> bytes:
    """从 ``FLASK_SECRET_KEY`` 经 HKDF 派生 32 字节 AES 密钥。"""
    material = flask_secret_key().encode("utf-8")
    return HKDF(
        master=material,
        key_len=_AES_KEY_LEN,
        salt=None,
        hashmod=SHA256,
        num_keys=1,
        context=_INFO,
    )


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def is_encrypted(value: str | None) -> bool:
    """``value`` 是否是本模块产出的密文。"""
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(plain: str) -> str:
    """加密明文；空串原样返回（空值不加密，省一次派生）。

    Raises:
        ValueError: 输入不是字符串。
    """
    if not isinstance(plain, str):
        raise ValueError("secret must be a string")
    if plain == "":
        return ""

    nonce = get_random_bytes(_NONCE_LEN)
    cipher = AES.new(_aes_key(), AES.MODE_GCM, nonce=nonce)
    ciphertext, tag = cipher.encrypt_and_digest(plain.encode("utf-8"))
    return f"{PREFIX}{_b64(nonce)}:{_b64(tag)}:{_b64(ciphertext)}"


def decrypt(stored: str | None) -> str:
    """解密。

    * 空值 → ``""``
    * **无** ``enc:v1:`` 前缀 → 视为**明文**（迁移期兼容旧库）并原样返回
    * 有前缀但解不开 → 抛 :class:`SecretDecryptError`

    Raises:
        SecretDecryptError: 格式损坏或认证失败（密钥变更 / 数据被篡改）。
    """
    if not stored:
        return ""
    if not is_encrypted(stored):
        # 旧库里的明文 secret —— 调用方在下次写入时会自动升级为密文
        return stored

    body = stored[len(PREFIX) :]
    parts = body.split(":")
    if len(parts) != 3:
        raise SecretDecryptError("密文格式损坏（应为 nonce:tag:ct 三段）")
    try:
        nonce = _unb64(parts[0])
        tag = _unb64(parts[1])
        ciphertext = _unb64(parts[2])
    except (ValueError, TypeError) as exc:
        raise SecretDecryptError("密文 base64 解码失败") from exc

    try:
        cipher = AES.new(_aes_key(), AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8")
    except (ValueError, KeyError) as exc:
        raise SecretDecryptError(
            "解密失败 —— 通常是 FLASK_SECRET_KEY 变了。"
            "请恢复原来的密钥，或重新填写该配置项"
        ) from exc


#: 短于这个长度就**全部打码**
_MIN_MASK_LEN = 8


def mask(value: str | None) -> str:
    """脱敏：``••••`` + 末 4 位（Q15 要求 GET 永不返回明文）。

    * 空值 → ``""``（前端据此显示「未设置」）
    * 长度 < :data:`_MIN_MASK_LEN` → **全部打码**

    为什么短值要全打码：``mask("abcde")`` 若按「末 4 位」规则会返回
    ``••••bcde`` —— 5 个字符里露出 4 个，等于没脱敏。
    真实的 secret（openid 28 位、token 32+ 位）远长于阈值，行为不变。
    """
    if not value:
        return ""
    if len(value) < _MIN_MASK_LEN:
        return "•" * len(value)
    return "••••" + value[-4:]
