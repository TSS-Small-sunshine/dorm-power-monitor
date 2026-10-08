"""飞书卡片构建（L1 / L2 / L4 + 颜色分区）。

契约
====

``build_summary_card`` / ``build_offline_card`` / ``template_for_remain`` 的
输出**必须与** ``tests/regression/fixtures/cards.json`` 逐字节一致 ——
那是在 legacy 代码上真实运行产出的快照（M0 步骤 0.4），
``tests/unit/test_notify_cards.py`` 会逐项比对。

三条从 legacy 继承的**细节**（改了就与快照不符，也会让用户看到变化）：

1. 缺失字段渲染成 ``—``（不是空、不是 ``0``）
2. ``dt`` 缺失时**整行**「🕐 抄表时间」消失（不是显示 ``—``）
3. 离线卡只认**驼峰键**（``runStatus`` / ``workStatus`` / ``stopReason`` /
   ``updateDt``）—— 这是 legacy 的行为，快照里三个用例全部渲染 ``—`` 正是
   因为生成器喂的是下划线键。重写**不**偷偷兼容下划线，否则就与快照不符。

与 legacy 的两处有意差异
========================

* 阈值（红 / 橙 / 蓝）从**配置注册表**读（``threshold_red`` 等，Q15 可改），
  默认值与 legacy 常量一致（30 / 80 / 200）
* 页脚文案统一为 :data:`FOOTER_NOTE`（品牌从 ``dorm-power-monitor`` 改为
  ``StarWatt 星瓦``，见 ``docs/NAMING.md``）；卡片正文里的数字格式**不变**

颜色分区（严格小于 —— 与快照的边界向量一致）
===========================================

======  ================  ======
条件    颜色               语义
======  ================  ======
缺失    ``blue``           数据没拿到，不吓人
< 红    ``red``            危险
< 橙    ``orange``         偏紧
< 蓝    ``blue``           正常
≥ 蓝    ``green``          充足
======  ================  ======
"""
from __future__ import annotations

import logging
from typing import Any

from starwatt import timeutil
from starwatt.config_registry import get_float
from starwatt.db.coerce import coerce_float, coerce_str

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "DEFAULT_RED_BELOW",
    "DEFAULT_ORANGE_BELOW",
    "DEFAULT_BLUE_BELOW",
    "FOOTER_NOTE",
    "build_low_battery_card",
    "build_offline_card",
    "build_stale_card",
    "build_summary_card",
    "build_violation_card",
    "format_wg_power",
    "template_for_remain",
]

#: legacy 常量（与注册表默认值一致）—— 注册表读不到时兜底
DEFAULT_RED_BELOW = 30.0
DEFAULT_ORANGE_BELOW = 80.0
DEFAULT_BLUE_BELOW = 200.0

#: 卡片页脚（品牌改名，见 docs/NAMING.md）
FOOTER_NOTE = "由 StarWatt 星瓦 推送"

#: 违规卡最多列出几条（其余折叠成「另有 N 条」）
_VIOLATION_MAX_LINES = 8


def _threshold(key: str, default: float) -> float:
    """读阈值配置；读不到（首次启动、meta 表未建）回落默认值。"""
    try:
        return float(get_float(key, default))
    except Exception:  # noqa: BLE001 —— 卡片构建绝不该因为读配置失败而崩
        return default


def template_for_remain(remain: float | None) -> str:
    """按剩余电量选 header 颜色（边界严格小于，与快照一致）。"""
    if remain is None:
        return "blue"
    if remain < _threshold("threshold_red", DEFAULT_RED_BELOW):
        return "red"
    if remain < _threshold("threshold_orange", DEFAULT_ORANGE_BELOW):
        return "orange"
    if remain < _threshold("threshold_blue", DEFAULT_BLUE_BELOW):
        return "blue"
    return "green"


def _note_element(note: str) -> dict:
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": note}]}


def _div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _kv_cell(icon: str, label: str, body_text: str) -> dict:
    """两列布局里的一个单元格（``icon **标题**`` 换行 + 值）。"""
    return _div(f"{icon} **{label}**\n{body_text}")


def _column_set(left: dict, right: dict) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "stretch",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [left]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [right]},
        ],
    }


