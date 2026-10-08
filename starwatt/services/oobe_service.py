"""OOBE 引导（B4 / B5）—— **配置中心的引导子集**。

设计要点（B5）
==============

OOBE **不拥有任何配置逻辑**：它只是同一批配置的「分步引导视图」。

::

    config_registry（唯一配置定义）
            │
    ┌───────┴────────┐
    ▼                ▼
  配置中心（全量）    OOBE（引导子集）
  /admin/config      /oobe，按 5 步渲染指定键
    └───────┬────────┘
            ▼
     同一个校验 / 同一份审计

因此本模块只做两件事：**分组**（哪一步显示哪些键）+ **推进**（校验并保存）。

5 步（B4：删掉「建号」步，6 → 5）
=================================

=====  ====================  ==================================================
步      标题                    复用的注册表键
=====  ====================  ==================================================
1       欢迎                   —（无配置）
2       学校接入                ``dorm_base_url`` ``dorm_openid`` ``dorm_room_id``
                               ``last_room_id``（只读状态） ``eqprice``
3       飞书群推送              ``feishu_webhook_url`` ``feishu_secret``
                               ``push_group_enabled``
4       飞书机器人              ``feishu_app_id`` ``feishu_app_secret``
                               ``feishu_verification_token`` ``feishu_encrypt_key``
                               ``push_bot_enabled``
5       推送偏好 + 完成          ``push_l1_enable`` ``push_l2_enable``
                               ``push_daily_enable`` ``push_daily_time``
                               ``push_weekly_time`` ``push_monthly_time``
                               ``quiet_hours_start`` ``quiet_hours_end``
=====  ====================  ==================================================

端点收敛（B5 §5.4：**10 → 2**）
===============================

* ``GET  /api/oobe/state``   → :func:`state`
* ``POST /api/oobe/advance`` → :func:`advance`（``direction`` = next/prev/skip；
  完成也走它）
* legacy 的 ``/admin/api/oobe/save`` ❌ 删除（消除 L6 双后端）

📌 建号**不在** OOBE 里（B4）：安装阶段用 ``BOOTSTRAP_ADMIN_PASSWORD`` 建号，
OOBE 只做配置。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from starwatt.config_registry import all_values, get_bool, get_int, registry, set_state
from starwatt.config_registry.types import Kind

logger = logging.getLogger("starwatt.config")

__all__ = [
    "LAST_INDEX",
    "STATE_OOBE_COMPLETED",
    "STATE_OOBE_STEP",
    "STEPS",
    "Step",
    "advance",
    "state",
    "step_payload",
]

#: OOBE 进度状态键（注册表 ``SITE`` 分组的 advanced 项）
STATE_OOBE_STEP = "oobe_step"
STATE_OOBE_COMPLETED = "oobe_completed"


@dataclass(frozen=True, slots=True)
class Step:
    """一个引导步骤。"""

    index: int
    key: str
    title: str
    description: str
    #: 本步展示 / 可写的注册表键（顺序即渲染顺序）
    keys: tuple[str, ...] = ()
    #: 判定「这步填完了」所需的键（空 = 无需填写）
    required: tuple[str, ...] = field(default=())

    @property
    def writable(self) -> tuple[str, ...]:
        """可写键 —— 只保留 ``CONFIG``（``STATE`` 由系统维护，写会报错）。"""
        out: list[str] = []
        for key in self.keys:
            setting = registry.get(key)
            if setting is not None and setting.kind is Kind.CONFIG:
                out.append(key)
        return tuple(out)


#: 5 步定义（顺序 = 用户看到的顺序）
STEPS: tuple[Step, ...] = (
    Step(
        index=0,
        key="welcome",
        title="欢迎",
        description="几步把宿舍电量监控配起来；随时可以跳过，之后在「配置中心」里改。",
    ),
    Step(
        index=1,
        key="school",
        title="学校接入",
        description="粘贴学校 H5 页面地址，自动解析凭据与房间号，并立即验证连通。",
        keys=(
            "dorm_base_url",
            "dorm_openid",
            "dorm_room_id",
            "last_room_id",
            "eqprice",
        ),
        required=("dorm_openid",),
    ),
    Step(
        index=2,
        key="feishu_group",
        title="飞书群推送",
        description="在飞书群里添加「自定义机器人」，把 Webhook 地址粘过来。",
        keys=("feishu_webhook_url", "feishu_secret", "push_group_enabled"),
    ),
    Step(
        index=3,
        key="feishu_bot",
        title="飞书机器人",
        description="想要私聊 / QQ 推送再填这步；只推群消息可以跳过。",
        keys=(
            "feishu_app_id",
            "feishu_app_secret",
            "feishu_verification_token",
            "feishu_encrypt_key",
            "push_bot_enabled",
        ),
    ),
    Step(
        index=4,
        key="preferences",
        title="推送偏好 + 完成",
        description="挑一下要收哪些提醒、什么时候不要打扰，然后完成。",
        keys=(
            "push_l1_enable",
            "push_l2_enable",
            "push_daily_enable",
            "push_daily_time",
            "push_weekly_time",
            "push_monthly_time",
            "quiet_hours_start",
            "quiet_hours_end",
        ),
    ),
)

#: 最后一步的下标（走完它 = 完成）
LAST_INDEX = len(STEPS) - 1


def _clamp_step(value: Any) -> int:
    """把任意输入收敛到合法步号（越界 / 非数字一律回到 0）。"""
    try:
        step = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(step, LAST_INDEX))


def _is_done(step: Step, values: dict[str, Any]) -> bool:
    """某步是否已填完（``required`` 全部非空）。

    📌 secret 值在 ``values`` 里是脱敏的 ``••••1234`` —— 非空即视为已填。
    """
    return all(str(values.get(key) or "").strip() for key in step.required)


def step_payload(step: Step, values: dict[str, Any]) -> dict[str, Any]:
    """某步的完整载荷（含表单元数据 —— 前端复用配置中心的渲染器）。"""
    settings = []
    for key in step.keys:
        setting = registry.get(key)
        if setting is not None:
            settings.append(setting.to_schema())
    return {
        "index": step.index,
        "key": step.key,
        "title": step.title,
        "description": step.description,
        "keys": list(step.keys),
        "writable": list(step.writable),
        "required": list(step.required),
        "done": _is_done(step, values),
        "settings": settings,
    }


def state() -> dict[str, Any]:
    """``GET /api/oobe/state`` 的载荷。

    Returns:
        ``{"step", "total", "completed", "steps", "current", "values"}``
    """
    values = all_values(kind=Kind.CONFIG, include_secrets=False, include_defaults=True)
    current = _clamp_step(get_int(STATE_OOBE_STEP, 0))
    return {
        "step": current,
        "total": len(STEPS),
        "completed": bool(get_bool(STATE_OOBE_COMPLETED, False)),
        "steps": [
            {
                "index": step.index,
                "key": step.key,
                "title": step.title,
                "done": _is_done(step, values),
            }
            for step in STEPS
        ],
        "current": step_payload(STEPS[current], values),
        "values": values,
    }


def advance(
    *,
    direction: str = "next",
    values: dict[str, Any] | None = None,
    skip: bool = False,
) -> dict[str, Any]:
    """``POST /api/oobe/advance`` —— 保存本步 + 前进 / 后退 / 跳过 / 完成。

    Args:
        direction: ``"next"`` / ``"prev"``。
        values: 本步要保存的配置（走同一套校验，**逐键独立**）。
        skip: ``True`` 时只移动步号、**不保存**。

    Returns:
        :func:`state` 的载荷 + ``{"saved": {...}}``（本步保存结果）。
    """
    from starwatt.services import admin_service

    current = _clamp_step(get_int(STATE_OOBE_STEP, 0))
    step = STEPS[current]

    saved: dict[str, Any] = {"applied": [], "errors": {}}
    if values and not skip:
        allowed = set(step.writable)
        payload = {key: value for key, value in values.items() if key in allowed}
        if payload:
            saved = admin_service.config_update(payload)

    if direction == "prev":
        target = max(0, current - 1)
    else:
        target = min(LAST_INDEX, current + 1)

    if direction != "prev" and target == LAST_INDEX:
        # 走到最后一步 = 完成（B5：``/complete`` 合并进 ``advance``）
        set_state(STATE_OOBE_COMPLETED, True)
        logger.info("OOBE 完成（共 %d 步）", len(STEPS))

    set_state(STATE_OOBE_STEP, target)
    payload = state()
    payload["saved"] = saved
    return payload
