"""F1–F5 端点 —— 每个数据源一个 :class:`starwatt.protocols.Endpoint` 实现。

字段映射**全部**从 legacy ``dorm_power.py`` + ``db/_legacy.py`` 提取，
不是推测出来的。

端点真相
========

============================================  ======  ==========
类                                             方法     节流
============================================  ======  ==========
``F1Live``      ``.../dormEmRealRead/getEmRealRead``       POST   每次
``F2Daily``     ``.../dormEmDayElectQuery/getEmDayElectQuery``  POST  24h
``F3Violation`` ``.../dormEmWgQuery/selectWgElect``        POST   1h
``F4RunStatus`` ``.../dormEmRunStatus/getEmRunStatus``     POST   每次
``F5Pay``       ``.../dormEmPayQuery/getEmPayQuery``       POST   24h
============================================  ======  ==========

``F1Live`` 还有一个「一次性回填」兄弟端点
``.../dormEmQuery/selectRecord``（``PATH_SELECT_RECORD``）——
它**不写入 daily_elec**（Round 33c）。

📌 ``F2`` 是 ``daily_elec`` 的**唯一来源**
==========================================

``zong_eq``（累计表码）的取值优先级 —— **这个顺序不能改**：

::

    eebm（F2 当日累计表码） > zong_eq/zongEq（显式给出） > useEq（F1 装机以来累计）

为什么：F2 的 ``eebm`` 与 F1 的 ``useEq`` **同名不同义**。混用会让图表的
``used_today = zong - prev_zong`` 算出 6.70 而不是 30.83。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from starwatt import timeutil
from starwatt.db.coerce import coerce_float, coerce_str
from starwatt.db.models import Record
from starwatt.db.repositories import (
    DailyElecRepo,
    PayRepo,
    RecordRepo,
    RunStatusRepo,
    ViolationRepo,
)
from starwatt.scraper.client import ScrapeError
from starwatt.timeutil import to_stamp

# ⚠️ ``now_cst`` 必须走**模块属性**（``timeutil.now_cst()``），不能
# ``from starwatt.timeutil import now_cst`` —— 后者在 import 时就绑定了函数
# 对象，conftest 的 ``frozen_now``（patch ``starwatt.timeutil.now_cst``）会失效。
# 这是 M1 修过的同一个坑，也是 AST 守卫 R7 的用意。
logger = logging.getLogger("starwatt.scrape")

__all__ = [
    "ENDPOINTS",
    "F1Live",
    "F2Daily",
    "F3Violation",
    "F4RunStatus",
    "F5Pay",
    "PATH_DATA",
    "PATH_DAY_ELECT",
    "PATH_PAY_QUERY",
    "PATH_RUN_STATUS",
    "PATH_SELECT_RECORD",
    "PATH_WG_ELECT",
    "PAY_SEMESTER_START",
    "RoomInfo",
    "as_list",
    "check_status",
    "date_range",
    "discover_room",
    "pay_date_range",
]

# ---------------------------------------------------------------------------
# 路径常量（唯一真相）
# ---------------------------------------------------------------------------
PATH_DATA = "/campus/webchat/dormEmRealRead/getEmRealRead"  # F1
PATH_SELECT_RECORD = "/campus/webchat/dormEmQuery/selectRecord"  # F1 回填
PATH_DAY_ELECT = "/campus/webchat/dormEmDayElectQuery/getEmDayElectQuery"  # F2
PATH_WG_ELECT = "/campus/webchat/dormEmWgQuery/selectWgElect"  # F3
PATH_RUN_STATUS = "/campus/webchat/dormEmRunStatus/getEmRunStatus"  # F4
PATH_PAY_QUERY = "/campus/webchat/dormEmPayQuery/getEmPayQuery"  # F5

#: F5 的日期锚点 —— 学期 / 电表启用日。
#:
#: 📌 **不是 30 天滚动窗口**：今天若在 9 月中旬，「30 天前」只到 8 月中旬，
#: 9/7 的充值记录会**永远查不到**。
PAY_SEMESTER_START = "2026-09-07"

#: 学校返回的成功码（``{"status": "0", ...}``）
_OK_STATUS = {"0", 0}


# ---------------------------------------------------------------------------
# 响应整形
# ---------------------------------------------------------------------------
def as_list(payload: Any) -> list[dict]:
    """把响应体强制成「字典列表」；其它一律当 0 行。

    学校对空结果会返回裸对象（``{"rows": []}``），偶尔甚至返回错误字符串。
    这里逐字保留 legacy ``_as_list`` 的行为 —— 容器名是实测出来的。
    """
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "data", "list", "records"):
            value = payload.get(key)
            if isinstance(value, list):
                return [r for r in value if isinstance(r, dict)]
    return []


def check_status(payload: dict, path: str) -> None:
    """校验学校返回的 ``status`` 字段。

    Raises:
        ScrapeError: ``status`` 非 ``"0"``（学校的失败码）。
    """
    status = payload.get("status")
    if status is not None and status not in _OK_STATUS:
        message = payload.get("message") or payload.get("msg") or ""
        raise ScrapeError(f"{path} 返回失败码 status={status} {message}".strip())


def date_range(days: int, *, today=None) -> tuple[str, str]:
    """``(stime, etime)`` —— ``YYYY-MM-DD``，**朴素 CST**。

    📌 legacy Round 47 修正过这里：原本用 ``datetime.now(UTC)``，清晨时段会
    把窗口整体平移 8 小时（用 CST 的 ``date()`` 才是用户的墙上时钟）。
    """
    end = (today or timeutil.now_cst()).date()
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def pay_date_range(*, today=None) -> tuple[str, str]:
    """F5 的日期范围：``2026-09-07`` → 今天（CST）。"""
    return PAY_SEMESTER_START, (today or timeutil.now_cst()).date().isoformat()


# ---------------------------------------------------------------------------
# A1 —— 房间自动发现
# ---------------------------------------------------------------------------
_INPUT_TAG_RE = re.compile(r"<input\b[^>]*>", re.IGNORECASE)
_INPUT_ATTR_RE = re.compile(
    r"""(?P<key>type|id|value)\s*=\s*["'](?P<val>[^"']*)["']""",
    re.IGNORECASE,
)
#: 📌 Round 34A：房间名在 **value 属性**里（不是 inner text）。
#: 两种属性顺序都接受 —— 学校 HTML 的属性顺序不保证。
_ROOM_NO_RE = re.compile(
    r"<input[^>]*\bid=[\"']roomNo[\"'][^>]*\bvalue=[\"']([^\"']+)[\"']"
    r"|"
    r"<input[^>]*\bvalue=[\"']([^\"']+)[\"'][^>]*\bid=[\"']roomNo[\"']",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RoomInfo:
    """从 ``finduser`` 页面解析出的房间信息。"""

    room_id: str | None
    room_label: str | None

    @property
    def found(self) -> bool:
        return bool(self.room_id)


def _parse_input_attrs(tag: str) -> dict[str, str]:
    return {
        m.group("key").lower(): m.group("val")
        for m in _INPUT_ATTR_RE.finditer(tag)
    }


def discover_room(html: str) -> RoomInfo:
    """从 ``finduser`` 的 HTML 里解析 ``roomId``（隐藏 input）与房间名。

    ``room_id`` 是数据接口要的 UUID；``room_label`` 是给人看的（best-effort，
    拿不到就返回 ``None``，调用方回落通用标题）。
    """
    room_id: str | None = None
    for tag in _INPUT_TAG_RE.finditer(html or ""):
        attrs = _parse_input_attrs(tag.group(0))
        if attrs.get("type", "").lower() != "hidden":
            continue
        if attrs.get("id", "").lower() == "roomid":
            room_id = attrs.get("value") or None

    room_label: str | None = None
    match = _ROOM_NO_RE.search(html or "")
    if match:
        # 两种属性顺序 → 值可能落在组 1 或组 2，取非空的那个
        raw = match.group(1) or match.group(2)
        if raw:
            room_label = raw.strip() or None

    return RoomInfo(room_id=room_id, room_label=room_label)


# ---------------------------------------------------------------------------
# 端点基类
# ---------------------------------------------------------------------------
class _Endpoint:
    """公共部分：``key`` / ``interval_sec`` / ``path`` + 请求辅助。

    子类只实现 :meth:`parse`（响应 → 规范化行）与 :meth:`persist`（落库）。
    """

    key: str = ""
    interval_sec: int | None = None
    path: str = ""

    def _post(self, ctx, **fields: Any) -> dict[str, Any]:
        """POST 到本端点的路径并做 ``status`` 校验。

        Raises:
            ScrapeError: 学校返回失败码，或响应不是对象。
        """
        payload = ctx.post_form(self.path, {"roomId": ctx.room_id, **fields})
        check_status(payload, self.path)
        return payload

    def fetch(self, ctx) -> list[dict[str, Any]]:  # pragma: no cover - 抽象
        raise NotImplementedError

    def persist(self, rows: list[dict[str, Any]]) -> None:  # pragma: no cover
        raise NotImplementedError

    def room_id(self, ctx) -> str:
        """取房间号；空则报错（除 F1 的发现流程外都要求非空）。"""
        room = coerce_str(getattr(ctx, "room_id", None))
        if not room:
            raise ScrapeError(f"{self.key}: 房间号为空，无法请求 {self.path}")
        return room


# ---------------------------------------------------------------------------
# F1 —— 实时电量（每次抓取）
# ---------------------------------------------------------------------------
class F1Live(_Endpoint):
    """``getEmRealRead`` —— 剩余电量。

    📌 只写 ``records``（``ts`` / ``read_time`` / ``remain``）。
    ``useEq`` 是「装机以来累计」，**不写入 daily_elec**（Round 33c）。
    """

    key = "F1"
    interval_sec = None  # 每次抓取
    path = PATH_DATA

    def fetch(self, ctx) -> list[dict[str, Any]]:
        payload = self._post(ctx)
        return [self.parse(payload)]

    @staticmethod
    def parse(payload: dict[str, Any]) -> dict[str, Any]:
        """从响应里取 ``remainEq`` 与 ``dt``。

        ``remainEq`` 缺失时返回 ``remain=None``（不是 0）——
        「不知道」与「没电了」是两回事，卡片配色依赖这个区别。
        """
        remain = coerce_float(
            payload.get("remainEq")
            if payload.get("remainEq") is not None
            else payload.get("remain")
        )
        read_time = coerce_str(payload.get("dt") or payload.get("readTime"))
        return {
            "ts": to_stamp(timeutil.now_cst()),
            "read_time": read_time or "",
            "remain": remain,
        }

    def persist(self, rows: list[dict[str, Any]]) -> None:
        # ⚠️ ``RecordRepo.insert`` 收的是 **Record 对象**，不是 dict ——
        # 用 ``Record.from_row`` 转换（它会忽略多余列并做类型强制）。
        for row in rows:
            RecordRepo.insert(Record.from_row(row))


# ---------------------------------------------------------------------------
# F2 —— 每日用电（24h 节流）—— daily_elec 的唯一来源
# ---------------------------------------------------------------------------
class F2Daily(_Endpoint):
    """``getEmDayElectQuery`` —— 每日用电（30 天窗口）。

    📌 **``daily_elec`` 的唯一来源**（Round 33c）。``F1Live`` 的 ``useEq``
    是「装机以来累计」，与这里的 ``eebm``（「当日累计表码」）同名不同义，
    混写会让日用量算错。
    """

    key = "F2"
    interval_sec = 24 * 3600
    path = PATH_DAY_ELECT

    def fetch(self, ctx) -> list[dict[str, Any]]:
        room = self.room_id(ctx)
        stime, etime = date_range(30)
        payload = self._post(ctx, stime=stime, etime=etime, page=1)
        return self.parse(as_list(payload), room)

    @staticmethod
    def parse(rows: list[dict], room_id: str) -> list[dict[str, Any]]:
        """规范化每一行；``roomId`` 注入行内（``persist`` 签名只有 rows）。"""
        out: list[dict[str, Any]] = []
        for raw in rows:
            dt = coerce_str(raw.get("dt"))
            if not dt:
                continue  # 没有日期 = 无法做主键，丢弃
            out.append({
                "roomId": room_id,
                "dt": dt,
                "total_eq": coerce_float(
                    raw.get("total_eq") if raw.get("total_eq") is not None
                    else raw.get("totalEq")
                ),
                "esbm": coerce_float(raw.get("esbm")),
                "eebm": coerce_float(raw.get("eebm")),
                "zong_eq": _cumulative_zong(raw),
            })
        return out

    def persist(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("roomId") or ""), []).append(row)
        for room, items in grouped.items():
            if room:
                DailyElecRepo.upsert_many(room, items)


def _cumulative_zong(raw: dict) -> float | None:
    """累计表码（``zong_eq``）的取值优先级 —— **顺序不能改**。

    ::

        eebm（F2 当日累计表码） > zong_eq/zongEq（显式） > useEq（F1 装机以来累计）

    📌 Round 33c：F2 **不返回** ``zong_eq``。若留空，图表的
    ``used_today = zong - prev_zong`` 会拿 F1 的陈旧行去算，得出 6.70
    而不是 30.83。
    """
    zong = coerce_float(raw.get("eebm"))
    if zong is None:
        zong = coerce_float(raw.get("zong_eq"))
    if zong is None:
        zong = coerce_float(raw.get("zongEq"))
    if zong is None:
        zong = coerce_float(raw.get("useEq"))
    return zong


# ---------------------------------------------------------------------------
# F3 —— 违规记录（1h 节流）
# ---------------------------------------------------------------------------
class F3Violation(_Endpoint):
    """``selectWgElect`` —— 大功率违规记录。

    📌 ``wg_power`` 的单位是 **kW（功率）**，不是度（电量）。
    卡片上写「kW」不是笔误。
    """

    key = "F3"
    interval_sec = 3600
    path = PATH_WG_ELECT

    def fetch(self, ctx) -> list[dict[str, Any]]:
        room = self.room_id(ctx)
        stime, etime = date_range(30)
        payload = self._post(ctx, stime=stime, etime=etime, page=1)
        return self.parse(as_list(payload), room)

    @staticmethod
    def parse(rows: list[dict], room_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in rows:
            dt = coerce_str(raw.get("dt"))
            reason = coerce_str(
                raw.get("wg_reason") or raw.get("wgReasonLabel")
            )
            if not dt or not reason:
                continue  # 主键是 (roomId, dt, wg_reason)，两者缺一不可
            out.append({
                "roomId": room_id,
                "dt": dt,
                "wg_reason": reason,
                "wg_power": coerce_float(
                    raw.get("wg_power") if raw.get("wg_power") is not None
                    else raw.get("wgPower")
                ),
            })
        return out

    def persist(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("roomId") or ""), []).append(row)
        for room, items in grouped.items():
            if room:
                ViolationRepo.upsert_many(room, items)


# ---------------------------------------------------------------------------
# F4 —— 电表状态（每次抓取）
# ---------------------------------------------------------------------------
#: 视为「在线」的状态值 —— 由配置项 ``offline_status_values`` 覆盖。
#: 📌 不在列表里的值（**含字段缺失**）一律判定为离线（B6）。
DEFAULT_ONLINE_VALUES = ("在线", "正常", "通讯正常")


class F4RunStatus(_Endpoint):
    """``getEmRunStatus`` —— 电表快照（电压 / 电流 / 功率 / 状态）。

    与 F1–F3 不同，它返回**裸对象**而不是列表；字段缺失是正常的
    （legacy 明确说「永远不因缺字段报错，行就是部分填充的」）。
    """

    key = "F4"
    interval_sec = None  # 每次抓取
    path = PATH_RUN_STATUS

    def fetch(self, ctx) -> list[dict[str, Any]]:
        room = self.room_id(ctx)
        payload = self._post(ctx)  # 只带 roomId，没有日期范围
        return [{"roomId": room, "data": payload}]

    def persist(self, rows: list[dict[str, Any]]) -> None:
        for row in rows:
            room = coerce_str(row.get("roomId"))
            data = row.get("data")
            if room and isinstance(data, dict):
                RunStatusRepo.upsert(room, data)


def is_meter_online(
    run_status: dict[str, Any] | None,
    online_values: tuple[str, ...] = DEFAULT_ONLINE_VALUES,
) -> bool:
    """电表是否在线。

    📌 B6：``runStatus`` **不在** ``online_values`` 里、或字段缺失，
    一律算**离线** —— 宁可误报也不能静默失败（旧代码踩过的坑）。
    """
    if not isinstance(run_status, dict):
        return False
    raw = coerce_str(
        run_status.get("runStatus") or run_status.get("run_status")
    )
    if raw is None:
        return False
    return raw in online_values


# ---------------------------------------------------------------------------
# F5 —— 缴费历史（24h 节流，日期锚定学期起点）
# ---------------------------------------------------------------------------
class F5Pay(_Endpoint):
    """``getEmPayQuery`` —— 缴费 / 退费记录。

    📌 两个 legacy 修正都保留：

    * ``feeType=0``（全部）。旧默认 ``-1`` 是**只查退费**，把每一条充值
      都过滤掉了 —— 这就是 F5 长期为空的原因。
    * 日期**锚定学期起点** ``2026-09-07``，不是 30 天滚动窗口。
    """

    key = "F5"
    interval_sec = 24 * 3600
    path = PATH_PAY_QUERY

    #: 0 = 全部（充值 + 退费），1 = 仅缴费，-1 = 仅退费
    fee_type = 0

    def fetch(self, ctx) -> list[dict[str, Any]]:
        room = self.room_id(ctx)
        stime, etime = pay_date_range()
        payload = self._post(ctx, stime=stime, etime=etime, feeType=self.fee_type)
        return self.parse(as_list(payload), room)

    @staticmethod
    def parse(rows: list[dict], room_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for raw in rows:
            dt = coerce_str(raw.get("dt"))
            pay_type = coerce_str(
                raw.get("pay_type") or raw.get("payTypeLabel")
            )
            fee_type = coerce_str(
                raw.get("fee_type") or raw.get("feeTypeLabel")
            )
            if not dt or not pay_type or not fee_type:
                continue  # 主键是 (roomId, dt, pay_type, fee_type)
            out.append({
                "roomId": room_id,
                "dt": dt,
                "pay_type": pay_type,
                "fee_type": fee_type,
                "money": coerce_float(raw.get("money")),
            })
        return out

    def persist(self, rows: list[dict[str, Any]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(str(row.get("roomId") or ""), []).append(row)
        for room, items in grouped.items():
            if room:
                PayRepo.upsert_many(room, items)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
#: 全部端点，按**执行顺序**排列（F1 先 —— 它决定 room_id 是否可用）。
ENDPOINTS: tuple[_Endpoint, ...] = (
    F1Live(),
    F4RunStatus(),
    F2Daily(),
    F3Violation(),
    F5Pay(),
)


