"""飞书事件的**解密**与**签名校验**（Q17 安全，覆盖率门禁 **100%**）。

为什么单独一个模块
==================

这是全项目**唯一**处理「外来输入 + 密钥」的地方，也是最容易被写错的地方：

* 解密要兼容飞书三代 SDK 的三种密文布局（见下）
* 签名校验曾经**有 fail-open 漏洞**（审计 B4）：三个签名头全空 → 直接放行，
  攻击者可以伪造事件。重写改为 **fail-closed**。

三条解密路径（按顺序尝试）
==========================

===  ===============================================================  ============
路径  密钥 / IV                                                        来源
===  ===============================================================  ============
1    ``key = SHA256(encrypt_key)``（32B）、``IV = 同一摘要前 16B``      飞书官方规范
     AES-256-CBC，明文 = ``[16B 随机盐][JSON][PKCS7]``
2    ``key = hex_decode(encrypt_key)``（16B）→ AES-128-CBC，            旧版 SDK
     ``IV = 密文前 16B``
3    ``key = encrypt_key.encode()``（32B）→ AES-256-CBC，               旧版 SDK
     ``IV = 密文前 16B``
===  ===============================================================  ============

📌 路径 1 的「16 字节盐」必须**硬切**（``unpadded[16:]``），不能去搜 ``{``
—— 随机盐有约 6% 的概率包含 ``0x7B``，搜出来的位置会落在盐内部，把 UTF-8
解码弄坏。这是 legacy 踩过的坑（Round 10 注释），重写保留。

fail-closed（审计 B4 / D5）
===========================

====================  ==================================================
情况                   行为
====================  ==================================================
加密事件缺签名头       **拒绝**（``False``）
``url_verification``   必须带签名头并通过校验；否则拒绝
其它 v1 事件缺签名头   **拒绝**（legacy 是放行 —— 这是漏洞，重写修掉）
====================  ==================================================

需要放行「控制台未开签名」的场景时，显式传 ``allow_unsigned=True``
—— 把宽松变成**调用方的显式选择**，而不是默认行为。

本模块**不依赖 Flask / DB**：密钥由调用方传入，纯函数、易测。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from collections.abc import Iterable, Mapping
from typing import Any

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "CryptoError",
    "SIGNATURE_HEADERS",
    "decrypt_payload",
    "do_sha256_verify",
    "is_encrypted",
    "verify_lark_signature",
    "verify_token",
]

#: 飞书 v2 事件的三个签名头（顺序即文档顺序）
SIGNATURE_HEADERS: tuple[str, str, str] = (
    "X-Lark-Signature",
    "X-Lark-Request-Timestamp",
    "X-Lark-Request-Nonce",
)

#: 路径 1 的明文盐长度（飞书规范固定 16 字节）
_SALT_LEN = 16


class CryptoError(RuntimeError):
    """解密 / 签名校验失败。消息可直接进日志（**不含密钥**）。"""


# ---------------------------------------------------------------------------
# 解密
# ---------------------------------------------------------------------------
def is_encrypted(body: Any) -> bool:
    """body 是否是加密事件（含非空 ``encrypt`` 字段）。"""
    return (
        isinstance(body, dict)
        and isinstance(body.get("encrypt"), str)
        and bool(body["encrypt"])
    )


def _path1(ciphertext: bytes, encrypt_key: str) -> dict:
    """飞书官方规范：SHA256(key) → AES-256-CBC，IV 取摘要前 16B，明文带 16B 盐。"""
    digest = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
    decrypted = AES.new(digest, AES.MODE_CBC, digest[:16]).decrypt(ciphertext)
    unpadded = unpad(decrypted, AES.block_size)
    # 硬切 16 字节盐 —— 不要搜 '{'（见模块 docstring）
    return json.loads(unpadded[_SALT_LEN:].decode("utf-8"))


def _legacy_candidates(encrypt_key: str) -> list[tuple[bytes, str]]:
    """路径 2 / 3 的候选密钥（``IV = 密文前 16B``）。"""
    candidates: list[tuple[bytes, str]] = []
    if len(encrypt_key) == 32:
        try:
            candidates.append((bytes.fromhex(encrypt_key), "AES-128 (hex 16B)"))
        except ValueError:
            pass  # 不是合法 hex → 跳过路径 2
    candidates.append((encrypt_key.encode("utf-8"), "AES-256 (utf-8)"))
    return candidates


def decrypt_payload(encrypt_b64: str, encrypt_key: str) -> dict:
    """解密飞书事件体，返回 JSON 对象。

    Args:
        encrypt_b64: 事件体里的 ``encrypt`` 字段（base64）。
        encrypt_key: 配置项 ``feishu_encrypt_key``（明文）。

    Raises:
        CryptoError: base64 非法 / 密钥为空 / 三条路径全部失败。
    """
    if not encrypt_key:
        raise CryptoError(
            "feishu_encrypt_key 为空：请在配置中心填写，"
            "或关闭飞书控制台的「事件加密」"
        )
    try:
        ciphertext = base64.b64decode(encrypt_b64)
    except Exception as exc:  # noqa: BLE001 —— base64 的异常类型很杂
        raise CryptoError(f"encrypt 字段不是合法 base64：{exc}") from exc

    errors: list[str] = []

    # --- 路径 1：飞书官方规范 ---
    try:
        return _path1(ciphertext, encrypt_key)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"SHA256-AES256: {type(exc).__name__}: {exc}")

    # --- 路径 2 / 3：密文必须够长且是 16 的整数倍 ---
    if len(ciphertext) < 2 * AES.block_size or len(ciphertext) % AES.block_size:
        raise CryptoError(
            f"密文长度 {len(ciphertext)} 非法；官方路径也失败（{errors[0]}）"
        )

    iv, body = ciphertext[:_SALT_LEN], ciphertext[_SALT_LEN:]
    for key_bytes, label in _legacy_candidates(encrypt_key):
        try:
            plain = AES.new(key_bytes, AES.MODE_CBC, iv).decrypt(body)
            return json.loads(unpad(plain, AES.block_size).decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{label}: {type(exc).__name__}: {exc}")

    raise CryptoError("三条解密路径全部失败 —— " + "；".join(errors))


# ---------------------------------------------------------------------------
# 签名校验
# ---------------------------------------------------------------------------
def do_sha256_verify(
    ts: str, nonce: str, body: bytes, sig: str, *, keys: Iterable[str]
) -> bool:
    """校验 ``X-Lark-Signature``。

    飞书规范是**普通 SHA256**（密钥拼进消息里）：:

        sig = SHA256(timestamp + nonce + key + raw_body)

    历史上也出现过 HMAC 变体，所以两种都试 —— 任一候选密钥命中即通过。
    ``keys`` 里的空值跳过（未配置的密钥不参与，避免空串碰撞）。
    """
    raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else (body or "")
    for key in keys:
        if not key:
            continue
        sha = hashlib.sha256((ts + nonce + key + raw).encode("utf-8")).hexdigest()
        if hmac.compare_digest(sha, sig):
            return True
        mac = hmac.new(
            key.encode("utf-8"), (ts + nonce + raw).encode("utf-8"), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(mac, sig):
            return True
    return False


def verify_lark_signature(
    headers: Mapping[str, str],
    body: bytes,
    *,
    encrypt_key: str = "",
    verification_token: str = "",
    is_encrypted_event: bool | None = None,
    allow_unsigned: bool = False,
) -> bool:
    """校验一次入站请求的签名（**fail-closed**）。

    Args:
        headers: 请求头。
        body: **原始**请求体字节（不要传重新序列化的 JSON —— 字节序会变）。
        encrypt_key / verification_token: 两个候选密钥。
        is_encrypted_event: 是否加密事件；``None`` = 从 body 推断。
        allow_unsigned: 显式放行「控制台未开签名」的场景（默认 ``False``）。

    Returns:
        ``True`` 才允许处理该事件。
    """
    sig = headers.get(SIGNATURE_HEADERS[0], "") or ""
    ts = headers.get(SIGNATURE_HEADERS[1], "") or ""
    nonce = headers.get(SIGNATURE_HEADERS[2], "") or ""

    if is_encrypted_event is None:
        try:
            parsed = json.loads(body.decode("utf-8")) if body else {}
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        is_encrypted_event = is_encrypted(parsed)

    if not (sig and ts and nonce):
        if allow_unsigned:
            logger.warning(
                "事件缺少签名头，但 allow_unsigned=True —— 放行（%s）",
                "加密事件" if is_encrypted_event else "明文事件",
            )
            return True
        # fail-closed：审计 B4 的 fail-open 漏洞在这里被堵上
        logger.warning("事件缺少签名头 —— 拒绝（fail-closed）")
        return False

    return do_sha256_verify(
        ts, nonce, body, sig, keys=(encrypt_key, verification_token)
    )


def verify_token(body: Any, *, verification_token: str = "") -> bool:
    """校验 v1 ``url_verification`` 握手里的 ``token`` 字段。

    * 不是握手事件 → ``False``（本函数只处理握手）
    * 未配置 ``feishu_verification_token`` → ``True``（用户没开校验）
    * 否则比较 ``body["token"]``
    """
    if not isinstance(body, dict) or body.get("type") != "url_verification":
        return False
    if not verification_token:
        return True
    return body.get("token") == verification_token
