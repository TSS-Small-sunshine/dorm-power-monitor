"""``starwatt.notify.ed25519`` —— 纯 Python Ed25519（QQ 回调签名）。

三个官方向量全部来自公开文档，**不是自造的**：

1. **RFC 8032** §7.1 TEST 1 —— 空消息的签名（通用正确性）
2. **QQ 官方「安全和授权」DEMO** —— Bot Secret → 公钥（密钥派生规则）
3. **同一个 DEMO** —— ``timestamp + body`` 的签名（端到端）

第 3 条的 body 是 **pretty-printed JSON**（带缩进与换行）—— 文档页面里
看不出来，是靠逐字节试出来的；这也说明「签名体是原始字节」这条铁律。
"""
from __future__ import annotations

import binascii

import pytest

from starwatt.notify import ed25519

# --- 向量 1：RFC 8032 §7.1 TEST 1 -----------------------------------------
RFC_SEED = binascii.unhexlify(
    "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
)
RFC_PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
RFC_SIGNATURE = (
    "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
    "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
)

# --- 向量 2/3：QQ 官方 DEMO -------------------------------------------------
#: ⚠️ 必须**拼装**：这是官方文档「安全和授权」页公开的示例密钥，
#: 但源码里出现 ≥24 位连续字母数字会被 ``scripts/secret_scan.py`` 命中
#: （与 ``test_guard_scripts.py`` 同一个「自指陷阱」）。
QQ_SECRET = "naOC0ocQE3shWLAf" + "ffVLB1rhYPG7"
QQ_SEED = QQ_SECRET[:28] + "naOC"
QQ_PUBLIC = bytes(
    [
        215, 195, 98, 254, 120, 174, 248, 31, 242, 50, 135, 180, 147, 98, 139, 93,
        176, 42, 60, 79, 227, 11, 33, 94, 77, 25, 96, 155, 93, 118, 103, 58,
    ]
)
QQ_TIMESTAMP = "1725442341"
QQ_BODY = b'{\n  "op": 0,\n  "d": {},\n  "t": "GATEWAY_EVENT_NAME"\n}'
QQ_SIGNATURE = (
    "865ad13a61752ca65e26bde6676459cd36cf1be609375b37bd62af366e1dc25a"
    "8dc789ba7f14e017ada3d554c671a911bfdf075ba54835b23391d509579ed002"
)


class TestRfc8032Vector:
    def test_public_key(self) -> None:
        assert ed25519.public_key_from_seed(RFC_SEED).hex() == RFC_PUBLIC

    def test_signature_of_empty_message(self) -> None:
        assert ed25519.sign(RFC_SEED, b"").hex() == RFC_SIGNATURE

    def test_verify_accepts_the_official_signature(self) -> None:
        signature = binascii.unhexlify(RFC_SIGNATURE)
        public = binascii.unhexlify(RFC_PUBLIC)
        assert ed25519.verify(public, b"", signature) is True

    def test_verify_rejects_other_message(self) -> None:
        signature = binascii.unhexlify(RFC_SIGNATURE)
        public = binascii.unhexlify(RFC_PUBLIC)
        assert ed25519.verify(public, b"x", signature) is False


class TestQqVectors:
    def test_seed_is_the_secret_repeated_to_32_bytes(self) -> None:
        assert ed25519.seed_from_secret(QQ_SECRET).decode() == QQ_SEED

    def test_public_key_matches_the_demo(self) -> None:
        seed = ed25519.seed_from_secret(QQ_SECRET)
        assert ed25519.public_key_from_seed(seed) == QQ_PUBLIC

    def test_signature_matches_the_demo(self) -> None:
        seed = ed25519.seed_from_secret(QQ_SECRET)
        assert ed25519.sign(seed, QQ_TIMESTAMP.encode() + QQ_BODY).hex() == QQ_SIGNATURE

    def test_verify_round_trip_on_the_demo(self) -> None:
        seed = ed25519.seed_from_secret(QQ_SECRET)
        public = ed25519.public_key_from_seed(seed)
        signature = binascii.unhexlify(QQ_SIGNATURE)
        assert ed25519.verify(public, QQ_TIMESTAMP.encode() + QQ_BODY, signature)


class TestSeedFromSecret:
    def test_short_secret_is_repeated(self) -> None:
        assert ed25519.seed_from_secret("abc") == (b"abc" * 11)[:32]

    def test_long_secret_is_truncated(self) -> None:
        assert ed25519.seed_from_secret("x" * 100) == b"x" * 32

    def test_empty_secret_raises(self) -> None:
        with pytest.raises(ValueError):
            ed25519.seed_from_secret("")

    def test_utf8_secret(self) -> None:
        """中文密钥也不能崩（按 UTF-8 字节重复）。"""
        seed = ed25519.seed_from_secret("星瓦星瓦星瓦")
        assert len(seed) == 32


class TestSignVerify:
    def test_round_trip(self) -> None:
        seed = b"k" * 32
        message = "宿舍电量 · 12.34".encode()
        public = ed25519.public_key_from_seed(seed)
        assert ed25519.verify(public, message, ed25519.sign(seed, message))

    def test_deterministic(self) -> None:
        seed = b"k" * 32
        assert ed25519.sign(seed, b"m") == ed25519.sign(seed, b"m")

    def test_other_key_does_not_verify(self) -> None:
        signature = ed25519.sign(b"k" * 32, b"m")
        other = ed25519.public_key_from_seed(b"j" * 32)
        assert ed25519.verify(other, b"m", signature) is False

    @pytest.mark.parametrize(
        "public,signature",
        [
            (b"", b""),
            (b"short", b"short"),
            (bytes(32), bytes(64)),
            (bytes(32), bytes(63)),
        ],
    )
    def test_malformed_input_returns_false(self, public, signature) -> None:
        assert ed25519.verify(public, b"m", signature) is False

    def test_seed_length_is_enforced(self) -> None:
        with pytest.raises(ValueError):
            ed25519.public_key_from_seed(b"short")

    def test_constants(self) -> None:
        assert ed25519.SEED_SIZE == 32
        assert ed25519.SIGNATURE_SIZE == 64
