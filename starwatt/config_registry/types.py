"""配置项的类型系统与校验引擎。

三个概念
========

``T`` —— 值的**类型**（前端据此选控件：开关 / 数字框 / 文本框 / 下拉 / 时间选择器）

``Kind`` —— 存储的**语义分类**（Q15 的「三类划分」）:

===========  ==========================  ==================  ==============
Kind         含义                        谁会写              WebUI
===========  ==========================  ==================  ==============
``CONFIG``   用户可改的配置              用户（PUT /config）  **可读写**
``STATE``    运行时状态（游标 / 时间戳）  业务代码            只读
``CACHE``    派生缓存（可随时重建）      业务代码            不可见
===========  ==========================  ==================  ==============

这个划分是**权限边界**，不是分类学：

* 只有 ``CONFIG`` 能被 ``PUT /api/admin/config`` 写
* ``STATE`` / ``CACHE`` 由代码写 —— 否则用户能把 ``last_scrape_at`` 改成
  任意值，让 stale 检测与报表去重彻底失效
* 导出配置时**只导出** ``CONFIG``

``validate`` —— 声明式校验规则串
================================

======================  ==========================================
``1..720``              数值范围（闭区间，int 或 float）
``enum:a|b|c``          取值白名单
``regex:^...$``         正则
``len:1..64``           字符串长度范围
``HH:MM``               24 小时制时间
``url``                 http/https URL
``email``               邮箱（简易）
``json``                合法 JSON
``none``                不校验（仅类型检查）
======================  ==========================================
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlparse

__all__ = [
    "Kind",
    "Setting",
    "T",
    "ValidationError",
    "is_valid_rule",
    "validate_value",
]


class T(Enum):
    """配置值的类型（决定前端控件与后端转换）。"""

    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    STR = "str"
    SECRET = "secret"  # 加密存储 + 脱敏返回
    TIME = "time"  # "HH:MM"
    ENUM = "enum"  # 配合 choices / validate="enum:a|b|c"
    DURATION = "duration"  # 秒数
    URL = "url"
    JSON = "json"


class Kind(Enum):
    """存储语义分类（Q15 三类划分）—— 也是权限边界。"""

    CONFIG = "config"  # 用户可改
    STATE = "state"  # 代码写，UI 只读
    CACHE = "cache"  # 代码写，UI 不可见


class ValidationError(ValueError):
    """配置值不合法。``key`` 用于前端把错误定位到具体字段。"""

    def __init__(self, key: str, message: str) -> None:
        self.key = key
        self.message = message
        super().__init__(f"{key}: {message}")


@dataclass(frozen=True, slots=True)
class Setting:
    """一个配置项的**唯一真相**声明。"""

    key: str  # meta 键名（沿用现有 snake_case → 零迁移）
    label: str  # 中文标签（UI 显示）
    group: str  # 分组（UI 分栏）
    type: T
    default: Any
    kind: Kind = Kind.CONFIG
    help: str = ""  # UI 帮助文本
    validate: str = "none"  # 校验规则串
    requires_restart: bool = False
    depends_on: str | None = None  # 条件显示：仅当该布尔项为真时可见
    advanced: bool = False  # 默认折叠
    choices: tuple[str, ...] = field(default=())  # ENUM 的选项

    # -- 派生属性 ---------------------------------------------------------
    @property
    def secret(self) -> bool:
        """是否需要加密存储 + 脱敏返回。"""
        return self.type is T.SECRET

    @property
    def user_editable(self) -> bool:
        """用户能否通过配置 API 修改。"""
        return self.kind is Kind.CONFIG

    @property
    def visible_in_ui(self) -> bool:
        """是否出现在配置页面（``CACHE`` 完全隐藏）。"""
        return self.kind is not Kind.CACHE

    def to_schema(self) -> dict[str, Any]:
        """给前端的元数据（**不含**当前值）。"""
        return {
            "key": self.key,
            "label": self.label,
            "group": self.group,
            "type": self.type.value,
            "kind": self.kind.value,
            "default": None if self.secret else self.default,
            "help": self.help,
            "validate": self.validate,
            "requires_restart": self.requires_restart,
            "depends_on": self.depends_on,
            "advanced": self.advanced,
            "choices": list(self.choices),
            "secret": self.secret,
            "user_editable": self.user_editable,
        }


# ---------------------------------------------------------------------------
# 校验引擎
# ---------------------------------------------------------------------------
_RANGE = re.compile(r"^(-?\d+(?:\.\d+)?)\.\.(-?\d+(?:\.\d+)?)$")
_LEN = re.compile(r"^len:(\d+)\.\.(\d+)$")
_ENUM = re.compile(r"^enum:(.+)$")
_REGEX = re.compile(r"^regex:(.+)$")
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _fmt(number: float) -> str:
    """``30.0`` → ``30``（让错误消息好看些）。"""
    return str(int(number)) if number == int(number) else str(number)


#: 不带参数的关键字规则
_KEYWORD_RULES = frozenset({"url", "email", "json"})


def is_valid_rule(rule: str) -> bool:
    """``rule`` 是否是可识别的校验规则串。

    供注册表自检使用：规则串写错（例如 ``"1-720"`` 少了点）会让校验
    **静默失效** —— 配置项看着有校验，实际什么都放行。
    """
    if rule in ("", "none"):
        return True
    if rule in _KEYWORD_RULES:
        return True
    return any(
        pattern.match(rule) is not None
        for pattern in (_RANGE, _LEN, _ENUM, _REGEX)
    )


def _coerce_number(key: str, value: Any) -> float:
    if isinstance(value, bool):  # bool 是 int 的子类 —— 必须显式排除
        raise ValidationError(key, "必须是数字")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ValidationError(key, "必须是数字")


def _check_range(key: str, value: Any, rule: str) -> None:
    match = _RANGE.match(rule)
    if match is None:
        return
    low, high = float(match.group(1)), float(match.group(2))
    number = _coerce_number(key, value)
    if not (low <= number <= high):
        raise ValidationError(key, f"必须在 {_fmt(low)}..{_fmt(high)} 之间")


def _check_url(key: str, value: Any) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(key, "必须是 URL")
    parsed = urlparse(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError(key, "必须是 http:// 或 https:// 开头的完整地址")


def _check_json(key: str, value: Any) -> None:
    if isinstance(value, (dict, list)):
        return
    if not isinstance(value, str):
        raise ValidationError(key, "必须是合法 JSON")
    try:
        json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ValidationError(key, "不是合法 JSON") from exc


def _check_len(key: str, value: Any, rule: str) -> None:
    match = _LEN.match(rule)
    if match is None:
        return
    low, high = int(match.group(1)), int(match.group(2))
    if not isinstance(value, str):
        raise ValidationError(key, "必须是字符串")
    if not (low <= len(value) <= high):
        raise ValidationError(key, f"长度必须在 {low}..{high} 之间")


def _check_enum(key: str, value: Any, rule: str) -> None:
    match = _ENUM.match(rule)
    if match is None:
        return
    allowed = match.group(1).split("|")
    if str(value) not in allowed:
        raise ValidationError(key, "必须是 " + " / ".join(allowed) + " 之一")


def _check_regex(key: str, value: Any, rule: str) -> None:
    match = _REGEX.match(rule)
    if match is None:
        return
    if not isinstance(value, str) or re.search(match.group(1), value) is None:
        raise ValidationError(key, "格式不正确")


def _check_email(key: str, value: Any) -> None:
    if not isinstance(value, str) or "@" not in value or "." not in value:
        raise ValidationError(key, "不是合法邮箱")


def _check_rule(key: str, value: Any, rule: str) -> None:
    """按规则串的前缀分派。每个分支独立成函数，便于单测与维护。"""
    if rule in ("", "none"):
        return
    if _RANGE.match(rule):
        _check_range(key, value, rule)
    elif _LEN.match(rule):
        _check_len(key, value, rule)
    elif _ENUM.match(rule):
        _check_enum(key, value, rule)
    elif _REGEX.match(rule):
        _check_regex(key, value, rule)
    elif rule == "url":
        _check_url(key, value)
    elif rule == "email":
        _check_email(key, value)
    elif rule == "json":
        _check_json(key, value)


def _check_numeric_type(key: str, kind: T, value: Any) -> None:
    """BOOL / INT / FLOAT / DURATION 的类型检查。"""
    if kind is T.BOOL:
        if not isinstance(value, bool):
            raise ValidationError(key, "必须是 true / false")
    elif kind in (T.INT, T.DURATION):
        number = _coerce_number(key, value)
        if number != int(number):
            raise ValidationError(key, "必须是整数")
    elif kind is T.FLOAT:
        _coerce_number(key, value)


def _check_text_type(key: str, kind: T, value: Any) -> None:
    """TIME / URL / JSON / STR / SECRET / ENUM 的类型检查。"""
    if kind is T.TIME:
        if not isinstance(value, str) or _TIME.match(value.strip()) is None:
            raise ValidationError(key, "必须是 HH:MM 格式（24 小时制）")
    elif kind is T.URL:
        _check_url(key, value)
    elif kind is T.JSON:
        _check_json(key, value)
    elif kind in (T.STR, T.SECRET, T.ENUM) and not isinstance(value, str):
        raise ValidationError(key, "必须是字符串")


def _check_type(setting: Setting, value: Any) -> None:
    """按 ``setting.type`` 做类型检查。"""
    _check_numeric_type(setting.key, setting.type, value)
    _check_text_type(setting.key, setting.type, value)


def validate_value(setting: Setting, value: Any) -> None:
    """按 ``setting`` 的类型与 ``validate`` 规则校验 ``value``。

    Raises:
        ValidationError: 不合法（消息为中文，可直接显示给用户）。
    """
    _check_type(setting, value)

    # ENUM 白名单（choices 与 validate 双写时以 choices 为准）
    if setting.type is T.ENUM and setting.choices and value not in setting.choices:
        raise ValidationError(
            setting.key, "必须是 " + " / ".join(setting.choices) + " 之一"
        )

    _check_rule(setting.key, value, setting.validate)
