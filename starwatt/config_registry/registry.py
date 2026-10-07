"""配置注册表 —— **每一个**配置项的声明式定义（Q15 的唯一真相）。

两条硬约束
==========

**1. 键名零迁移**
现有 37 个 ``meta`` 键名**一个都不改**（Q9/Q10）。重写后旧库直接可用，
新旧代码读写同一张表，可并行开发、秒级回滚。

**2. 覆盖性（RK13）**
M1 做全量常量审计：``grep`` 魔法数字 → 每一个都要有归属。
漏一个就意味着「所有配置可改」做不到 100%。

分组（8 组，UI 分栏）
====================

======  ==============================================================
站点     站点名 / 时区（锁定只读）/ 主题色
数据采集  openid(secret) / base_url / 房间 / 单价 / 节流 / 重试 / 超时 / UA
告警阈值  低电三档 / 离线判定 / stale 判定 / 违规冷却
推送（群）总开关 / webhook(secret) / 各层开关 / 时间 / 静默时段 / 接收人
机器人    总开关 / app_id / app_secret(secret) / 校验与加密密钥 / 9 个命令
认证      session 时长 / 锁定策略 / 密码策略 / public_readonly
日志      全局级别 / 模块级覆盖 / 格式 / 文件轮转
高级      api_internal_token(secret) / 备份 / DB 路径（只读展示）
======  ==============================================================

已**删除**的键
==============

``admin_password``（L20 🔴）—— 明文管理员密码，``web.py:1240`` 拿它认证，
三处明文写入。R68 文档谎称已移除。Q21 决定**彻底删除**：改走
``starwatt.auth`` 的 scrypt 哈希 + 服务端会话。
"""
from __future__ import annotations

from typing import Any

from starwatt.config_registry.types import Kind, Setting, T

__all__ = [
    "GROUPS",
    "GROUP_ORDER",
    "REGISTRY",
    "REMOVED_KEYS",
    "by_group",
    "defaults",
    "get",
    "keys",
    "schema",
]

# ---------------------------------------------------------------------------
# 分组（顺序即 UI 显示顺序）
# ---------------------------------------------------------------------------
SITE = "站点"
SCRAPE = "数据采集"
THRESHOLD = "告警阈值"
PUSH = "推送（群）"
BOT = "机器人（私聊）"
AUTH = "认证"
LOG = "日志"
ADVANCED = "高级"

GROUP_ORDER: tuple[str, ...] = (
    SITE,
    SCRAPE,
    THRESHOLD,
    PUSH,
    BOT,
    AUTH,
    LOG,
    ADVANCED,
)

GROUPS: dict[str, str] = {
    SITE: "站点信息与外观",
    SCRAPE: "学校接口的抓取参数",
    THRESHOLD: "触发告警的电量阈值",
    PUSH: "飞书群机器人群推送",
    BOT: "飞书私聊机器人（事件订阅）",
    AUTH: "登录与会话策略",
    LOG: "运行日志",
    ADVANCED: "高级（改动需谨慎）",
}

# ---------------------------------------------------------------------------
# 构造函数（少写重复字段）
# ---------------------------------------------------------------------------
_C = Kind.CONFIG
_S = Kind.STATE
_X = Kind.CACHE


def cfg(
    key: str,
    label: str,
    group: str,
    type_: T,
    default: Any,
    **kw: Any,
) -> Setting:
    return Setting(
        key=key, label=label, group=group, type=type_, default=default,
        kind=_C, **kw,
    )


def state(
    key: str, label: str, group: str, type_: T, default: Any, **kw: Any
) -> Setting:
    return Setting(
        key=key, label=label, group=group, type=type_, default=default,
        kind=_S, **kw,
    )


def cache(
    key: str, label: str, group: str, type_: T, default: Any, **kw: Any
) -> Setting:
    return Setting(
        key=key, label=label, group=group, type=type_, default=default,
        kind=_X, **kw,
    )


