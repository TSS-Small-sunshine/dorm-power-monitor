"""策略参数读取 + Flask 密钥自举。

两层职责
========

1. **可调策略**（session 时长 / 锁定阈值 / 锁定窗口）—— 读 env，
   非法值一律回落默认，**绝不抛异常**（认证路径不能因为配置写错就崩）
2. **``FLASK_SECRET_KEY`` 自举** —— 缺失时自动生成并**写回 ``.env``**，
   这样重启后已签发的 cookie 仍然有效；同时它也是
   「配置中心 secrets 的 AES-GCM 加密密钥」的派生源（Q15/Q17）
"""
from __future__ import annotations

import logging
import os
import secrets

from starwatt.auth.constants import (
    DEFAULT_LOCKOUT_MINUTES,
    DEFAULT_LOCKOUT_THRESHOLD,
    DEFAULT_SESSION_HOURS,
    ENV_LOCKOUT_MINUTES,
    ENV_LOCKOUT_THRESHOLD,
    ENV_SECRET_KEY,
    ENV_SESSION_HOURS,
)

logger = logging.getLogger("starwatt.auth")

__all__ = [
    "flask_secret_key",
    "lockout_minutes",
    "lockout_threshold",
    "session_hours",
]


def _positive_int(env_key: str, default: int) -> int:
    """读一个正整数 env；空 / 非数字 / ≤0 一律回落 ``default``。"""
    raw = (os.environ.get(env_key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def session_hours() -> int:
    """会话有效期（小时）。"""
    return _positive_int(ENV_SESSION_HOURS, DEFAULT_SESSION_HOURS)


def lockout_minutes() -> int:
    """触发锁定后的锁定时长（分钟）。"""
    return _positive_int(ENV_LOCKOUT_MINUTES, DEFAULT_LOCKOUT_MINUTES)


def lockout_threshold() -> int:
    """在锁定窗口内允许的失败次数上限。"""
    return _positive_int(ENV_LOCKOUT_THRESHOLD, DEFAULT_LOCKOUT_THRESHOLD)


def _persist_to_env(key: str, value: str) -> bool:
    """把 ``KEY=value`` 追加到项目根的 ``.env``。

    返回是否写入成功。**失败只记 warning，不抛异常** ——
    只读文件系统（Docker 只读根）下也应能继续运行。
    """
    from starwatt.config import ENV_FILE

    try:
        existing = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
        if f"{key}=" in existing:
            # 已有一行（可能为空值）→ 就地替换，避免重复键
            lines = []
            for line in existing.splitlines():
                if line.startswith(f"{key}="):
                    lines.append(f"{key}={value}")
                else:
                    lines.append(line)
            ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        else:
            prefix = "" if not existing or existing.endswith("\n") else "\n"
            with ENV_FILE.open("a", encoding="utf-8") as handle:
                handle.write(f"{prefix}{key}={value}\n")
        return True
    except OSError as exc:
        logger.warning(
            "无法把 %s 写回 %s（只读文件系统？）：%s", key, ENV_FILE, exc
        )
        return False


def flask_secret_key() -> str:
    """返回 Flask 签名密钥；缺失时生成并写回 ``.env``。

    生成后**同时**写进 ``os.environ``，保证当前进程立即生效。
    """
    current = (os.environ.get(ENV_SECRET_KEY) or "").strip()
    if current:
        return current

    generated = secrets.token_hex(32)
    os.environ[ENV_SECRET_KEY] = generated
    if _persist_to_env(ENV_SECRET_KEY, generated):
        logger.warning(
            "%s 缺失，已自动生成并写入 .env（请备份 —— 丢失会导致"
            "配置中心的加密 secrets 无法解密）",
            ENV_SECRET_KEY,
        )
    else:
        logger.warning(
            "%s 缺失且无法写回 .env，本次运行使用临时密钥 —— "
            "重启后所有已签发的会话将失效",
            ENV_SECRET_KEY,
        )
    return generated
