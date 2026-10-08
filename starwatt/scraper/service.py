"""抓取编排 —— **节流 / 回填 / stale 降级**（M2）。

一句话
======

``endpoints.py`` 只负责「怎么请求 + 怎么解析」，本模块负责
**什么时候跑、跑完写不写状态、失败怎么降级**。

三件事
======

**1. 节流（cadence gate）**

F1 / F4 每次抓取都跑；F2 / F3 / F5 由 ``meta`` 里的时间戳门控：

======================  ==========================  ==============================
端点                     状态键（meta）               间隔配置项（注册表）
======================  ==========================  ==============================
``F2`` ``getEmDayElectQuery``   ``last_daily_elect_at``      ``scrape_f2_throttle_hours``
``F3`` ``selectWgElect``        ``last_violation_check_at``  ``scrape_f3_throttle_hours``
``F5`` ``getEmPayQuery``        ``last_pay_at``              ``scrape_f5_throttle_hours``
======================  ==========================  ==============================

📌 间隔以**注册表配置**为准（Q15：WebUI 可改），``Endpoint.interval_sec`` 只作为
契约快照里的默认值（见 ``tests/unit/test_scraper.py::TestEndpointRegistry``）。

📌 **失败也打点**（沿用 legacy 冻结行为）：某端点抓取失败后仍然写状态键，
否则 10 分钟的调度周期会对着同一所学校接口反复重试。代价是「失败要等一个
节流周期才会重试」—— 这是有意的取舍，不是 bug。**唯一的例外是首次回填**
（见下）：回填失败**不**打点，下一个周期立刻重试。

**2. 回填（一次性历史导入）**

首次运行（``backfill_done`` 未置位）时，绕过节流立刻抓 F2 + F5，
**全部成功才**置 ``backfill_done=True``（Round 39 修复：部分失败时保持未置位，
下次抓取整批重试 —— 否则 ``daily_elec`` 会永久缺数据）。

⚠️ 与 legacy 的差异（Q22 + Round 33c）：legacy 的回填还调用了
``/dormEmQuery/selectRecord``（F1-backfill），但它把 ``useEq``（装机以来累计）
写进 ``daily_elec``，与 F2 的 ``eebm``（当日累计）语义冲突。重写**删除**这一步：
**``daily_elec`` 的唯一来源就是 F2**。

**3. stale 降级**

``last_scrape_status`` 的四个取值（注册表已冻结）：

======  ==================================================================
取值     含义
======  ==================================================================
``never``  从未成功抓取过
``ok``     本次成功
``stale``  **降级**：本次失败、或距上次成功已超过 ``stale_hours`` ——
          继续用旧数据（图表不清零），并把原因记进日志
``error``  抓取**无法开始**（未配置 openid / 无法解析 roomId）—— 需要用户干预
======  ==================================================================

``run_once()`` 只在「无法开始」时抛 :class:`ScrapeError`；**数据源失败不抛**
—— 而是返回 ``ok=False`` 的报告。进程内调度（RK4）没有 exit code 可用，
失败必须靠状态键 + 日志暴露给 M4 的 API 与 M3 的 stale 推送。

📌 与 legacy 的另一个差异：legacy 的 ``datetime.utcnow()`` 让节流窗口与
``stale_hours`` 判定整体偏 8 小时（L22 同类缺陷）。重写统一走
:func:`starwatt.timeutil.now_cst`（AST 守卫 R7 强制）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from starwatt import timeutil
from starwatt.config_registry import get_bool, get_int, get_str, set_state
from starwatt.db.connection import init as db_init
from starwatt.scraper.client import (
    DEFAULT_TIMEOUT_SEC,
    FINDUSER_PATH,
    SchoolClient,
    ScrapeError,
)
from starwatt.scraper.endpoints import ENDPOINTS, discover_room

logger = logging.getLogger("starwatt.scrape")

__all__ = [
    "BACKFILL_KEYS",
    "STATE_BACKFILL_DONE",
    "STATE_LAST_ROOM_ID",
    "STATE_LAST_SCRAPE_AT",
    "STATE_LAST_SCRAPE_STATUS",
    "STATUS_ERROR",
    "STATUS_NEVER",
    "STATUS_OK",
    "STATUS_STALE",
    "THROTTLES",
    "ScrapeContext",
    "ScrapeReport",
    "build_client",
    "is_due",
    "is_stale",
    "run_once",
    "stale_gap_seconds",
    "throttle_seconds",
]

# ---------------------------------------------------------------------------
# meta 状态键（键名与 legacy 完全一致 —— Q9/Q10 零迁移）
# ---------------------------------------------------------------------------
STATE_BACKFILL_DONE = "backfill_done"
STATE_LAST_ROOM_ID = "last_room_id"
STATE_LAST_SCRAPE_AT = "last_scrape_at"
STATE_LAST_SCRAPE_STATUS = "last_scrape_status"

#: ``last_scrape_status`` 的取值（与注册表的 ``choices`` 一致）
STATUS_OK = "ok"
STATUS_STALE = "stale"
STATUS_ERROR = "error"
STATUS_NEVER = "never"

#: 节流端点 → ``(间隔配置键, 状态键)``
THROTTLES: dict[str, tuple[str, str]] = {
    "F2": ("scrape_f2_throttle_hours", "last_daily_elect_at"),
    "F3": ("scrape_f3_throttle_hours", "last_violation_check_at"),
    "F5": ("scrape_f5_throttle_hours", "last_pay_at"),
}

#: 首次回填必须**全部成功**的端点。F2 是 ``daily_elec`` 的唯一来源，
#: 缺了它 30 天图表就是空的；F5 决定缴费历史卡片。
BACKFILL_KEYS: tuple[str, ...] = ("F2", "F5")

#: ``stale_hours`` 配置读不到时的兜底（注册表默认值也是 2）
DEFAULT_STALE_HOURS = 2

#: 节流小时数配置读不到时的兜底
DEFAULT_THROTTLE_HOURS = 24

# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class ScrapeReport:
    """一次抓取周期的结果（调度器日志 / M4 API / 集成测试共用）。

    ``ok`` 表示**核心数据（F1 剩余电量）**是否拿到 —— 它是仪表盘的主数据，
    拿不到就等于这次抓取白跑，即使 F2/F3 成功。
    """

    started_at: str = ""
    room_id: str = ""
    room_label: str | None = None
    ok: bool = False
    #: 真正执行并落库的端点 key（``fetch_only`` 时为「只抓取」的端点）
    ran: list[str] = field(default_factory=list)
    #: 因节流而跳过的端点 key
    skipped: list[str] = field(default_factory=list)
    #: ``{端点 key: 错误}`` —— **已脱敏**（不含 openid）
    errors: dict[str, str] = field(default_factory=dict)
    #: ``{端点 key: 行数}``
    rows: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """一行日志用摘要（**不含** openid）。"""
        tail = self.room_id[-4:] if self.room_id else "????"
        parts = [f"room=***{tail}", f"执行={'/'.join(self.ran) or '-'}"]
        if self.skipped:
            parts.append(f"节流跳过={'/'.join(self.skipped)}")
        if self.errors:
            detail = "; ".join(f"{k}: {v}" for k, v in self.errors.items())
            parts.append(f"失败=[{detail}]")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# FetchContext 实现 —— 把 openid 注入 URL
# ---------------------------------------------------------------------------
@dataclass
class ScrapeContext:
    """:class:`starwatt.protocols.FetchContext` 的实现。

    ``SchoolClient`` 只管「怎么发 HTTP」，不认识 openid；学校接口却要求
    ``?openid=`` 查询参数（legacy ``_build_url``）。这一层就是那条缝：
    端点拿到的是「路径」，本类负责把凭据拼上去。

    ⚠️ 拼好的 URL **含凭据**，任何日志 / 异常消息都要先过 :func:`_sanitize`。
    """

    client: Any
    openid: str
    room_id: str

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(self.url(path), **kwargs)

    def post_form(self, path: str, data: dict[str, Any]) -> dict[str, Any]:
        return self.client.post_form(self.url(path), data)

    def url(self, path: str) -> str:
        """``path`` + ``?openid=...``（openid 做百分号编码，防止 ``&``/``#`` 破坏 URL）。"""
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}openid={quote(self.openid, safe='')}"


