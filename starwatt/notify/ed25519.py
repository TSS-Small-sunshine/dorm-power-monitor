"""Ed25519 —— **纯 Python** 实现（只为 QQ 官方机器人的签名）。

为什么不装库
============

QQ 开放平台的回调签名用 Ed25519。Python 标准库没有它，而现成的库都要
引 Rust / C 依赖：

* ``cryptography`` —— Rust（PyO3），**没有 armv7 wheel**
* ``PyNaCl`` —— 需要 libsodium

而 B1/Q4 的硬约束是「剔除全部 Rust 依赖，保证 armv7 可构建」。为了一个
回调签名把整条架构约束打破，不值得 —— 所以这里用 Ed25519 的经典参考实现
（RFC 8032 附录，公开领域，纯整数运算，无依赖）。

用途与边界
==========

============  ==========================================================
用在哪        说明
============  ==========================================================
回调 URL 验证  QQ 发 ``op=13`` + ``plain_token`` → 我们用 Bot Secret 派生
              私钥，对 ``event_ts + plain_token`` 签名后回给 QQ
入站签名校验   对 ``X-Signature-Timestamp + body`` 用派生公钥验签
============  ==========================================================

⚠️ 这是**签名/验签**用途，不是加密。性能上：一次签名约 10 ms（纯 Python），
回调频率极低（每次消息事件一次验签），完全够用；但**不要**拿它做高频场景。

密钥派生规则（QQ 特有）
======================

QQ 的「私钥种子」不是 hex 解码，而是把 **Bot Secret 字符串本身重复**到
至少 32 字节、取前 32 字节：

::

    secret = "naOC0ocQE3shWLAfffVLB1rhYPG7"        # 30 字符
    重复后  = "naOC0ocQE3shWLAfffVLB1rhYPG7naOC0oc..."
    seed    = 前 32 字节 = "naOC0ocQE3shWLAfffVLB1rhYPG7naOC"

见 :func:`seed_from_secret` 与官方文档「安全和授权」的 DEMO 输入输出。
"""
from __future__ import annotations

import hashlib

__all__ = [
    "SEED_SIZE",
    "SIGNATURE_SIZE",
    "public_key_from_seed",
    "seed_from_secret",
    "sign",
    "verify",
]

#: 种子长度（字节）
SEED_SIZE = 32

#: 签名长度（字节）
SIGNATURE_SIZE = 64

#: 曲线参数（RFC 8032）
_Q = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_B = 256


def _h(data: bytes) -> bytes:
    return hashlib.sha512(data).digest()


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = -121665 * _inv(121666) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = 4 * _inv(5) % _Q
_BX = _xrecover(_BY)
_BASE = (_BX % _Q, _BY % _Q)


def _edwards(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return (x3 % _Q, y3 % _Q)


def _scalarmult(point: tuple[int, int], exponent: int) -> tuple[int, int]:
    """点乘（双倍-相加，迭代实现 —— 递归版在深链上有栈风险）。"""
    result = (0, 1)
    addend = point
    while exponent > 0:
        if exponent & 1:
            result = _edwards(result, addend)
        addend = _edwards(addend, addend)
        exponent >>= 1
    return result


def _encode_int(value: int) -> bytes:
    return value.to_bytes(32, "little")


def _encode_point(point: tuple[int, int]) -> bytes:
    x, y = point
    bits = [(y >> i) & 1 for i in range(_B - 1)] + [x & 1]
    return bytes(
        sum(bits[i * 8 + j] << j for j in range(8)) for i in range(32)
    )


def _decode_point(data: bytes) -> tuple[int, int]:
    if len(data) != 32:
        raise ValueError("点必须是 32 字节")
    y = int.from_bytes(data, "little") & ((1 << 255) - 1)
    x = _xrecover(y)
    if (x & 1) != (data[31] >> 7):
        x = _Q - x
    point = (x, y)
    if not _is_on_curve(point):
        raise ValueError("点不在曲线上")
    return point


def _is_on_curve(point: tuple[int, int]) -> bool:
    x, y = point
    return (-x * x + y * y - 1 - _D * x * x * y * y) % _Q == 0


def _bit(data: bytes, index: int) -> int:
    return (data[index // 8] >> (index % 8)) & 1


def _secret_scalar(seed: bytes) -> int:
    """``seed`` → 私钥标量（RFC 8032 的 clamp）。"""
    digest = _h(seed)
    return 2 ** (_B - 2) + sum(
        2**i * _bit(digest, i) for i in range(3, _B - 2)
    )


def _prefix(seed: bytes) -> bytes:
    """``seed`` → 确定性 nonce 前缀（``SHA512(seed)`` 的**后半 32 字节**）。

    ⚠️ 注意不是 ``h[4:8]`` —— 广为流传的某版参考实现把 ``range(b//8, b//4)``
    误当成「前缀的字节区间」，实际取的是 ``h[4:8]``（只有 4 个字节）。
    那样签出来的签名虽然自洽可验，但**与 RFC 8032 测试向量不符**。
    这里按标准取 ``h[32:64]``。
    """
    return _h(seed)[SEED_SIZE:]


def _hint(message: bytes) -> int:
    digest = _h(message)
    return sum(2**i * _bit(digest, i) for i in range(2 * _B))


def seed_from_secret(secret: str) -> bytes:
    """QQ 的密钥派生：把 Bot Secret **重复**到 32 字节（不是 hex 解码）。

    Raises:
        ValueError: ``secret`` 为空。
    """
    if not secret:
        raise ValueError("Bot Secret 不能为空")
    raw = secret.encode("utf-8")
    repeats = (SEED_SIZE // len(raw)) + 1
    return (raw * repeats)[:SEED_SIZE]


def public_key_from_seed(seed: bytes) -> bytes:
    """``seed``（32 字节）→ 32 字节公钥。"""
    if len(seed) != SEED_SIZE:
        raise ValueError(f"seed 必须是 {SEED_SIZE} 字节，收到 {len(seed)}")
    return _encode_point(_scalarmult(_BASE, _secret_scalar(seed)))


def sign(seed: bytes, message: bytes) -> bytes:
    """用 ``seed`` 对 ``message`` 签名，返回 64 字节签名。"""
    public = public_key_from_seed(seed)
    r = _hint(_prefix(seed) + message)
    big_r = _scalarmult(_BASE, r)
    s = (r + _hint(_encode_point(big_r) + public + message) * _secret_scalar(seed)) % _L
    return _encode_point(big_r) + _encode_int(s)


def verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """验签。任何格式问题一律返回 ``False``（**不抛异常**）。"""
    try:
        if len(signature) != SIGNATURE_SIZE or len(public_key) != 32:
            return False
        big_r = _decode_point(signature[:32])
        a = _decode_point(public_key)
        s = int.from_bytes(signature[32:], "little")
        if s >= _L:
            return False
        expected = _scalarmult(_BASE, s)
        actual = _edwards(big_r, _scalarmult(a, _hint(signature[:32] + public_key + message)))
        return expected == actual
    except Exception:  # noqa: BLE001 —— 验签失败就是 False，不区分原因
        return False