# ---------------------------------------------------------------------------
# L2 —— 常规摘要卡
# ---------------------------------------------------------------------------
def build_summary_card(
    body: dict[str, Any],
    room_label: str | None,
    note: str = FOOTER_NOTE,
    stale: bool = False,
    *,
    title_prefix: str | None = None,
    header_template: str | None = None,
    top_of_hour: bool | None = None,
) -> dict:
    """构建 L2 摘要卡（与 ``cards.json`` 快照一致）。

    Args:
        body: 门户原始 F1 响应（``remainEq`` / ``freeEq`` / ``rechargeEq`` /
            ``useEq`` / ``totalEq`` / ``remainWqMoney`` / ``dt`` / ``eqprice``）。
        room_label: 房间名；为空则用通用标题。
        note: 页脚。
        stale: ``True`` 时加「⚠ 数据陈旧」语义（标题变 ⚠、底色变 yellow）。
        title_prefix: 覆盖默认 ``⚡``（L3 报表用 📊/📈/📅）。
        header_template: 覆盖自动推导的底色。
        top_of_hour: 覆盖「是否整点」；``None`` = 由当前时间推导。

    Returns:
        飞书 interactive 卡片。
    """
    remain = coerce_float(body.get("remainEq"))
    free_eq = coerce_float(body.get("freeEq"))
    recharge_eq = coerce_float(body.get("rechargeEq"))
    use_eq = coerce_float(body.get("useEq"))
    total_eq = coerce_float(body.get("totalEq"))
    remain_wq_money = coerce_float(body.get("remainWqMoney"))
    dt = coerce_str(body.get("dt"))
    eqprice = coerce_float(body.get("eqprice"))

    top_of_hour_eff = is_top_of_hour() if top_of_hour is None else bool(top_of_hour)

    # ---- header ----
    title_room = room_label if room_label else "宿舍电量监控"
    header_subtitle = (
        f"剩余 {remain:.2f} kW·h" if remain is not None else "剩余 — kW·h"
    )
    title_prefix_eff = "⚡" if title_prefix is None else title_prefix
    header_template_eff = (
        ("green" if top_of_hour_eff else template_for_remain(remain))
        if header_template is None
        else header_template
    )
    if stale:
        title_prefix_eff = "⚠"
        header_template_eff = "yellow"

    header = {
        "title": {
            "tag": "plain_text",
            "content": (
                f"🔔 ⏰ 整点播报 · {title_room}"
                if top_of_hour_eff
                else f"{title_prefix_eff} {title_room}"
            ),
        },
        "subtitle": {"tag": "plain_text", "content": header_subtitle},
        "template": header_template_eff,
    }

    # ---- summary ----
    summary_lines: list[str] = []
    if total_eq is not None and remain is not None and total_eq > 0:
        used_pct = (total_eq - remain) / total_eq * 100
        prefix = "⏰ " if top_of_hour_eff else ""
        summary_lines.append(
            f"{prefix}📊 累计用电 **{total_eq:.2f}** kW·h"
            f"　·　已用 **{used_pct:.1f}%**"
        )
    if eqprice is not None:
        summary_lines.append(f"⚡ 单价：**¥{eqprice:.3f}**/kW·h")
    summary_div = _div("　·　".join(summary_lines)) if summary_lines else None

    # ---- 两行两列 ----
    free_text = f"{free_eq:.2f} kW·h" if free_eq is not None else "—"
    charge_text = f"{recharge_eq:.2f} kW·h" if recharge_eq is not None else "—"
    used_text = f"{use_eq:.2f} kW·h" if use_eq is not None else "—"
    water_text = f"¥{remain_wq_money:.2f}" if remain_wq_money is not None else "—"

    cols_free_charge = _column_set(
        _kv_cell("🎁", "免费电量", free_text),
        _kv_cell("💰", "充值电量", charge_text),
    )
    cols_used_water = _column_set(
        _kv_cell("🔥", "已用电量", used_text),
        _kv_cell("💧", "剩余水费", water_text),
    )

    # ---- 抄表时间（dt 缺失时整行消失）----
    read_time_div = (
        _div(f"🕐 抄表时间：**{dt}**") if dt is not None else None
    )

    # ---- 装配（顺序与快照一致）----
    elements: list[dict] = []
    if summary_div is not None:
        elements.append(summary_div)
    elements.append({"tag": "hr"})
    elements.append(cols_free_charge)
    elements.append({"tag": "hr"})
    elements.append(cols_used_water)
    elements.append({"tag": "hr"})
    if read_time_div is not None:
        elements.append(read_time_div)
    elements.append(_note_element(note))

    return {
        "msg_type": "interactive",
        "card": {"header": header, "elements": elements},
    }


# ---------------------------------------------------------------------------
# L3 —— 日报 / 周报 / 月报
# ---------------------------------------------------------------------------
#: 报表正文里两值之间的分隔符（与快照的摘要行一致）
_SEP = "　·　"


def _eqprice() -> float | None:
    """电价（元/度）—— 报表摘要里显示单价用；读不到则不显示。"""
    try:
        return get_float("eqprice", 0.5)
    except Exception:  # noqa: BLE001 —— 卡片构建不该因为读配置失败而崩
        return None


