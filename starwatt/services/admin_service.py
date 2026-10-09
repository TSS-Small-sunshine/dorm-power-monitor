"""管理后台 —— 配置 API（Q15 七端点）+ 用户 / 审计（M4 §4.1）。

七个配置端点
============

==============================  ==========================================
端点（``/api/admin/config/...``）  本模块的函数
==============================  ==========================================
``GET  schema``                  :func:`config_schema`
``GET  ``                        :func:`config_values`
``PUT  ``                        :func:`config_update`
``POST test``                    :func:`config_test`
``GET  export``                  :func:`config_export`
``POST import``                  :func:`config_import`
``POST reset``                   :func:`config_reset`
==============================  ==========================================

其余管理能力
============

* :func:`list_users` / :func:`list_audit` —— 用户与审计（只读视图）
* :func:`bootstrap_admin` —— B4：首启用 ``BOOTSTRAP_ADMIN_PASSWORD`` 建号
* :func:`first_run_maintenance` —— 启动维护（幂等）

📌 与 OOBE 的关系（B5）：OOBE **不拥有任何配置逻辑**，它只是这批函数的
「分步引导视图」—— 同一个 ``config_update``、同一套校验、同一份审计。
"""
from __future__ import annotations

import logging
from typing import Any

from starwatt.config_registry import (
    all_values,
    ensure_defaults,
    purge_removed,
    registry,
    reset_to_defaults,
    set_many,
)
from starwatt.config_registry.portable import export_config, import_config
from starwatt.config_registry.types import Kind
from starwatt.db.repositories import AuditRepo, UserRepo
from starwatt.flags import (
    COMMAND_FLAGS,
    FLAGS,
    LAYER_FLAGS,
    enabled_flags,
    in_quiet_hours,
    is_enabled,
    why_suppressed,
)
from starwatt.logging_setup import CATEGORIES, LEVEL_BY_NAME, LOG_FILENAME, registered_secrets

logger = logging.getLogger("starwatt.config")

__all__ = [
    "FLAG_GROUP_TITLES",
    "TEST_TARGETS",
    "bootstrap_admin",
    "config_export",
    "config_import",
    "config_reset",
    "config_schema",
    "config_test",
    "config_update",
    "config_values",
    "enabled_flags",
    "ensure_defaults",
    "first_run_maintenance",
    "flags_state",
    "list_audit",
    "list_users",
    "logging_state",
]

#: ``POST /api/admin/config/test`` 支持的测试目标 → 人话标签
TEST_TARGETS: dict[str, str] = {
    "feishu_group": "飞书群机器人",
    "qq": "QQ 官方机器人",
}


# ---------------------------------------------------------------------------
# 配置读写
# ---------------------------------------------------------------------------
def config_schema() -> dict[str, Any]:
    """全部配置项的元数据（前端据此渲染表单）。"""
    return registry.schema()


def config_values(*, include_secrets: bool = False) -> dict[str, Any]:
    """当前配置值 + 当前开启的开关。

    ``include_secrets=False``（默认）时 secret 返回 ``••••1234`` ——
    默认值就是安全值，避免某个 API 忘了脱敏就泄露。
    """
    return {
        "values": all_values(kind=Kind.CONFIG, include_secrets=include_secrets),
        "flags": enabled_flags(),
    }


def config_update(payload: dict[str, Any]) -> dict[str, Any]:
    """批量更新配置（**逐键独立**，部分失败不影响其它项）。

    Returns:
        ``{"applied": [...], "errors": {key: 中文错误}}``
    """
    if not isinstance(payload, dict) or not payload:
        return {"applied": [], "errors": {"": "请求体必须是 {key: value} 对象"}}

    errors = set_many(payload)
    applied = [key for key in payload if key not in errors]
    if applied:
        logger.info("配置更新：%d 项（%s）", len(applied), "、".join(applied[:8]))
    return {"applied": applied, "errors": errors}