def _sanitize(text: object, openid: str) -> str:
    """把凭据从错误消息里抹掉（Q20：secret 不得进日志 / API）。

    ``SchoolClient`` 的异常消息里带的是**完整请求路径**（含 ``openid=``），
    所以这里按值替换 —— 与 ``logging_setup.RedactingFilter`` 同一套思路，
    但对象是「要返回给调用方的数据结构」，不是日志记录。
    """
    raw = str(text)
    if openid:
        raw = raw.replace(openid, "***")
    return raw


# ---------------------------------------------------------------------------
# 节流 / stale 判定
# ---------------------------------------------------------------------------
def is_due(state_key: str, interval_sec: int | None, *, now=None) -> bool:
    """节流门是否打开（``True`` = 该跑了）。

    * 从未运行 → ``True``
    * 时间戳**无法解析**（人工改坏 / 旧格式）→ ``True``
      —— 宁可多抓一次，也不能因为一个坏值让数据永远停在某天
    * ``interval_sec`` 为 ``None`` / ``<= 0`` → ``True``（每次抓取）
    """
    if interval_sec is None or interval_sec <= 0:
        return True
    last = timeutil.parse_stamp(get_str(state_key))
    if last is None:
        return True
    moment = now or timeutil.now_cst()
    return moment - last >= timedelta(seconds=interval_sec)