def l3_narrative(kind: str, summary: dict[str, Any]) -> str:
    """L3 报表的专属叙述（纯文本，lark_md）。空串 = 不加这一段。"""
    def _num(key: str) -> float:
        return coerce_float(summary.get(key)) or 0.0

    def _count(key: str) -> int:
        value = coerce_float(summary.get(key))
        return int(value) if value else 0

    lines: list[str] = []
    if kind == "daily":
        lines.append(
            f"📅 今日：**{_num('today_kwh'):.2f}** kW·h"
            f"{_SEP}昨日：**{_num('yesterday_kwh'):.2f}** kW·h"
        )
        lines.append(f"📈 7 日均值：**{_num('avg7_kwh'):.2f}** kW·h/天")
        if _count("violations_today"):
            lines.append(f"⚠ 今日违规：**{_count('violations_today')}** 次")
    elif kind == "weekly":
        lines.append(
            f"📅 上周累计：**{_num('last_week_kwh'):.2f}** kW·h"
            f"{_SEP}本月累计：**{_num('month_total_kwh'):.2f}** kW·h"
        )
        lines.append(f"💰 近 30 天充值：**¥{_num('recharge_total'):.2f}**")
        if _count("violations_week"):
            lines.append(f"⚠ 近 7 天违规：**{_count('violations_week')}** 次")
    elif kind == "monthly":
        lines.append(
            f"📅 上月累计：**{_num('last_month_kwh'):.2f}** kW·h"
            f"{_SEP}本月累计：**{_num('this_month_kwh'):.2f}** kW·h"
        )
        lines.append(f"💰 近 60 天充值：**¥{_num('recharge_total'):.2f}**")
        if _count("violations_total"):
            lines.append(f"⚠ 近 60 天违规：**{_count('violations_total')}** 次")
    return "\n".join(lines)


def build_l3_card(
    kind: str,
    summary: dict[str, Any],
    room_label: str | None = None,
    note: str = FOOTER_NOTE,
    *,
    title_prefix: str,
    header_template: str,
) -> dict:
    """构建 L3 报表卡片（复用 L2 的两列布局 + 一段 kind 专属叙述）。

    Args:
        kind: ``daily`` / ``weekly`` / ``monthly``。
        summary: :mod:`starwatt.notify.reports` 产出的摘要 dict。
        title_prefix / header_template: 由调用方传入（``reports.L3_TITLES`` /
            ``reports.L3_TEMPLATES``）—— 本模块不反向依赖 reports。

    Returns:
        飞书 interactive 卡片；叙述插在 ``note`` 之前（与 legacy 一致）。
    """
    body: dict[str, Any] = {
        "remainEq": summary.get("remain"),
        "dt": summary.get("dt"),
        "eqprice": _eqprice(),
    }
    # 摘要行的「累计用电」用本月的累积值，量级才可比
    if kind == "monthly":
        body["totalEq"] = summary.get("this_month_kwh") or summary.get("month_total_kwh")
    else:
        body["totalEq"] = summary.get("month_total_kwh")

    card = build_summary_card(
        body,
        room_label,
        note,
        title_prefix=title_prefix,
        header_template=header_template,
        top_of_hour=False,  # L3 就是这一分钟的正牌报告，不要再挂「整点播报」
    )

    narrative = l3_narrative(kind, summary)
    if narrative:
        elements: list[dict] = card["card"]["elements"]
        narrative_div = _div(narrative)
        if elements and elements[-1].get("tag") == "note":
            elements.insert(len(elements) - 1, narrative_div)
        else:
            elements.append(narrative_div)
    return card


# ---------------------------------------------------------------------------
# L4 —— 电表离线
# ---------------------------------------------------------------------------
def is_top_of_hour(moment=None) -> bool:
    """当前（CST）是否整点 —— L2 的「⏰ 整点播报」判定。

    📌 legacy 用 ``datetime.utcnow().minute``，于是「整点」比用户墙上时钟晚 8
    小时（L22 同类缺陷）。重写走 :func:`starwatt.timeutil.now_cst`。

    ⚠️ 必须通过**模块属性**调用（``timeutil.now_cst()``）——
    ``from starwatt.timeutil import now_cst`` 会在 import 时绑定函数对象，
    conftest 的 ``frozen_now`` 就失效了（与 ``endpoints.py`` 同一个坑）。
    """
    return (moment or timeutil.now_cst()).minute == 0