def config_test(target: str) -> dict[str, Any]:
    """测试某个渠道是否配置正确（配置页 / OOBE 的「发送测试消息」）。

    合并了 legacy OOBE 的 ``validate-feishu`` / ``validate-webhook``
    （B5 §5.4：端点 10 → 2）。

    Returns:
        ``{"ok": bool, "target": str, "error": str | None}``
    """
    if target == "feishu_group":
        from starwatt.notify import transport

        if not transport.resolve_webhook():
            return {"ok": False, "target": target, "error": "未配置 webhook 地址"}
        card = transport.build_text_card(
            "✅ 测试消息", "如果你看到这条，说明飞书群推送配置正确。"
        )
        # 🔑 显式动作例外：用户主动点「发送测试消息」就是为了验证凭据，
        # 即使群推送总开关关着也要真的发一次（否则按钮在关推送时失效）。
        # 这是 ``respect_flags=False`` 的**唯一**用途；自动告警一律走
        # ``flags.should_push`` + ``transport`` 出口的双重判定。
        ok = transport.post_card(card, raise_on_error=False, respect_flags=False)
        return {
            "ok": ok,
            "target": target,
            "error": None if ok else "发送失败（详见服务端日志）",
        }

    if target == "qq":
        from starwatt.notify import qq

        qq_target = qq.resolve_target()
        if qq_target is None:
            return {
                "ok": False,
                "target": target,
                "error": "还没有可用会话：先在 QQ 里给机器人发一句话，或填写目标 OpenID",
            }
        result = qq.send_text(qq_target, "✅ 测试消息：QQ 推送配置正确。")
        ok = bool(result)
        return {
            "ok": ok,
            "target": target,
            "error": None if ok else "发送失败（详见服务端日志）",
        }

    return {
        "ok": False,
        "target": target,
        "error": f"未知的测试目标（可用：{sorted(TEST_TARGETS)}）",
    }


def config_export(*, include_secrets: bool = False) -> dict[str, Any]:
    """导出配置（N16）。"""
    return export_config(include_secrets=include_secrets)


def config_import(payload: Any, *, allow_secrets: bool = False) -> dict[str, Any]:
    """导入配置（N16，合并语义）。"""
    return import_config(payload, allow_secrets=allow_secrets)


def config_reset() -> dict[str, Any]:
    """把配置恢复成默认值（**不动** STATE / CACHE）。"""
    return {"reset": reset_to_defaults()}


# ---------------------------------------------------------------------------
# 用户 / 审计
# ---------------------------------------------------------------------------
def list_users() -> list[dict[str, Any]]:
    """用户列表（**绝不含密码哈希**）。"""
    return [
        {
            "id": user.id,
            "username": user.username,
            "role": user.role,
            "created_at": user.created_at,
            "last_login_at": user.last_login_at,
            "disabled": bool(user.disabled),
            "must_change_password": bool(user.must_change_password),
        }
        for user in UserRepo.list_all()
    ]


