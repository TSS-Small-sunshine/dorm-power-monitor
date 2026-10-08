"""``starwatt.notify.card_builder`` —— 与 M0 契约快照逐字节比对。

``cards.json`` 是在 **legacy 代码上真实运行**产出的（M0 步骤 0.4），
所以这些断言是「重写没有偷偷改变用户看到的东西」的唯一证据。

输入向量与 ``tests/regression/generate_fixtures.py`` 的 ``_CARD_BODIES``
完全一致（复制而非 import —— 那份生成器属于 M0 的历史产物，将来可能移除）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from starwatt.notify import card_builder as cb

FIXTURE = Path(__file__).resolve().parents[1] / "regression" / "fixtures" / "cards.json"

ROOM_LABEL = "6号楼-1-119"
NOTE = "note-fixture"

#: 与 generate_fixtures._CARD_BODIES 相同
CARD_BODIES: dict[str, dict] = {
    "normal": {
        "remainEq": "12.34",
        "freeEq": "5.0",
        "rechargeEq": "7.34",
        "useEq": "100.0",
        "totalEq": "112.34",
        "remainWqMoney": "3.20",
        "dt": "2026-10-06 12:00:00",
        "eqprice": "0.5000",
    },
    "all_null": {
        "remainEq": None,
        "freeEq": None,
        "rechargeEq": None,
        "useEq": None,
        "totalEq": None,
        "remainWqMoney": None,
        "dt": None,
        "eqprice": None,
    },
    "red_zone": {"remainEq": "29.99", "totalEq": "100.0", "dt": "2026-10-06 12:00:00"},
    "orange_zone": {"remainEq": "79.99", "totalEq": "200.0", "dt": "2026-10-06 12:00:00"},
    "blue_zone": {"remainEq": "199.99", "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
    "green_zone": {"remainEq": "200.0", "totalEq": "400.0", "dt": "2026-10-06 12:00:00"},
    "no_dt": {"remainEq": "50.0", "totalEq": "200.0"},
}

#: 颜色分区边界（严格小于）
TEMPLATE_BOUNDARIES = (None, 0, 29.99, 30, 79.99, 80, 199.99, 200, 999)


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# ===========================================================================
# L2 摘要卡 —— 7 个向量逐字节一致
# ===========================================================================
class TestSummaryCardContract:
    @pytest.mark.parametrize("name", sorted(CARD_BODIES))
    def test_matches_snapshot(self, fixture, name, tmp_db) -> None:
        got = cb.build_summary_card(
            CARD_BODIES[name], ROOM_LABEL, NOTE, top_of_hour=False
        )
        assert got == fixture["_build_card"][name]

    def test_json_is_byte_identical(self, fixture, tmp_db) -> None:
        """序列化后也要一致 —— 字段顺序不同也会让飞书渲染变化。"""
        got = json.dumps(
            cb.build_summary_card(
                CARD_BODIES["normal"], ROOM_LABEL, NOTE, top_of_hour=False
            ),
            ensure_ascii=False,
            sort_keys=True,
        )
        want = json.dumps(
            fixture["_build_card"]["normal"], ensure_ascii=False, sort_keys=True
        )
        assert got == want


# ===========================================================================
# L4 离线卡
# ===========================================================================
class TestOfflineCardContract:
    @pytest.mark.parametrize(
        ("name", "run_status"),
        [
            ("empty", {}),
            # 📌 生成器喂的就是蛇形键 —— 卡片只认驼峰，所以三个向量输出相同
            ("offline", {"run_status": "离线", "update_dt": "2026-10-06 11:00:00"}),
            ("missing_label", {"update_dt": "2026-10-06 11:00:00"}),
        ],
    )
    def test_matches_snapshot(self, fixture, name, run_status, tmp_db) -> None:
        assert (
            cb.build_offline_card(run_status, NOTE)
            == fixture["_build_offline_card"][name]
        )

    def test_camel_case_keys_are_rendered(self, tmp_db) -> None:
        """真实门户给的是驼峰键 —— 那时卡片必须显示内容（不是 —）。"""
        card = cb.build_offline_card(
            {
                "runStatus": "离线",
                "workStatus": "通讯中断",
                "stopReason": "欠费",
                "updateDt": "2026-10-06 11:00:00",
            },
            NOTE,
        )
        content = card["card"]["elements"][0]["text"]["content"]
        assert "离线" in content and "通讯中断" in content
        assert (
            card["card"]["header"]["subtitle"]["content"]
            == "最后在线 2026-10-06 11:00:00"
        )


# ===========================================================================
# 颜色分区
# ===========================================================================
class TestTemplateForRemain:
    def test_matches_snapshot(self, fixture, tmp_db) -> None:
        for raw, expected in fixture["_template_for_remain"].items():
            value = None if raw == "None" else float(raw)
            assert cb.template_for_remain(value) == expected, raw

    def test_boundaries_match_snapshot(self, fixture, tmp_db) -> None:
        for value in TEMPLATE_BOUNDARIES:
            key = str(value)
            assert (
                cb.template_for_remain(value)
                == fixture["_template_for_remain_boundaries"][key]
            ), key

    def test_thresholds_follow_config(self, tmp_db) -> None:
        """阈值来自配置注册表（Q15 可改）—— 改配置后分区立刻变。"""
        from starwatt.config_registry import set_many

        assert cb.template_for_remain(29.99) == "red"
        set_many({"threshold_red": 10})
        assert cb.template_for_remain(29.99) == "orange"


# ===========================================================================
# 其余卡片（快照未覆盖，但结构必须稳定）
# ===========================================================================
class TestAlertCards:
    def test_low_battery_card(self, tmp_db) -> None:
        card = cb.build_low_battery_card(12.345, note=NOTE)
        assert card["card"]["header"]["template"] == "red"
        assert card["card"]["header"]["subtitle"]["content"] == "剩余 12.35 kW·h"
        assert "**30 kW·h**" in card["card"]["elements"][0]["text"]["content"]
        assert card["card"]["elements"][-1]["elements"][0]["content"] == NOTE

    def test_low_battery_card_fractional_threshold(self, tmp_db) -> None:
        card = cb.build_low_battery_card(5.0, red_below=12.5, note=NOTE)
        assert "**12.5 kW·h**" in card["card"]["elements"][0]["text"]["content"]

    def test_stale_card(self, tmp_db) -> None:
        card = cb.build_stale_card("2026-10-06 09:00:00", 3 * 3600, note=NOTE)
        assert card["card"]["header"]["template"] == "orange"
        assert card["card"]["header"]["subtitle"]["content"] == (
            "上次成功抓取 2026-10-06 09:00:00 CST"
        )
        assert "180 分钟" in card["card"]["elements"][0]["text"]["content"]

    def test_violation_card_lists_rows(self, tmp_db) -> None:
        rows = [
            {"dt": "2026-10-06 11:00:00", "wg_reason": "大功率", "wg_power": 1.5},
            {"dt": "2026-10-06 10:00:00", "wg_reason": "违规电器", "wg_power": None},
        ]
        card = cb.build_violation_card(rows, note=NOTE)
        body = card["card"]["elements"][0]["text"]["content"]
        assert "**2** 条新的违规记录" in body
        assert "• 2026-10-06 11:00:00　·　大功率　·　1.50 kW" in body
        assert "—" in body  # 缺失功率

    def test_violation_card_folds_extra_rows(self, tmp_db) -> None:
        rows = [
            {"dt": f"2026-10-06 0{i}:00:00", "wg_reason": "r", "wg_power": 1.0}
            for i in range(10)
        ]
        card = cb.build_violation_card(rows, note=NOTE)
        body = card["card"]["elements"][0]["text"]["content"]
        assert "…另有 2 条未显示。" in body

    def test_format_wg_power_is_kw(self) -> None:
        """``wg_power`` 单位是 kW（瞬时功率），不是 kW·h。"""
        assert cb.format_wg_power(1.5) == "1.50 kW"
        assert cb.format_wg_power(None) == "—"


class TestTopOfHour:
    def test_top_of_hour_prefix(self, tmp_db) -> None:
        card = cb.build_summary_card(
            CARD_BODIES["red_zone"], ROOM_LABEL, NOTE, top_of_hour=True
        )
        header = card["card"]["header"]
        assert header["title"]["content"] == f"🔔 ⏰ 整点播报 · {ROOM_LABEL}"
        assert header["template"] == "green"  # 整点强制绿色

    def test_stale_overrides_prefix_and_template(self, tmp_db) -> None:
        card = cb.build_summary_card(
            CARD_BODIES["green_zone"], ROOM_LABEL, NOTE, stale=True, top_of_hour=False
        )
        header = card["card"]["header"]
        assert header["title"]["content"].startswith("⚠ ")
        assert header["template"] == "yellow"

    def test_is_top_of_hour_uses_cst(self, frozen_now) -> None:
        """``frozen_now`` = 12:00:00 → 整点（legacy 用 UTC 会差 8 小时）。"""
        assert cb.is_top_of_hour() is True

    def test_summary_card_derives_top_of_hour(self, tmp_db, frozen_now) -> None:
        card = cb.build_summary_card(CARD_BODIES["red_zone"], ROOM_LABEL, NOTE)
        assert "整点播报" in card["card"]["header"]["title"]["content"]