def throttle_seconds(key: str) -> int | None:
    """端点的节流间隔（秒）；``None`` = 每次抓取。

    📌 以注册表配置为准（Q15）。小时数下限 1 小时 —— 配置项自身有
    ``1..720`` 校验，这里只做兜底。
    """
    spec = THROTTLES.get(key)
    if spec is None:
        return None
    hours = get_int(spec[0], DEFAULT_THROTTLE_HOURS)
    return max(1, hours) * 3600


def stale_gap_seconds(*, now=None) -> float | None:
    """距上次**成功**抓取的秒数；从未成功过则 ``None``。"""
    last = timeutil.parse_stamp(get_str(STATE_LAST_SCRAPE_AT))
    if last is None:
        return None
    return ((now or timeutil.now_cst()) - last).total_seconds()


def is_stale(*, now=None) -> bool:
    """上次成功抓取是否已超过 ``stale_hours``。"""
    gap = stale_gap_seconds(now=now)
    if gap is None:
        return False
    return gap >= get_int("stale_hours", DEFAULT_STALE_HOURS) * 3600


def _begin_status(moment, *, fetch_only: bool) -> None:
    """抓取开始前更新 ``last_scrape_status``（``never`` / ``stale`` 降级）。

    只在这两种情况下写：

    * 从未成功过 → ``never``（让 UI 能区分「没配好」与「数据旧了」）
    * 已陈旧 → ``stale``（**降级**：继续用旧数据，记 WARNING）

    正常情况（刚抓过、距上次成功很近）**不写** —— 保留上一次的真实结果。
    """
    if fetch_only:
        return
    if not get_str(STATE_LAST_SCRAPE_AT):
        set_state(STATE_LAST_SCRAPE_STATUS, STATUS_NEVER)
        return
    if is_stale(now=moment):
        hours = get_int("stale_hours", DEFAULT_STALE_HOURS)
        logger.warning(
            "上次成功抓取已超过 %d 小时 —— 降级为 stale，继续使用旧数据", hours
        )
        set_state(STATE_LAST_SCRAPE_STATUS, STATUS_STALE)


def build_client() -> SchoolClient:
    """按注册表配置构造 HTTP 客户端（每次抓取一个，用完即关）。"""
    return SchoolClient(
        get_str("dorm_base_url"),
        user_agent=get_str("scrape_user_agent"),
        timeout_sec=get_int("scrape_timeout_sec", DEFAULT_TIMEOUT_SEC),
        retry_count=get_int("scrape_retry_count", 3),
        allow_private_hosts=get_bool("allow_private_hosts", False),
    )


def _notify_hooks() -> dict[str, Any]:
    """解析 M3 推送钩子（延迟导入，避免抓取层强依赖通知层）。

    通知层不可用（缺 Pillow / pycryptodome）时返回空 dict —— **静默降级**：
    抓取本身不该因为通知依赖缺失而失败（K7）。
    """
    try:
        from starwatt.notify import policies
    except Exception:  # noqa: BLE001
        logger.debug("通知层不可用，本次抓取不推送")
        return {}
    return {
        "stale": policies.push_stale_if_due,
        "low_battery": policies.push_low_battery_if_due,
        "violations": policies.push_violations_if_due,
        "offline": policies.push_offline_if_due,
    }


