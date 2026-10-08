"""``starwatt.notify.crypto`` —— 飞书事件解密与签名校验。

安全回归网：签名校验一旦被削弱（fail-open 复活），必须立刻在 CI 红掉。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os

import pytest
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad

from starwatt.notify import crypto

KEY = "a" * 32
SALT = bytes(range(16))  # 固定盐 → 固定密文，向量可复现
PAYLOAD = {"type": "event_callback", "token": "t-1", "schema": "2.0"}


# ---------------------------------------------------------------------------
# 构造三种密文（同时是「三种路径」的活文档）
# ---------------------------------------------------------------------------
def _encrypt_path1(payload: dict, key: str = KEY, salt: bytes = SALT) -> str:
    """飞书官方规范：SHA256(key) 当密钥与 IV，明文前缀 16B 盐。"""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    plain = salt + json.dumps(payload).encode("utf-8")
    cipher = AES.new(digest, AES.MODE_CBC, digest[:16])
    return base64.b64encode(cipher.encrypt(pad(plain, 16))).decode("utf-8")


def _encrypt_legacy(payload: dict, key_bytes: bytes) -> str:
    """旧版 SDK：IV = 密文前 16B。"""
    iv = os.urandom(16)
    plain = json.dumps(payload).encode("utf-8")
    cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
    return base64.b64encode(iv + cipher.encrypt(pad(plain, 16))).decode("utf-8")


class TestIsEncrypted:
    def test_detects_encrypt_field(self) -> None:
        assert crypto.is_encrypted({"encrypt": "abc"}) is True

    @pytest.mark.parametrize("body", [None, {}, {"encrypt": ""}, {"encrypt": 1}, "x", []])
    def test_rejects_others(self, body) -> None:
        assert crypto.is_encrypted(body) is False


class TestDecryptPath1:
    def test_official_spec(self) -> None:
        assert crypto.decrypt_payload(_encrypt_path1(PAYLOAD), KEY) == PAYLOAD

    def test_salt_may_contain_brace(self) -> None:
        """🔑 盐里出现 ``{``（0x7B）时不能把 JSON 切坏 —— 必须硬切 16 字节。"""
        salt = b"\x7b" * 16
        assert crypto.decrypt_payload(_encrypt_path1(PAYLOAD, salt=salt), KEY) == PAYLOAD

    def test_non_ascii_payload(self) -> None:
        payload = {"msg": "宿舍电量 · 剩余 12.34 kW·h"}
        assert crypto.decrypt_payload(_encrypt_path1(payload), KEY) == payload


class TestDecryptLegacyPaths:
    def test_hex_key_is_aes128(self) -> None:
        """32 位 hex 密钥 → 16 原始字节 → AES-128-CBC。"""
        key = "0123456789abcdef0123456789abcdef"
        blob = _encrypt_legacy(PAYLOAD, bytes.fromhex(key))
        assert crypto.decrypt_payload(blob, key) == PAYLOAD

    def test_utf8_key_is_aes256(self) -> None:
        """32 位非 hex 密钥 → 32 字节 → AES-256-CBC。"""
        key = "z" * 32
        blob = _encrypt_legacy(PAYLOAD, key.encode("utf-8"))
        assert crypto.decrypt_payload(blob, key) == PAYLOAD


class TestDecryptErrors:
    def test_empty_key_raises(self) -> None:
        with pytest.raises(crypto.CryptoError, match="encrypt_key"):
            crypto.decrypt_payload(_encrypt_path1(PAYLOAD), "")

    def test_bad_base64_raises(self) -> None:
        with pytest.raises(crypto.CryptoError, match="base64"):
            crypto.decrypt_payload("not base64!!", KEY)

    def test_wrong_key_raises(self) -> None:
        with pytest.raises(crypto.CryptoError, match="失败"):
            crypto.decrypt_payload(_encrypt_path1(PAYLOAD), "b" * 32)

    def test_short_ciphertext_raises(self) -> None:
        with pytest.raises(crypto.CryptoError):
            crypto.decrypt_payload(base64.b64encode(b"short").decode(), KEY)


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------
def _sha_sig(ts: str, nonce: str, key: str, body: bytes) -> str:
    return hashlib.sha256((ts + nonce + key + body.decode("utf-8")).encode()).hexdigest()


def _hmac_sig(ts: str, nonce: str, key: str, body: bytes) -> str:
    return hmac.new(
        key.encode(), (ts + nonce + body.decode("utf-8")).encode(), hashlib.sha256
    ).hexdigest()


class TestDoSha256Verify:
    def test_plain_sha256_matches(self) -> None:
        body = b'{"a":1}'
        sig = _sha_sig("1", "n", KEY, body)
        assert crypto.do_sha256_verify("1", "n", body, sig, keys=(KEY,)) is True

    def test_hmac_variant_matches(self) -> None:
        body = b'{"a":1}'
        sig = _hmac_sig("1", "n", KEY, body)
        assert crypto.do_sha256_verify("1", "n", body, sig, keys=(KEY,)) is True

    def test_tampered_body_fails(self) -> None:
        sig = _sha_sig("1", "n", KEY, b'{"a":1}')
        assert crypto.do_sha256_verify("1", "n", b'{"a":2}', sig, keys=(KEY,)) is False

    def test_empty_keys_never_match(self) -> None:
        """未配置密钥时不能「空串也算命中」。"""
        assert crypto.do_sha256_verify("", "", b"{}", "", keys=("", None)) is False

    def test_wrong_signature_fails(self) -> None:
        assert crypto.do_sha256_verify("1", "n", b"{}", "deadbeef", keys=(KEY,)) is False


class TestVerifyLarkSignature:
    @staticmethod
    def _headers(sig: str, ts: str = "1", nonce: str = "n") -> dict:
        return {
            "X-Lark-Signature": sig,
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
        }

    def test_valid_signature_passes(self) -> None:
        body = json.dumps(PAYLOAD).encode("utf-8")
        assert crypto.verify_lark_signature(
            self._headers(_sha_sig("1", "n", KEY, body)), body, encrypt_key=KEY
        )

    def test_missing_headers_is_rejected(self) -> None:
        """🔑 fail-closed：审计 B4 的 fail-open 漏洞不得复活。"""
        body = b'{"type":"event_callback"}'
        assert crypto.verify_lark_signature({}, body, encrypt_key=KEY) is False

    def test_partial_headers_are_rejected(self) -> None:
        headers = self._headers("x")
        headers["X-Lark-Request-Nonce"] = ""
        assert crypto.verify_lark_signature(headers, b"{}", encrypt_key=KEY) is False

    def test_allow_unsigned_is_explicit_opt_in(self) -> None:
        assert (
            crypto.verify_lark_signature(
                {}, b"{}", encrypt_key=KEY, allow_unsigned=True
            )
            is True
        )

    def test_infers_encrypted_event(self) -> None:
        body = json.dumps({"encrypt": "xx"}).encode("utf-8")
        assert crypto.verify_lark_signature({}, body, encrypt_key=KEY) is False

    def test_bad_signature_is_rejected(self) -> None:
        assert (
            crypto.verify_lark_signature(self._headers("nope"), b"{}", encrypt_key=KEY)
            is False
        )

    def test_verification_token_also_works_as_key(self) -> None:
        body = b"{}"
        sig = _sha_sig("1", "n", "tok", body)
        assert crypto.verify_lark_signature(
            self._headers(sig), body, verification_token="tok"
        )


class TestVerifyToken:
    def test_handshake_with_matching_token(self) -> None:
        body = {"type": "url_verification", "token": "t-1", "challenge": "c"}
        assert crypto.verify_token(body, verification_token="t-1") is True

    def test_handshake_with_wrong_token(self) -> None:
        assert (
            crypto.verify_token({"type": "url_verification", "token": "x"},
                                verification_token="t-1")
            is False
        )

    def test_handshake_without_configured_token(self) -> None:
        assert crypto.verify_token({"type": "url_verification"}) is True

    def test_non_handshake_returns_false(self) -> None:
        assert crypto.verify_token({"type": "event_callback"}) is False
        assert crypto.verify_token(None) is False


def test_signature_headers_are_the_documented_ones() -> None:
    assert crypto.SIGNATURE_HEADERS == (
        "X-Lark-Signature",
        "X-Lark-Request-Timestamp",
        "X-Lark-Request-Nonce",
    )