# ---------------------------------------------------------------------------
# 站点
# ---------------------------------------------------------------------------
_SITE_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "site_name", "站点名称", SITE, T.STR, "StarWatt 星瓦",
        help="显示在页面标题与推送卡片上", validate="len:1..32",
    ),
    cfg(
        "site_timezone", "时区", SITE, T.STR, "Asia/Shanghai",
        help="🔒 锁定只读 —— 全项目时间语义依赖它（Q10）",
        requires_restart=True,
    ),
    cfg(
        "theme_color", "主题色", SITE, T.STR, "#1677ff",
        help="前端主色调（十六进制）", validate="regex:^#[0-9a-fA-F]{6}$",
    ),
    cfg(
        "room_label", "房间显示名", SITE, T.STR, "",
        help="留空则用学校返回的房间名", validate="len:0..32",
    ),
    state("oobe_completed", "OOBE 已完成", SITE, T.BOOL, False, advanced=True),
    state("oobe_step", "OOBE 当前步骤", SITE, T.INT, 0, advanced=True),
)


# ---------------------------------------------------------------------------
# 数据采集
# ---------------------------------------------------------------------------
_SCRAPE_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "dorm_base_url", "学校接口地址", SCRAPE, T.URL,
        "https://xydf.xxx.edu.cn",
        help="宿舍电量系统的根地址", validate="url",
    ),
    cfg(
        "dorm_openid", "登录凭据 openid", SCRAPE, T.SECRET, "",
        help="🔒 加密存储。浏览器登录后从 Cookie 里复制",
    ),
    cfg(
        "dorm_room_id", "宿舍房间号", SCRAPE, T.STR, "",
        help="留空则每次抓取时自动发现", validate="len:0..64",
    ),
    cfg(
        "eqprice", "电价（元/度）", SCRAPE, T.FLOAT, 0.5,
        help="仅用于金额估算", validate="0..100",
    ),
    cfg(
        "scrape_interval_sec", "抓取间隔（秒）", SCRAPE, T.DURATION, 600,
        help="默认 10 分钟", validate="60..86400",
    ),
    cfg(
        "scrape_f2_throttle_hours", "F2 每日用电节流（小时）", SCRAPE, T.INT, 24,
        help="该接口数据一天只变一次，不必频繁拉", validate="1..720",
        advanced=True,
    ),
    cfg(
        "scrape_f3_throttle_hours", "F3 违规记录节流（小时）", SCRAPE, T.INT, 1,
        validate="1..720", advanced=True,
    ),
    cfg(
        "scrape_f5_throttle_hours", "F5 缴费历史节流（小时）", SCRAPE, T.INT, 24,
        validate="1..720", advanced=True,
    ),
    cfg(
        "scrape_timeout_sec", "单次请求超时（秒）", SCRAPE, T.DURATION, 15,
        validate="3..120", advanced=True,
    ),
    cfg(
        "scrape_retry_count", "失败重试次数", SCRAPE, T.INT, 3,
        help="指数退避，仅对传输层错误重试", validate="0..10", advanced=True,
    ),
    cfg(
        "scrape_user_agent", "请求 UA", SCRAPE, T.STR,
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
        "MicroMessenger/8.0.49",
        help="学校接口只对微信内置浏览器放行，勿随意改动",
        validate="len:10..400", advanced=True,
    ),
    cfg(
        "semester_start_date", "学期起始日", SCRAPE, T.STR, "2026-09-07",
        help="📌 F5 缴费查询锚定此日期（不是 30 天滚动窗口）",
        validate="regex:^\\d{4}-\\d{2}-\\d{2}$", advanced=True,
    ),
    state("backfill_done", "历史回填已完成", SCRAPE, T.BOOL, False, advanced=True),
    state("last_scrape_at", "上次抓取时间", SCRAPE, T.STR, ""),
    state("last_scrape_status", "上次抓取结果", SCRAPE, T.STR, "",
          choices=("ok", "stale", "error", "never")),
    state("last_daily_elect_at", "上次 F2 抓取", SCRAPE, T.STR, "", advanced=True),
    state("last_pay_at", "上次 F5 抓取", SCRAPE, T.STR, "", advanced=True),
    state("last_violation_check_at", "上次 F3 抓取", SCRAPE, T.STR, "",
          advanced=True),
    state("last_room_id", "上次发现的房间 ID", SCRAPE, T.STR, "", advanced=True),
)