def _safe_push(hook, *args) -> None:
    """跑一个推送钩子 —— **任何异常都不得影响抓取主流程**（K7）。

    ``hook`` 为 ``None``（``fetch_only`` 或通知层不可用）时什么都不做。
    """
    if hook is None:
        return
    try:
        hook(*args)
    except Exception:  # noqa: BLE001
        logger.warning(
            "推送钩子 %s 失败（已忽略）", getattr(hook, "__name__", hook), exc_info=True
        )


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------
def _fail_status(*, fetch_only: bool) -> None:
    """「无法开始」时把状态标成 ``error``（配置问题，需要用户干预）。"""
    if not fetch_only:
        set_state(STATE_LAST_SCRAPE_STATUS, STATUS_ERROR)


def _fetch(endpoint, ctx: ScrapeContext, report: ScrapeReport, *, openid: str):
    """抓取一个端点；失败返回 ``None`` 并记进报告（**不抛**）。"""
    try:
        return endpoint.fetch(ctx)
    except Exception as exc:  # noqa: BLE001 —— 单个端点失败不该打断整轮
        message = _sanitize(f"{type(exc).__name__}: {exc}", openid)
        report.errors[endpoint.key] = message
        logger.warning("%s 抓取失败：%s", endpoint.key, message)
        return None


def _persist(endpoint, rows, report: ScrapeReport, *, openid: str) -> bool:
    """落库一个端点；失败返回 ``False``（同样记进报告）。"""
    try:
        endpoint.persist(rows)
        return True
    except Exception as exc:  # noqa: BLE001
        message = _sanitize(f"{type(exc).__name__}: {exc}", openid)
        report.errors[endpoint.key] = message
        logger.warning("%s 落库失败：%s", endpoint.key, message)
        return False


def _run_endpoint(
    endpoint,
    ctx: ScrapeContext,
    report: ScrapeReport,
    moment,
    *,
    fetch_only: bool,
    forced: bool,
) -> bool:
    """跑**一个**端点：节流 → 抓取 → 落库 → 打点。

    Returns:
        ``True`` = 成功（含「被节流跳过」）；``False`` = 失败。
    """
    key = endpoint.key
    spec = THROTTLES.get(key)
    interval = throttle_seconds(key)

    if spec is not None and not forced and not is_due(spec[1], interval, now=moment):
        report.skipped.append(key)
        logger.debug("%s 未到节流周期（%ss），跳过", key, interval)
        return True

    rows = _fetch(endpoint, ctx, report, openid=ctx.openid)
    if rows is None:
        if forced:
            return False  # 回填失败 → 不打点，下轮整批重试
        if spec is not None and not fetch_only:
            # legacy 冻结行为：普通节流端点失败也打点，避免打爆学校接口
            set_state(spec[1], timeutil.to_stamp(moment))
        return False

    if fetch_only:
        report.ran.append(key)
        report.rows[key] = len(rows)
        return True

    if not _persist(endpoint, rows, report, openid=ctx.openid):
        return False

    if spec is not None:
        set_state(spec[1], timeutil.to_stamp(moment))
    report.ran.append(key)
    report.rows[key] = len(rows)
    return True


def _scrape_all(
    ctx: ScrapeContext,
    report: ScrapeReport,
    moment,
    *,
    fetch_only: bool,
    endpoints=None,
    hooks: dict | None = None,
) -> None:
    """跑全部端点，然后推送告警，最后收尾。"""
    backfill_pending = not fetch_only and not get_bool(STATE_BACKFILL_DONE)
    if backfill_pending:
        logger.info("首次运行：开始一次性历史回填（%s）", "、".join(BACKFILL_KEYS))

    backfill_ok = True
    for endpoint in endpoints if endpoints is not None else ENDPOINTS:
        forced = backfill_pending and endpoint.key in BACKFILL_KEYS  # 回填期强制抓取
        ok = _run_endpoint(
            endpoint, ctx, report, moment, fetch_only=fetch_only, forced=forced
        )
        if forced and not ok:
            backfill_ok = False

    # ---- M3 推送（顺序与 legacy 一致：低电 → 违规 → 离线）----
    if hooks:
        _safe_push(hooks.get("low_battery"))
        _safe_push(hooks.get("violations"), ctx.room_id)
        _safe_push(hooks.get("offline"), ctx.room_id)

    _finish(
        report,
        moment,
        fetch_only=fetch_only,
        backfill_pending=backfill_pending,
        backfill_ok=backfill_ok,
    )


