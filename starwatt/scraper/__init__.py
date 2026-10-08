"""``starwatt.scraper`` —— 学校数据采集层（M2 ✅）。

M2 进度
=======

* ✅ ``ssrf.py`` —— **4 层** SSRF 防护（L1 scheme / L2 主机名 / L3 解析后 IP / L4 在 client）
* ✅ ``client.py`` —— HTTP 客户端：请求伪装 + 重试退避 + 重定向逐跳复检 + 体积上限
* ✅ ``endpoints.py`` —— F1–F5（实现 :class:`starwatt.protocols.Endpoint`）
* ✅ ``service.py`` —— 编排：节流 / 回填 / stale 降级（``run_once``）
* ✅ ``starwatt/scheduler.py`` —— APScheduler（Q14 + B3 ``post_fork``）

接口真相（从 develop 分支的 ``dorm_power.py`` 提取，**不可臆造**）
================================================================

===========================================  ======  =========
路径                                          方法    节流
===========================================  ======  =========
``/campus/webchat/dormEmRealRead/finduser``   GET     —
``.../dormEmRealRead/getEmRealRead``          POST    每次
``.../dormEmQuery/selectRecord``              POST    **已废弃**（见下）
``.../dormEmDayElectQuery/getEmDayElectQuery`` POST   24h
``.../dormEmWgQuery/selectWgElect``           POST    1h
``.../dormEmRunStatus/getEmRunStatus``        POST    每次
``.../dormEmPayQuery/getEmPayQuery``          POST    24h
===========================================  ======  =========

URL 形态：``{base_url}{path}?openid={openid}`` —— 由
:class:`starwatt.scraper.service.ScrapeContext` 拼接（``client.FINDUSER_PATH``
与 ``endpoints`` 的路径常量是唯一真相）。

📌 **F1 的行不再写入 ``daily_elec``**（Round 33c）：F1 的 ``useEq`` 是
「装机以来累计」，与 F2 的 ``eebm``（「当日累计」）语义不兼容，混写会让
日用量出现 6.70 vs 30.83 这类错误。**F2 是 ``daily_elec`` 的唯一来源。**

📌 legacy 的 ``selectRecord`` 回填**已删除**（Q22 + Round 33c）：它只把
``useEq`` 写进 ``daily_elec``，与上面这条约束直接冲突；回填现在只跑 F2 + F5。
"""
from __future__ import annotations

from starwatt.scraper.client import (
    FINDUSER_PATH,
    MAX_BYTES,
    MAX_REDIRECTS,
    HttpResult,
    SchoolClient,
    ScrapeError,
)
from starwatt.scraper.endpoints import (
    ENDPOINTS,
    F1Live,
    F2Daily,
    F3Violation,
    F4RunStatus,
    F5Pay,
    RoomInfo,
    as_list,
    discover_room,
    is_meter_online,
)
from starwatt.scraper.service import (
    BACKFILL_KEYS,
    THROTTLES,
    ScrapeContext,
    ScrapeReport,
    build_client,
    is_due,
    is_stale,
    run_once,
    throttle_seconds,
)
from starwatt.scraper.ssrf import SsrfError, assert_public_url

__all__ = [
    "BACKFILL_KEYS",
    "ENDPOINTS",
    "FINDUSER_PATH",
    "F1Live",
    "F2Daily",
    "F3Violation",
    "F4RunStatus",
    "F5Pay",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "THROTTLES",
    "HttpResult",
    "RoomInfo",
    "SchoolClient",
    "ScrapeContext",
    "ScrapeError",
    "ScrapeReport",
    "SsrfError",
    "as_list",
    "assert_public_url",
    "build_client",
    "discover_room",
    "is_due",
    "is_meter_online",
    "is_stale",
    "run_once",
    "throttle_seconds",
]