# ---------------------------------------------------------------------------
# 告警阈值（B10 颜色分级 + B6 离线 + B7 stale + B8 冷却）
# ---------------------------------------------------------------------------
_THRESHOLD_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "threshold_red", "低电红档（度）", THRESHOLD, T.FLOAT, 30,
        help="剩余电量低于此值 → 红色卡片 + L1 告警", validate="0..100000",
    ),
    cfg(
        "threshold_orange", "低电橙档（度）", THRESHOLD, T.FLOAT, 80,
        validate="0..100000",
    ),
    cfg(
        "threshold_blue", "低电蓝档（度）", THRESHOLD, T.FLOAT, 200,
        validate="0..100000",
    ),
    cfg(
        "offline_status_values", "视为在线的状态值", THRESHOLD, T.JSON,
        ["在线", "正常", "通讯正常"],
        help="📌 不在列表中的值（含字段缺失）一律判定为离线",
        validate="json",
    ),
    cfg(
        "stale_hours", "数据陈旧阈值（小时）", THRESHOLD, T.INT, 2,
        help="距上次成功抓取超过此时长 → 推 stale 橙卡", validate="1..168",
    ),
    cfg(
        "violation_cooldown_min", "违规告警冷却（分钟）", THRESHOLD, T.INT, 30,
        validate="1..1440",
    ),
    cfg(
        "violation_power_threshold", "违规标红功率（kW）", THRESHOLD, T.FLOAT, 1.0,
        help="📌 F3 返回的 wg_power 单位是 kW（功率），不是度（电量）",
        validate="0..100",
    ),
    cfg(
        "low_battery_rearm", "低电告警复归值（度）", THRESHOLD, T.FLOAT, 40,
        help="📌 只在「非低电 → 低电」跃迁时告警；回升到此值以上才重新武装",
        validate="0..100000",
    ),
)


# ---------------------------------------------------------------------------
# 推送（群机器人）
# ---------------------------------------------------------------------------
_PUSH_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "push_group_enabled", "群推送总开关", PUSH, T.BOOL, True,
        help="关闭后所有群卡片静默丢弃（记 NOTICE，不报错）",
    ),
    cfg(
        "feishu_webhook_url", "飞书群 Webhook 地址", PUSH, T.SECRET, "",
        help="🔒 加密存储。自定义机器人 → 复制 Webhook 地址",
    ),
    cfg(
        "feishu_secret", "飞书群签名密钥", PUSH, T.SECRET, "",
        help="🔒 加密存储。机器人开启「签名校验」时必填",
    ),
    cfg(
        "push_l1_enable", "L1 低电告警", PUSH, T.BOOL, True,
        help="📌 旧版此项是死代码（L21），重写后真正生效",
        depends_on="push_group_enabled",
    ),
    cfg(
        "push_l2_enable", "L2 常规摘要卡", PUSH, T.BOOL, True,
        help="📌 旧版此项是死代码（L21），重写后真正生效",
        depends_on="push_group_enabled",
    ),
    cfg(
        "push_daily_enable", "L3 日报", PUSH, T.BOOL, True,
        depends_on="push_group_enabled",
    ),
    cfg(
        "push_weekly_enable", "L3 周报", PUSH, T.BOOL, True,
        depends_on="push_group_enabled",
    ),
    cfg(
        "push_monthly_enable", "L3 月报", PUSH, T.BOOL, True,
        depends_on="push_group_enabled",
    ),
    cfg(
        "push_offline_enable", "L4 电表离线告警", PUSH, T.BOOL, True,
        help="🆕 旧版无此开关，离线卡总是发送", depends_on="push_group_enabled",
    ),
    cfg(
        "push_violation_enable", "违规告警", PUSH, T.BOOL, True,
        help="🆕 旧版无此开关", depends_on="push_group_enabled",
    ),
    cfg(
        "push_stale_enable", "数据陈旧告警", PUSH, T.BOOL, True,
        help="🆕 旧版无此开关", depends_on="push_group_enabled",
    ),
    cfg(
        "push_daily_time", "日报时间", PUSH, T.TIME, "09:00",
        depends_on="push_daily_enable",
    ),
    cfg(
        "push_weekly_time", "周报时间（周一）", PUSH, T.TIME, "09:00",
        depends_on="push_weekly_enable",
    ),
    cfg(
        "push_monthly_time", "月报时间（每月 1 号）", PUSH, T.TIME, "09:00",
        depends_on="push_monthly_enable",
    ),
    cfg(
        "quiet_hours_start", "静默时段开始", PUSH, T.TIME, "23:00",
        help="📌 旧版此项是死代码（L21）—— 静默时段从未生效",
    ),
    cfg(
        "quiet_hours_end", "静默时段结束", PUSH, T.TIME, "07:00",
    ),
    cfg(
        "push_receivers_l1", "L1 接收人（open_id，逗号分隔）", PUSH, T.STR, "",
        help="📌 旧版此项零调用（L21）。留空 = 只发群", advanced=True,
        validate="len:0..512",
    ),
    cfg(
        "push_receivers_l2", "L2 接收人", PUSH, T.STR, "", advanced=True,
        validate="len:0..512",
    ),
    cfg(
        "push_receivers_report", "报表接收人", PUSH, T.STR, "", advanced=True,
        validate="len:0..512",
    ),
    cfg(
        "push_receivers_alert", "告警接收人", PUSH, T.STR, "", advanced=True,
        validate="len:0..512",
    ),
    # -- 报表去重状态（代码写） -------------------------------------------
    state("last_daily_report_date", "上次日报日期", PUSH, T.STR, "", advanced=True),
    state("last_weekly_report_iso", "上次周报 ISO 周", PUSH, T.STR, "",
          advanced=True),
    state("last_monthly_report_mo", "上次月报月份", PUSH, T.STR, "",
          advanced=True),
    state("last_low_battery_alert_at", "上次低电告警", PUSH, T.STR, "",
          advanced=True),
    state("last_violation_alert_at", "上次违规告警", PUSH, T.STR, "",
          advanced=True),
    state("last_stale_alert_at", "上次 stale 告警", PUSH, T.STR, "",
          advanced=True),
)


