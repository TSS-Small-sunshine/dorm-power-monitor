"""``starwatt.config_registry`` —— 配置中心（Q15）。

5 个文件
========

=================  ==========================================================
``types.py``       ``T`` 类型 / ``Kind`` 三类 / ``Setting`` / 校验引擎
``registry.py``    **声明式定义每一个配置项**（唯一真相）
``secrets.py``     AES-256-GCM 加解密 + 脱敏
``store.py``       读写 meta + 类型转换 + 缓存 + 权限边界
``__init__.py``    本文件（对外唯一入口）
=================  ==========================================================

给调用方的建议
==============

**业务代码只用** :func:`get_int` / :func:`get_bool` / :func:`get_str` /
:func:`get_json` —— 不要直接碰 ``MetaRepo``。这样：

* 类型转换只有一处实现
* 新增配置项时不必改调用方（默认值来自注册表）
* 缓存生效，不会每次读都打 DB

::

    from starwatt.config_registry import get_int, get_bool

    interval = get_int("scrape_interval_sec")   # 600
    enabled = get_bool("push_group_enabled")    # True

写运行时状态（业务代码）::

    from starwatt.config_registry import set_state
    set_state("last_scrape_at", to_stamp(now_cst()))

写用户配置（**只应由配置 API 调用**）::

    from starwatt.config_registry import set_many
    errors = set_many(payload)   # {key: 中文错误}
"""
from __future__ import annotations

from starwatt.config_registry import registry, secrets
from starwatt.config_registry.registry import (
    GROUPS,
    GROUP_ORDER,
    REGISTRY,
    REMOVED_KEYS,
    by_group,
    defaults,
    keys,
    schema,
)
from starwatt.config_registry.secrets import (
    SecretDecryptError,
    is_encrypted,
    mask,
)
from starwatt.config_registry.store import (
    all_values,
    ensure_defaults,
    get,
    get_bool,
    get_float,
    get_int,
    get_json,
    get_str,
    invalidate,
    purge_removed,
    raw,
    register_all_secrets,
    reset_to_defaults,
    set,
    set_many,
    set_state,
)
from starwatt.config_registry.types import (
    Kind,
    Setting,
    T,
    ValidationError,
    is_valid_rule,
    validate_value,
)

__all__ = [
    # 声明
    "GROUPS",
    "GROUP_ORDER",
    "REGISTRY",
    "REMOVED_KEYS",
    "Kind",
    "Setting",
    "T",
    "ValidationError",
    "by_group",
    "defaults",
    "is_valid_rule",
    "keys",
    "registry",
    "schema",
    "validate_value",
    # 读写
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
    "reset_to_defaults",
    "set",
    "set_many",
    "set_state",
    # secrets
    "SecretDecryptError",
    "is_encrypted",
    "mask",
    "secrets",
]
