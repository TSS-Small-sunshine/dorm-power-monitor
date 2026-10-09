"""配置导出 / 导入（N16）—— **可移植的 JSON**。

为什么需要
==========

换机器、备份、把配置分享给同学时，用户不该手抄 40 个配置项。导出成一个
JSON、导入回去即可。

四条硬规则
==========

**1. 只导出 ``CONFIG``**
``STATE``（游标 / 时间戳）与 ``CACHE``（派生缓存）是**运行时数据**，
导出它们会在新机器上造成「上次抓取时间在未来」这类诡异状态。
（见 ``types.Kind`` 的权限边界。）

**2. secret 默认脱敏**
``all_values(include_secrets=False)`` 返回 ``••••1234``。导出脱敏值时导入
会跳过它们（**不会**把掩码当成真凭据写回去 —— 这是最容易犯的错）。
需要真正迁移凭据时显式 ``include_secrets=True``。

**3. 未知键只跳过、不报错**
旧版本的导出文件可能含已下线的键（例如 ``admin_password``，L20）。
导入时**逐键校验**：未知键进 ``skipped``，非法值进 ``errors``，
**其余照常导入** —— 用户不该因为一个过期字段丢掉整份配置。

**4. 导入是「合并」不是「替换」**
只写文件里出现的键；没出现的保持原样。想彻底重置用
:func:`starwatt.config_registry.store` 的 ``reset_to_defaults``。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from starwatt.config_registry import registry
from starwatt.config_registry.store import all_values, invalidate, set_many
from starwatt.config_registry.types import Kind
from starwatt.timeutil import stamp

logger = logging.getLogger("starwatt.config")

__all__ = [
    "EXPORT_FORMAT",
    "MASKED_MARKER",
    "export_config",
    "import_config",
    "is_masked",
]

#: 导出文件的格式标识（导入时校验，避免把别的 JSON 喂进来）
EXPORT_FORMAT = "starwatt-config"

#: 脱敏值的形态（``secrets.mask`` 的产物）；导入时**跳过**
MASKED_MARKER = "••••"


def is_masked(value: Any) -> bool:
    """是否是脱敏占位值（``••••1234``）。"""
    return isinstance(value, str) and value.startswith(MASKED_MARKER)


def export_config(*, include_secrets: bool = False) -> dict[str, Any]:
    """导出全部 ``CONFIG`` 项。

    Args:
        include_secrets: ``True`` 时导出 secret **明文**（用于整机迁移）；
            默认 ``False``，secret 导出为 ``••••1234``。

    Returns:
        ``{"format", "exported_at", "include_secrets", "config": {...}}``
    """
    values = all_values(
        kind=Kind.CONFIG, include_secrets=include_secrets, include_defaults=True
    )
    return {
        "format": EXPORT_FORMAT,
        "exported_at": stamp(),
        "include_secrets": include_secrets,
        "config": values,
    }


def _acceptable(key: str, value: Any, allow_secrets: bool) -> bool:
    """该键值是否应该被导入（见模块 docstring 的四条硬规则）。"""
    setting = registry.get(key)
    if setting is None:
        return False  # 旧版本字段 / 手写垃圾
    if setting.kind is not Kind.CONFIG:
        return False  # STATE / CACHE 不可导入
    if not setting.secret:
        return True
    if is_masked(value):
        return False  # 脱敏占位值：绝不写回掩码
    return allow_secrets


def import_config(
    payload: Any, *, allow_secrets: bool = False
) -> dict[str, Any]:
    """导入一份导出文件（**合并**语义）。

    Args:
        payload: :func:`export_config` 的产物（或直接是 ``{key: value}``）。
        allow_secrets: 允许写入 secret 明文。默认 ``False`` —— 脱敏值一律跳过，
            且**拒绝**非脱敏的 secret（防止有人把别人的明文凭据灌进来）。

    Returns:
        ``{"applied": [...], "skipped": [...], "errors": {key: msg}}``
    """
    if not isinstance(payload, dict):
        return {"applied": [], "skipped": [], "errors": {"": "payload 不是对象"}}

    data = payload.get("config") if "config" in payload else payload
    if not isinstance(data, dict):
        return {"applied": [], "skipped": [], "errors": {"": "config 字段不是对象"}}

    fmt = payload.get("format")
    if fmt is not None and fmt != EXPORT_FORMAT:
        return {
            "applied": [],
            "skipped": [],
            "errors": {"": f"不是 StarWatt 配置导出文件（format={fmt!r}）"},
        }

    skipped: list[str] = [
        key for key in data if not _acceptable(key, data[key], allow_secrets)
    ]
    candidates = {
        key: value
        for key, value in data.items()
        if _acceptable(key, value, allow_secrets)
    }

    errors = set_many(candidates)
    applied = [key for key in candidates if key not in errors]
    skipped.extend(key for key in errors)

    if applied:
        invalidate()
    logger.info(
        "配置导入：应用 %d 项，跳过 %d 项，失败 %d 项",
        len(applied),
        len(skipped),
        len(errors),
    )
    return {"applied": applied, "skipped": skipped, "errors": errors}


def to_json(payload: dict[str, Any]) -> str:
    """导出为 JSON 文本（``ensure_ascii=False`` —— 中文可读）。"""
    return json.dumps(payload, ensure_ascii=False, indent=2)