# ---------------------------------------------------------------------------
# 机器人（私聊）—— 飞书事件订阅
# ---------------------------------------------------------------------------
_BOT_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "push_bot_enabled", "私聊机器人总开关", BOT, T.BOOL, False,
        help="关闭后 /feishu/event 返回 200 但不处理（飞书侧不报错）",
    ),
    cfg(
        "feishu_app_id", "飞书 App ID", BOT, T.STR, "",
        help="开发者后台 → 凭证与基础信息", validate="len:0..64",
        depends_on="push_bot_enabled",
    ),
    cfg(
        "feishu_app_secret", "飞书 App Secret", BOT, T.SECRET, "",
        help="🔒 加密存储", depends_on="push_bot_enabled",
    ),
    cfg(
        "feishu_verification_token", "事件校验 Token", BOT, T.SECRET, "",
        help="🔒 加密存储。事件订阅页的 Verification Token",
        depends_on="push_bot_enabled",
    ),
    cfg(
        "feishu_encrypt_key", "事件加密 Key", BOT, T.SECRET, "",
        help="🔒 加密存储。开启「加密推送」时必填（AES-256-CBC）",
        depends_on="push_bot_enabled",
    ),
    # -- 9 个命令开关（Q16）-----------------------------------------------
    cfg("cmd_remain_enabled", "命令 /状态 /剩余", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_meter_enabled", "命令 /电表", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_today_enabled", "命令 /今日", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_history_enabled", "命令 /历史", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_pay_enabled", "命令 /缴费", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_violations_enabled", "命令 /违规", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
    cfg("cmd_help_enabled", "命令 /帮助", BOT, T.BOOL, True,
        depends_on="push_bot_enabled"),
)


# ---------------------------------------------------------------------------
# 认证（策略参数在 starwatt.auth.secret，这里只暴露只读展示 + public_readonly）
# ---------------------------------------------------------------------------
_AUTH_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "public_readonly", "允许匿名只读访问", AUTH, T.BOOL, False,
        help="开启后未登录也能看首页（Q11）。默认关闭 —— 需登录",
    ),
    cfg(
        "session_hours", "会话有效期（小时）", AUTH, T.INT, 24,
        help="滑动续期，但绝对上限 30 天（L19 修复）", validate="1..720",
    ),
    cfg(
        "lockout_threshold", "失败锁定阈值（次）", AUTH, T.INT, 5,
        validate="1..100",
    ),
    cfg(
        "lockout_minutes", "锁定时长（分钟）", AUTH, T.INT, 15,
        validate="1..1440",
    ),
)


