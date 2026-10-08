"""配置读写 —— 类型转换 + 加密 + 内存缓存。

数据流
======

::

    registry.Setting（声明：类型 / 默认 / 校验 / kind）
              ↓
        store.get(key)  ← 内存缓存
              ↓
        MetaRepo.get(key)  ← meta 表（value 恒为 TEXT）
              ↓
        类型还原（bool / int / float / json / 解密 secret）

关键设计
========

**1. 未写入的键返回声明里的默认值**
``meta`` 表不需要预先塞满默认值 —— :func:`get` 读不到就回落到
``Setting.default``。这样升级新增配置项时**零迁移**（RK15）。

**2. 缓存 + 写时失效**
配置读极频繁（每次抓取、每次请求），写极少。
写入时按 key 失效，读时命中缓存。

**3. 只有 ``CONFIG`` 能被用户写**
:func:`set` 默认拒绝写 ``STATE`` / ``CACHE``（权限边界，见 types.Kind）。
业务代码写状态用 ``set_state()`` 显式声明意图。

**4. secret 自动加解密**
写入时加密、读取时解密，调用方拿到的一律是明文。
:func:`all_values` 默认脱敏 —— 避免某个 API 忘了脱敏就泄露。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from starwatt.config_registry import registry
from starwatt.config_registry.secrets import decrypt, encrypt, is_encrypted, mask
from starwatt.config_registry.types import Kind, Setting, T, ValidationError
from starwatt.config_registry.types import validate_value as _validate
from starwatt.db.repositories import MetaRepo
from starwatt.logging_setup import forget_secret, register_secret

logger = logging.getLogger("starwatt.config")

__all__ = [
    "all_values",
    "ensure_defaults",
    "get",
    "get_bool",
    "get_float",
    "get_int",
    "get_json",
    "get_str",
    "invalidate",
    "purge_removed",
    "raw",
    "register_all_secrets",
    "set",
    "set_many",
    "set_state",
]

_MISSING = object()

#: ``key -> 已还原的 Python 值``（只缓存成功解析的项）
_cache: dict[str, Any] = {}
_cache_lock = threading.RLock()


# ---------------------------------------------------------------------------
# 序列化（Python 值 ←→ meta 的 TEXT）
# ---------------------------------------------------------------------------
def _to_text(setting: Setting, value: Any) -> str:
    """把 Python 值编码成 ``meta.value`` 的 TEXT。"""
    if setting.secret:
        return encrypt(str(value))
    if setting.type is T.BOOL:
        return "1" if value else "0"
    if setting.type in (T.INT, T.DURATION):
        return str(int(value))
    if setting.type is T.FLOAT:
        return repr(float(value))
    if setting.type is T.JSON:
        return value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False
        )
    return str(value)


def _from_text(setting: Setting, text: str | None) -> Any:
    """把 ``meta.value`` 还原成 Python 值（``None`` = 未设置）。"""
    if text is None:
        return _MISSING
    if setting.secret:
        return decrypt(text)
    if setting.type is T.BOOL:
        return text.strip().lower() in ("1", "true", "yes", "on")
    if setting.type in (T.INT, T.DURATION):
        try:
            return int(float(text.strip()))
        except (ValueError, TypeError):
            return _MISSING
    if setting.type is T.FLOAT:
        try:
            return float(text.strip())
        except (ValueError, TypeError):
            return _MISSING
    if setting.type is T.JSON:
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            return _MISSING
    return text


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------
def raw(key: str) -> str | None:
    """``meta`` 里的**原始** TEXT（secret 仍为密文）。"""
    return MetaRepo.get(key)


def get(key: str, default: Any = _MISSING) -> Any:
    """读一个配置项（已还原类型 / 已解密）。

    优先级：``meta`` 里的值 → 注册表默认值 → ``default`` 参数。

    Raises:
        KeyError: 键不在注册表且未提供 ``default``。
        SecretDecryptError: secret 解密失败（**故意不吞** —— 见 secrets.py）。
    """
    setting = registry.get(key)
    if setting is None:
        if default is _MISSING:
            raise KeyError(f"未注册的配置项：{key!r}")
        return default

    with _cache_lock:
        if key in _cache:
            return _cache[key]

    value = _from_text(setting, MetaRepo.get(key))
    if value is _MISSING:
        value = setting.default

    with _cache_lock:
        _cache[key] = value
    return value


def get_str(key: str, default: str = "") -> str:
    value = get(key, default)
    return value if isinstance(value, str) else str(value)


def get_int(key: str, default: int = 0) -> int:
    value = get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    value = get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_bool(key: str, default: bool = False) -> bool:
    value = get(key, default)
    return bool(value)


def get_json(key: str, default: Any = None) -> Any:
    """读 JSON 项；解析失败回落 ``default``（不抛异常）。"""
    value = get(key, default if default is not None else {})
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return default if default is not None else {}
    return value


# ---------------------------------------------------------------------------
# 写
# ---------------------------------------------------------------------------
def invalidate(key: str | None = None) -> None:
    """失效缓存（``None`` = 全清）。"""
    with _cache_lock:
        if key is None:
            _cache.clear()
        else:
            _cache.pop(key, None)


def _remember_secret(setting: Setting, value: Any) -> None:
    """secret 写入时把明文登记到日志脱敏表；换值时注销旧值。

    这是 Q20 铁律（secret 不得出现在日志里）的**第一道防线** ——
    即使某个程序员忘了脱敏、直接把配置值打进日志，输出里也只会是 ``***``。
    """
    if not setting.secret:
        return
    previous = MetaRepo.get(setting.key)
    if previous:
        forget_secret(decrypt(previous) if is_encrypted(previous) else previous)
    register_secret(str(value))


def register_all_secrets() -> int:
    """把库里所有 secret 项的明文登记进脱敏表，返回登记条数。

    应在**启动时**调用一次：否则重启后、用户尚未重新保存配置之前，
    旧 secret 在日志里是不受保护的。
    """
    count = 0
    for key, setting in registry.REGISTRY.items():
        if not setting.secret:
            continue
        text = MetaRepo.get(key)
        if not text:
            continue
        plain = decrypt(text) if is_encrypted(text) else text
        if plain:
            register_secret(plain)
            count += 1
    return count


def set(
    key: str,
    value: Any,
    *,
    allow_state: bool = False,
) -> None:
    """写入一个配置项（校验 → 编码 → 落库 → 失效缓存）。

    Args:
        key: 注册表里的键名。
        value: Python 值（``bool`` / ``int`` / ``str`` / ``dict`` …）。
        allow_state: 允许写 ``STATE`` / ``CACHE``。业务代码写运行时状态时
            必须显式传 ``True`` —— 这个「摩擦」是故意的：防止误把用户
            可改配置写成代码私有状态。

    Raises:
        KeyError: 键未注册。
        ValidationError: 值不合法（消息可直接显示给用户）。
        PermissionError: 试图通过配置 API 写 ``STATE`` / ``CACHE``。
    """
    setting = registry.get(key)
    if setting is None:
        raise KeyError(f"未注册的配置项：{key!r}")

    if not setting.user_editable and not allow_state:
        raise PermissionError(
            f"{key!r} 是 {setting.kind.value} 类，不能被配置 API 写入"
            "（业务代码请用 set_state）"
        )

    _validate(setting, value)
    _remember_secret(setting, value)
    MetaRepo.set(key, _to_text(setting, value))
    invalidate(key)


def set_state(key: str, value: Any) -> None:
    """业务代码写 ``STATE`` / ``CACHE``（``CONFIG`` 也允许，便于回填）。"""
    set(key, value, allow_state=True)


def set_many(items: dict[str, Any]) -> dict[str, str]:
    """批量更新，**逐键校验**。

    与「全成功或全失败」不同：这里**逐键独立**提交，返回
    ``{key: 错误消息}``。理由 —— 用户在配置页一次改 5 项，其中 1 项
    填错时，不该把另外 4 项正确的改动一起丢掉。

    Returns:
        ``{失败的 key: 中文错误}``；全部成功则为空字典。
    """
    errors: dict[str, str] = {}
    for key, value in items.items():
        setting = registry.get(key)
        if setting is None:
            errors[key] = "未注册的配置项"
            continue
        if not setting.user_editable:
            errors[key] = f"{setting.kind.value} 类配置不可修改"
            continue
        try:
            _validate(setting, value)
        except ValidationError as exc:
            errors[key] = exc.message
            continue
        _remember_secret(setting, value)
        MetaRepo.set(key, _to_text(setting, value))
        invalidate(key)
    return errors


# ---------------------------------------------------------------------------
# 批量视图
# ---------------------------------------------------------------------------
def all_values(
    *,
    kind: Kind | None = Kind.CONFIG,
    include_secrets: bool = False,
    include_defaults: bool = True,
) -> dict[str, Any]:
    """``key -> 值``，供 ``GET /api/admin/config``。

    Args:
        kind: 只取该类别（``None`` = 全部）。
        include_secrets: ``True`` 才返回 secret 明文；默认**脱敏**
            （``••••1234``）。默认值就是安全值 —— 避免调用方忘了脱敏。
        include_defaults: 未写入的项是否用注册表默认值补上。
    """
    out: dict[str, Any] = {}
    for key, setting in registry.REGISTRY.items():
        if kind is not None and setting.kind is not kind:
            continue
        text = MetaRepo.get(key)
        if text is None and not include_defaults:
            continue

        if setting.secret:
            if include_secrets:
                out[key] = decrypt(text) if text else ""
            else:
                # 脱敏：需要明文末 4 位，所以必须先解出来再打码
                out[key] = mask(decrypt(text)) if text else ""
            continue

        value = _from_text(setting, text)
        out[key] = setting.default if value is _MISSING else value
    return out


def ensure_defaults() -> int:
    """把**未写入**的 ``CONFIG`` 项按默认值落库，返回写入条数。

    为什么需要：前端渲染表单时需要拿到「当前值」，而 ``meta`` 里可能
    一条都没有。虽然 :func:`get` 会回落到默认值，但显式落库能让
    「导出配置」与「重置为默认」的行为更直观。

    幂等 —— 已存在的键不动（**不覆盖用户改过的值**）。
    """
    existing = MetaRepo.all()
    pairs: dict[str, str] = {}
    for key, setting in registry.REGISTRY.items():
        if setting.kind is not Kind.CONFIG or key in existing:
            continue
        pairs[key] = _to_text(setting, setting.default)
    if pairs:
        MetaRepo.set_many(pairs)
        invalidate()
        logger.info("config: 写入 %d 项默认配置", len(pairs))
    return len(pairs)


def purge_removed() -> int:
    """删除已下线的旧键（当前只有 ``admin_password``，L20）。

    旧库里这个明文密码还在 —— 留着就是留一个后门（Q21 要求彻底移除）。
    """
    removed = 0
    for key in registry.REMOVED_KEYS:
        if MetaRepo.get(key) is not None:
            MetaRepo.delete(key)
            removed += 1
            logger.warning("config: 已删除废弃的旧配置项 %r（L20）", key)
    if removed:
        invalidate()
    return removed


def reset_to_defaults() -> int:
    """把全部 ``CONFIG`` 项恢复成注册表默认值（**删除** meta 里的覆盖值）。

    为什么是「删除」而不是「写回默认值」：未写入的键本来就会回落到
    ``Setting.default``（见模块 docstring 第 1 条），删掉才是真正干净的
    「恢复出厂」。``STATE`` / ``CACHE``（游标、缓存）**不受影响** ——
    重置配置不该让抓取历史「回到过去」。

    Returns:
        被重置的键数量。
    """
    existing = MetaRepo.all()
    keys = [
        key
        for key, setting in registry.REGISTRY.items()
        if setting.kind is Kind.CONFIG and key in existing
    ]
    for key in keys:
        MetaRepo.delete(key)
    if keys:
        invalidate()
        logger.warning("config: 已重置 %d 项配置为默认值", len(keys))
    return len(keys)