def list_audit(limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
    """审计日志（只读、倒序；``limit`` 上限 1000）。"""
    return [
        {
            "id": entry.id,
            "action": entry.action,
            "user_id": entry.user_id,
            "target": entry.target,
            "ip": entry.ip,
            "created_at": entry.created_at,
            "details": entry.details,
        }
        for entry in AuditRepo.recent(limit=max(1, min(limit, 1000)), action=action)
    ]


# ---------------------------------------------------------------------------
# 功能开关 / 日志的**实时状态**（5.7 / 5.8）
# ---------------------------------------------------------------------------
#: 开关三段的人话标题（顺序 = 页面上的顺序）
FLAG_GROUP_TITLES: tuple[tuple[str, str], ...] = (
    ("master", "总开关"),
    ("layer", "告警层（群推送）"),
    ("command", "命令（私聊机器人）"),
)


def flags_state() -> dict[str, Any]:
    """全部开关 + **此刻的生效状态**（5.7 的「实时状态提示」）。

    为什么要有这个端点（而不是让前端从 schema 里猜）：开关的父子语义住在
    :mod:`starwatt.flags`（``parent``），而「某层此刻为什么不推」由
    :func:`starwatt.flags.why_suppressed` 计算 —— 那是**运行时判断**
    （总开关 / 分项开关 / 静默时段三选一），前端复现不了，也不该复现。

    Returns:
        ``{"groups", "flags", "suppressed", "quiet_hours"}``
    """
    layer_keys = set(LAYER_FLAGS.values())
    command_keys = set(COMMAND_FLAGS.values())

    def kind_of(key: str) -> str:
        if key in layer_keys:
            return "layer"
        if key in command_keys:
            return "command"
        return "master"

    flags = [
        {
            "key": key,
            "label": flag.label,
            "parent": flag.parent,
            "kind": kind_of(key),
            "enabled": is_enabled(key),
        }
        for key, flag in FLAGS.items()
    ]

    suppressed = []
    for layer, key in LAYER_FLAGS.items():
        reason = why_suppressed(layer)
        if reason:
            suppressed.append(
                {"layer": layer, "key": key, "label": FLAGS[key].label, "reason": reason}
            )

    return {
        "groups": [{"kind": kind, "title": title} for kind, title in FLAG_GROUP_TITLES],
        "flags": flags,
        "suppressed": suppressed,
        "quiet_hours": in_quiet_hours(),
    }


def logging_state() -> dict[str, Any]:
    """日志设置的**实时状态**（5.8）：级别 / 类别 / 覆盖 / 格式 / 文件。

    ``effective`` 把「全局级别 + 模块级覆盖」算成一张表 —— 改
    ``log_overrides`` 时最容易犯的错是「以为生效了其实没有」，这张表直接
    回答「现在到底几级」。

    Returns:
        ``{"levels", "categories", "global_level", "format", "overrides",
        "effective", "file", "masked_secrets"}``
    """
    from starwatt.config import get_settings
    from starwatt.config_registry import get_bool, get_int, get_json, get_str

    raw_overrides = get_json("log_overrides", {}) or {}
    overrides = {
        str(key): str(value)
        for key, value in raw_overrides.items()
        if str(key) in CATEGORIES  # 拼错的类别直接不显示（后端也不认）
    }
    global_level = get_str("log_level", "INFO")

    enabled = get_bool("log_file_enabled", False)
    log_path = ""
    if enabled:
        log_path = str(get_settings().data_dir / "logs" / LOG_FILENAME)

    return {
        # 级别按数值升序（TRACE → CRITICAL），前端直接照抄顺序
        "levels": [name for name, _ in sorted(LEVEL_BY_NAME.items(), key=lambda kv: kv[1])],
        "categories": list(CATEGORIES),
        "global_level": global_level,
        "format": get_str("log_format", "text"),
        "overrides": overrides,
        "effective": {
            category: overrides.get(category, global_level) for category in CATEGORIES
        },
        "file": {
            "enabled": enabled,
            "path": log_path,
            "retention_days": get_int("log_file_retention_days", 7),
        },
        "masked_secrets": registered_secrets(),
    }


# ---------------------------------------------------------------------------
# 启动维护（B4）
# ---------------------------------------------------------------------------
def bootstrap_admin() -> dict[str, Any]:
    """首启建号：``users`` 为空 且 ``BOOTSTRAP_ADMIN_PASSWORD`` 存在 → 建管理员。

    见 ``docs/BLOCKER_FIXES.md`` §4（B4）：随机密码由 ``install.sh`` /
    Docker entrypoint 写入 ``.env``，**用后即焚**，并强制首登改密。

    Returns:
        ``{"created": bool, "username": str | None}``
    """
    from starwatt.auth.service import ensure_bootstrap_admin

    try:
        user = ensure_bootstrap_admin()
    except Exception:  # noqa: BLE001 —— 建号失败不该阻止服务启动
        logger.exception("bootstrap 建号失败（忽略，服务继续启动）")
        return {"created": False, "username": None}

    if user is not None:
        logger.warning(
            "已用 bootstrap 密码创建管理员 %s —— 请立即登录并修改密码", user.username
        )
    return {"created": user is not None, "username": user.username if user else None}


def first_run_maintenance() -> dict[str, Any]:
    """启动维护（**幂等**，可重复调用）。

    ==========================  ==========================================
    步骤                          作用
    ==========================  ==========================================
    ``db.init()``                建表（幂等）
    ``purge_removed()``          删掉已下线的旧键（``admin_password``，L20）
    ``ensure_defaults()``        把未写入的配置项按默认值落库
    ``register_all_secrets()``   把 secret 交给日志脱敏（Q20）
    ``bootstrap_admin()``        首启建号（B4）
    ==========================  ==========================================
    """
    from starwatt.config_registry import register_all_secrets
    from starwatt.db.connection import init as db_init

    db_init()
    report: dict[str, Any] = {
        "purged": purge_removed(),
        "defaults": ensure_defaults(),
        "bootstrap": bootstrap_admin(),
    }
    register_all_secrets()
    return report