# ---------------------------------------------------------------------------
# 日志（Q20）
# ---------------------------------------------------------------------------
_LOG_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "log_level", "全局日志级别", LOG, T.ENUM, "INFO",
        choices=("TRACE", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR",
                 "CRITICAL"),
    ),
    cfg(
        "log_overrides", "模块级级别覆盖", LOG, T.JSON, {},
        help='如 {"scrape": "DEBUG", "db": "WARNING"}', validate="json",
    ),
    cfg(
        "log_format", "日志格式", LOG, T.ENUM, "text",
        choices=("text", "json"),
    ),
    cfg(
        "log_file_enabled", "写日志文件", LOG, T.BOOL, False,
        help="按天轮转；运行日志可轮转，审计日志不可",
    ),
    cfg(
        "log_file_retention_days", "日志保留天数", LOG, T.INT, 7,
        validate="1..365", depends_on="log_file_enabled",
    ),
)


# ---------------------------------------------------------------------------
# 高级
# ---------------------------------------------------------------------------
_ADVANCED_SETTINGS: tuple[Setting, ...] = (
    cfg(
        "api_internal_token", "内部 API Token", ADVANCED, T.SECRET, "",
        help="🔒 加密存储。供脚本/监控调用只读接口",
    ),
    cfg(
        "backup_enabled", "自动备份", ADVANCED, T.BOOL, True,
        help="每日备份 DB 到数据目录",
    ),
    cfg(
        "backup_retention_days", "备份保留天数", ADVANCED, T.INT, 7,
        validate="1..365", depends_on="backup_enabled",
    ),
    cfg(
        "db_path_display", "数据库路径（只读展示）", ADVANCED, T.STR, "",
        help="由 .env 的 DB_PATH 决定，不可在此修改",
    ),
    # ⚠️ ``schema_version`` 由 starwatt.db.migrations 直接写 meta ——
    # 它**必须**在注册表里声明，否则任何遍历 meta 的代码
    # （``MetaRepo.all()`` 的结果喂给 ``REGISTRY[key]``）都会 KeyError。
    # 这类「代码写的 meta 键没登记」是 RK13 最容易漏的一类。
    state(
        "schema_version", "数据库 schema 版本", ADVANCED, T.INT, 0,
        help="由迁移框架维护，只读", advanced=True,
    ),
)


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
_ALL: tuple[Setting, ...] = (
    _SITE_SETTINGS
    + _SCRAPE_SETTINGS
    + _THRESHOLD_SETTINGS
    + _PUSH_SETTINGS
    + _BOT_SETTINGS
    + _AUTH_SETTINGS
    + _LOG_SETTINGS
    + _ADVANCED_SETTINGS
)

#: ``key -> Setting``（唯一真相）
REGISTRY: dict[str, Setting] = {s.key: s for s in _ALL}

#: 已删除的旧键 —— 出现在库里即视为历史残留，启动时可清理
REMOVED_KEYS: frozenset[str] = frozenset({"admin_password"})


def get(key: str) -> Setting | None:
    """按 key 取声明（不存在返回 ``None``）。"""
    return REGISTRY.get(key)


def keys(kind: Kind | None = None) -> tuple[str, ...]:
    """全部键名；传 ``kind`` 则只返回该类别。"""
    if kind is None:
        return tuple(REGISTRY)
    return tuple(k for k, s in REGISTRY.items() if s.kind is kind)


def by_group() -> dict[str, list[Setting]]:
    """按分组聚合（顺序遵循 :data:`GROUP_ORDER`）。"""
    out: dict[str, list[Setting]] = {g: [] for g in GROUP_ORDER}
    for setting in _ALL:
        out.setdefault(setting.group, []).append(setting)
    return out


def schema() -> dict[str, Any]:
    """给 ``GET /api/admin/config/schema`` 的完整元数据。"""
    return {
        "groups": [
            {"key": g, "label": GROUPS.get(g, g), "settings": [
                s.to_schema() for s in by_group()[g]
            ]}
            for g in GROUP_ORDER
        ],
        "total": len(REGISTRY),
    }


def defaults(kind: Kind | None = None) -> dict[str, Any]:
    """``key -> default``（可只取某一类别）。"""
    return {
        k: s.default
        for k, s in REGISTRY.items()
        if kind is None or s.kind is kind
    }