def build_offline_card(run_status: dict[str, Any], note: str = FOOTER_NOTE) -> dict:
    """构建红色「⚠ 电表离线」卡（与 ``cards.json`` 快照一致）。

    📌 只认**驼峰键**（见模块 docstring 第 3 条）：门户返回的就是驼峰，
    下划线键是内部表示，不该出现在卡片契约里。
    """
    update_dt = coerce_str(run_status.get("updateDt")) or "—"
    stop_reason = coerce_str(run_status.get("stopReason")) or "—"
    work_status = coerce_str(run_status.get("workStatus")) or "—"
    run_status_label = coerce_str(run_status.get("runStatus")) or "—"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠ 电表离线"},
                "subtitle": {"tag": "plain_text", "content": f"最后在线 {update_dt}"},
                "template": "red",
            },
            "elements": [
                _div(
                    f"🚨 电表状态：**{run_status_label}**\n"
                    f"📡 通讯状态：**{work_status}**\n"
                    f"⏸ 停机原因：**{stop_reason}**\n"
                    f"🕐 最后上报：**{update_dt}**"
                ),
                _note_element(note),
            ],
        },
    }


# ---------------------------------------------------------------------------
# L1 —— 低电告警
# ---------------------------------------------------------------------------
def _fmt_threshold(value: float) -> str:
    """``30.0`` → ``"30"``（阈值是整数时别显示成 30.0）。"""
    return str(int(value)) if value == int(value) else str(value)


def build_low_battery_card(
    remain: float, *, red_below: float | None = None, note: str = FOOTER_NOTE
) -> dict:
    """构建「🚨 剩余电量过低」卡（L1）。"""
    threshold = (
        _threshold("threshold_red", DEFAULT_RED_BELOW)
        if red_below is None
        else float(red_below)
    )
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🚨 剩余电量过低"},
                "subtitle": {
                    "tag": "plain_text",
                    "content": f"剩余 {remain:.2f} kW·h",
                },
                "template": "red",
            },
            "elements": [
                _div(
                    f"⚡ 剩余电量已跌破 **{_fmt_threshold(threshold)} kW·h** 阈值。"
                    f"建议尽快充值，避免突然断电。"
                ),
                _note_element(note),
            ],
        },
    }


# ---------------------------------------------------------------------------
# stale —— 抓取器长时间无响应
# ---------------------------------------------------------------------------
def build_stale_card(
    last_success: str, gap_seconds: float, note: str = FOOTER_NOTE
) -> dict:
    """构建「⚠ 抓取器长时间无响应」卡（B7）。

    📌 文案里的「请检查 cron」已改为「请检查调度器」—— Q14 之后抓取由
    进程内 APScheduler 驱动，用户不再需要看 crontab。
    """
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "⚠ 抓取器长时间无响应"},
                "subtitle": {
                    "tag": "plain_text",
                    "content": f"上次成功抓取 {last_success} CST",
                },
                "template": "orange",
            },
            "elements": [
                _div(
                    f"🛠 已超过 **{int(gap_seconds // 60)} 分钟** 未成功抓取。"
                    f"请检查调度器 / 网络 / 抓取器日志。"
                ),
                _note_element(note),
            ],
        },
    }


# ---------------------------------------------------------------------------
# 违规 —— 大功率记录
# ---------------------------------------------------------------------------
def format_wg_power(value: float | None) -> str:
    """``wg_power`` 是**瞬时功率（kW）**，不是电量（kW·h）—— 单位别写错。"""
    if value is None:
        return "—"
    return f"{value:.2f} kW"


def build_violation_card(rows: list[dict], note: str = FOOTER_NOTE) -> dict:
    """构建「🚨 用电违规」卡；``rows`` 是**本次要提示的新记录**。

    超过 :data:`_VIOLATION_MAX_LINES` 条时折叠成「…另有 N 条未显示」。
    """
    lines: list[str] = []
    for row in rows[:_VIOLATION_MAX_LINES]:
        dt = coerce_str(row.get("dt")) or "—"
        reason = coerce_str(row.get("wg_reason") or row.get("wgReasonLabel")) or "—"
        power = coerce_float(
            row.get("wg_power") if row.get("wg_power") is not None else row.get("wgPower")
        )
        lines.append(f"• {dt}　·　{reason}　·　{format_wg_power(power)}")

    body_text = f"⚠ 检测到 **{len(rows)}** 条新的违规记录：\n\n" + "\n".join(lines)
    if len(rows) > _VIOLATION_MAX_LINES:
        body_text += f"\n\n…另有 {len(rows) - _VIOLATION_MAX_LINES} 条未显示。"

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": "🚨 用电违规"},
                "subtitle": {"tag": "plain_text", "content": f"{len(rows)} 条新记录"},
                "template": "red",
            },
            "elements": [_div(body_text), _note_element(note)],
        },
    }