def _finish(
    report: ScrapeReport,
    moment,
    *,
    fetch_only: bool,
    backfill_pending: bool,
    backfill_ok: bool,
) -> None:
    """收尾：回填标记 + ``ok``/``stale`` 状态 + 一行日志。"""
    # F1 必须**真的跑成功**：失败会进 ``errors`` 而不进 ``ran``
    report.ok = "F1" in report.ran

    if fetch_only:
        logger.info("抓取（fetch_only，未落库）：%s", report.summary())
        return

    if backfill_pending:
        if backfill_ok:
            set_state(STATE_BACKFILL_DONE, True)
            logger.info("历史回填完成（%s）", "、".join(BACKFILL_KEYS))
        else:
            logger.warning("历史回填未完成（部分端点失败），下次抓取会整批重试")

    if report.ok:
        set_state(STATE_LAST_SCRAPE_AT, timeutil.to_stamp(moment))
        set_state(STATE_LAST_SCRAPE_STATUS, STATUS_OK)
        logger.info("抓取完成：%s", report.summary())
    else:
        # stale 降级：核心数据没更新，但旧数据继续可用（不清零、不向用户报错）
        set_state(STATE_LAST_SCRAPE_STATUS, STATUS_STALE)
        logger.warning("核心数据（F1）抓取失败 —— 降级为 stale：%s", report.summary())


def run_once(
    *,
    fetch_only: bool = False,
    client=None,
    endpoints=None,
    now=None,
) -> ScrapeReport:
    """跑一轮完整抓取。

    Args:
        fetch_only: 只抓取、**不落库也不写状态**（RK3 的冒烟模式：
            用来验证「能连上学校接口」而不产生任何副作用）。
        client: 注入的 HTTP 客户端（测试用；为 ``None`` 时按注册表构造）。
        endpoints: 注入的端点列表（测试用；为 ``None`` 时用
            :data:`starwatt.scraper.endpoints.ENDPOINTS`）。
        now: 注入的「当前时刻」（测试用；默认 :func:`timeutil.now_cst`）。

    Returns:
        :class:`ScrapeReport`；``ok=False`` 表示核心数据（F1）没拿到
        —— 此时状态键已被置为 ``stale``（降级），旧数据继续可用。

    Raises:
        ScrapeError: **无法开始**（未配置 openid / 无法解析 roomId）。
            数据源层面的失败**不会**抛异常，见模块 docstring。
    """
    moment = now or timeutil.now_cst()
    db_init()

    report = ScrapeReport(started_at=timeutil.to_stamp(moment))

    openid = get_str("dorm_openid").strip()
    if not openid:
        _fail_status(fetch_only=fetch_only)
        raise ScrapeError("未配置 dorm_openid（配置中心 → 数据采集 → 登录凭据 openid）")

    owns_client = client is None
    client = client or build_client()
    try:
        room_id = get_str("dorm_room_id").strip()
        room_label: str | None = None
        ctx = ScrapeContext(client=client, openid=openid, room_id=room_id)

        if not room_id:
            # A1 房间自动发现（legacy 的 finduser 页面解析）
            info = discover_room(ctx.get(FINDUSER_PATH))
            room_id, room_label = info.room_id, info.room_label
            if not room_id:
                _fail_status(fetch_only=fetch_only)
                raise ScrapeError(
                    "无法从 finduser 页面解析 roomId（请检查 openid，"
                    "或在配置里直接填写 dorm_room_id）"
                )
            ctx.room_id = room_id

        report.room_id = room_id
        report.room_label = room_label
        if not fetch_only:
            set_state(STATE_LAST_ROOM_ID, room_id)

        _begin_status(moment, fetch_only=fetch_only)

        # M3：stale 检查在任何抓取工作**之前**（legacy 顺序）
        hooks = {} if fetch_only else _notify_hooks()
        _safe_push(hooks.get("stale"))

        _scrape_all(
            ctx,
            report,
            moment,
            fetch_only=fetch_only,
            endpoints=endpoints,
            hooks=hooks,
        )
        return report
    finally:
        if owns_client:
            client.close()
