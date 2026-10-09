"""``starwatt.notify.commands`` —— 9 个命令 + 菜单（渠道无关）。

命令的文案是**冻结契约**（用户的肌肉记忆），所以这里逐字断言：
含 emoji、含半角逗号（legacy 原样），以及「无数据时说什么」。
"""
from __future__ import annotations

import pytest

from starwatt.config_registry import set_state
from starwatt.db.models import Record
from starwatt.db.repositories import (
    DailyElecRepo,
    PayRepo,
    RecordRepo,
    RunStatusRepo,
    ViolationRepo,
)
from starwatt.notify import commands as cmd

ROOM = "room-1"


@pytest.fixture(autouse=True)
def _room(tmp_db, frozen_now):
    """默认给一个房间号（``last_room_id``），并冻结时间。

    ⚠️ 必须依赖 ``tmp_db`` —— 否则 ``set_state`` 会打在一个还没建表的库上。
    """
    set_state("last_room_id", ROOM)
    return ROOM


class TestMaps:
    def test_commands_match_legacy(self) -> None:
        assert cmd.COMMANDS == {
            "/状态": "remain",
            "/剩余": "remain",
            "/电表": "meter",
            "/电表状态": "meter",
            "/今日": "today",
            "/历史": "history",
            "/缴费": "pay",
            "/违规": "violations",
            "/帮助": "help",
        }

    def test_menu_keys_match_legacy(self) -> None:
        assert set(cmd.MENU_KEYS) == {
            "menu_remain",
            "menu_meter",
            "menu_today",
            "menu_history",
            "menu_pay",
            "menu_violations",
            "menu_help",
        }
        assert cmd.MENU_KEYS["menu_remain"] == "remain"

    def test_help_text_starts_with_the_brand_line(self) -> None:
        assert cmd.HELP_TEXT.startswith("⚡ 宿舍电量小助手 · 命令列表")
        assert "/帮助 — 显示本条" in cmd.HELP_TEXT

    def test_every_handler_has_a_title(self) -> None:
        for handler in set(cmd.COMMANDS.values()) | set(cmd.MENU_KEYS.values()):
            assert cmd.TITLES.get(handler), handler


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("/状态", "/状态"),
            (" /状态", "/状态"),
            ("@_user_1 /状态", "/状态"),
            ("@_bot_1   /电表", "/电表"),
            ("@_user_1 @_user_2 /今日", "/今日"),
            ("<@!1234567890> /状态", "/状态"),
            ("<@123> /历史 6", "/历史 6"),
            ("", ""),
            (None, ""),
            ("@_user_1", ""),
            ("<@!1234567890>", ""),
        ],
    )
    def test_strip_mention(self, raw, expected) -> None:
        assert cmd.strip_mention(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("", 24),
            ("12", 12),
            ("720", 720),
            ("9999", 24),
            ("abc", 24),
            ("-5", 24),
            ("0", 24),
            ("3.5", 24),
        ],
    )
    def test_parse_history_args(self, raw, expected) -> None:
        assert cmd.parse_history_args(raw) == expected

    def test_current_room_id_reads_state(self, tmp_db) -> None:
        set_state("last_room_id", "room-9")
        assert cmd.current_room_id() == "room-9"

    def test_current_room_id_returns_none_when_unset(self, tmp_db) -> None:
        set_state("last_room_id", "")
        assert cmd.current_room_id() is None

    def test_current_room_id_survives_config_error(self, tmp_db, monkeypatch) -> None:
        """读配置失败（首次启动 meta 表未建）时返回 None，不抛。"""
        def _boom(*_args, **_kwargs):
            raise RuntimeError("no such table: meta")

        monkeypatch.setattr(cmd, "get_str", _boom)
        assert cmd.current_room_id() is None


class TestCmdRemain:
    def test_no_data(self, tmp_db) -> None:
        assert cmd.cmd_remain() == "暂未抓到电量数据,等下次抓取。"

    def test_normal(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time="2026-10-06 10:00:00", remain=42.5))
        assert cmd.cmd_remain() == (
            "⚡ 当前剩余: 42.50 kW·h\n🕐 抄表时间: 2026-10-06 10:00:00"
        )

    def test_missing_remain(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=None))
        assert cmd.cmd_remain() == "抄表时间: —\n剩余电量数据缺失。"


class TestCmdMeter:
    def test_no_room(self, tmp_db) -> None:
        set_state("last_room_id", "")
        assert cmd.cmd_meter() == "暂未发现 roomId,请先跑一次抓取。"

    def test_no_data(self, tmp_db) -> None:
        assert cmd.cmd_meter() == "暂无电表数据,等下一次抓取。"

    def test_online(self, tmp_db) -> None:
        RunStatusRepo.upsert(
            ROOM,
            {"vol": 220.1, "cur": 0.45, "yggl": 12.3, "runStatus": "在线",
             "updateDt": "2026-10-06 11:59:00"},
        )
        text = cmd.cmd_meter()
        assert text.startswith("🟢 电表状态: 在线")
        assert "⚡ 电压: 220.10 V" in text
        assert "🔌 电流: 0.45 A" in text
        assert "💡 有功功率: 12.300 W" in text
        assert text.endswith("🕐 最后上报: 2026-10-06 11:59:00")

    def test_offline_icon(self, tmp_db) -> None:
        RunStatusRepo.upsert(ROOM, {"runStatus": "离线"})
        assert cmd.cmd_meter().startswith("🔴 电表状态: 离线")

    def test_missing_fields_render_dashes(self, tmp_db) -> None:
        RunStatusRepo.upsert(ROOM, {"runStatus": "在线"})
        text = cmd.cmd_meter()
        assert "⚡ 电压: —" in text and "🔌 电流: —" in text


class TestCmdToday:
    def test_no_data(self, tmp_db) -> None:
        assert cmd.cmd_today() == "暂无今日用电数据,等下一次抓取。"

    def test_uses_total_eq(self, tmp_db) -> None:
        DailyElecRepo.upsert_many(ROOM, [{"roomId": ROOM, "dt": "2026-10-06", "total_eq": 12.34}])
        assert cmd.cmd_today() == "📅 2026-10-06\n⚡ 今日用电: 12.34 kW·h"

    def test_falls_back_to_zong_eq(self, tmp_db) -> None:
        DailyElecRepo.upsert_many(
            ROOM, [{"roomId": ROOM, "dt": "2026-10-06", "total_eq": None, "zong_eq": 7.5}]
        )
        assert "⚡ 今日用电: 7.50 kW·h" in cmd.cmd_today()

    def test_missing_values(self, tmp_db) -> None:
        DailyElecRepo.upsert_many(ROOM, [{"roomId": ROOM, "dt": "2026-10-06"}])
        assert cmd.cmd_today() == "日期 2026-10-06: 今日用电数据缺失。"


class TestCmdHistory:
    def test_no_data(self, tmp_db) -> None:
        assert cmd.cmd_history(hours=12) == "近 12 小时无抓取数据。"

    def test_consumption_is_first_minus_last(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 09:00:00", read_time=None, remain=50.0))
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=44.5))
        text = cmd.cmd_history(hours=24)
        assert "开始 (2026-10-06 09:00:00): 50.00 kW·h" in text
        assert "结束 (2026-10-06 11:00:00): 44.50 kW·h" in text
        assert text.endswith("消耗: 5.50 kW·h")

    def test_all_rows_missing_remain(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 09:00:00", read_time=None, remain=None))
        assert cmd.cmd_history(hours=24) == "近 24 小时无有效剩余电量数据。"


class TestCmdPay:
    def test_no_data(self, tmp_db) -> None:
        assert cmd.cmd_pay() == "📋 暂无缴费记录。"

    def test_lists_three_recent(self, tmp_db) -> None:
        PayRepo.upsert_many(
            ROOM,
            [
                {"roomId": ROOM, "dt": f"2026-10-0{i}", "pay_type": "充值",
                 "fee_type": "电费", "money": 10.0 * i}
                for i in range(1, 5)
            ],
        )
        text = cmd.cmd_pay()
        assert text.startswith("📋 最近缴费 (最近 3 笔):")
        assert len(text.splitlines()) == 4  # 标题 + 3 笔
        assert "¥40.00" in text  # 最新一笔（dt 倒序）


class TestCmdViolations:
    def test_none(self, tmp_db) -> None:
        assert cmd.cmd_violations() == "✓ 近 30 天无违规记录,太棒了！"

    def test_lists_and_folds(self, tmp_db) -> None:
        ViolationRepo.upsert_many(
            ROOM,
            [
                {"roomId": ROOM, "dt": f"2026-10-{i:02d} 09:00:00",
                 "wg_reason": "大功率", "wg_power": 1.5}
                for i in range(1, 11)
            ],
        )
        text = cmd.cmd_violations()
        assert text.startswith("⚠ 近 30 天违规 (10 条):")
        assert "1.50 kW" in text
        assert text.endswith("· …另有 2 条未显示。")

    def test_missing_power_renders_dash(self, tmp_db) -> None:
        ViolationRepo.upsert_many(
            ROOM, [{"roomId": ROOM, "dt": "2026-10-06 09:00:00", "wg_reason": "违规电器"}]
        )
        assert "—" in cmd.cmd_violations()


class TestDispatch:
    def test_empty_message_is_not_replied(self, tmp_db) -> None:
        assert cmd.dispatch_text("").is_empty is True
        assert cmd.dispatch_text("@_user_1").is_empty is True
        assert cmd.dispatch_text(None).is_empty is True

    def test_help_command(self, tmp_db) -> None:
        reply = cmd.dispatch_text("/帮助")
        assert reply.text == cmd.HELP_TEXT
        assert reply.title.startswith("⚡")

    def test_unknown_command_returns_help(self, tmp_db) -> None:
        reply = cmd.dispatch_text("/不存在")
        assert reply.text.startswith("未知命令: /不存在")
        assert cmd.HELP_TEXT in reply.text

    def test_mention_is_stripped_in_groups(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=9.0))
        reply = cmd.dispatch_text("@_bot_1 /状态")
        assert reply.text.startswith("⚡ 当前剩余: 9.00 kW·h")

    def test_history_argument_is_parsed(self, tmp_db) -> None:
        RecordRepo.insert(Record(ts="2026-10-06 11:00:00", read_time=None, remain=9.0))
        assert "近 6 小时" in cmd.dispatch_text("/历史 6").text
        assert "近 24 小时" in cmd.dispatch_text("/历史 abc").text

    def test_room_id_can_be_passed_explicitly(self, tmp_db) -> None:
        set_state("last_room_id", "")
        RunStatusRepo.upsert(ROOM, {"runStatus": "在线"})
        assert "电表状态: 在线" in cmd.dispatch_text("/电表", ROOM).text

    def test_menu_dispatch(self, tmp_db) -> None:
        RunStatusRepo.upsert(ROOM, {"runStatus": "在线"})
        assert "电表状态: 在线" in cmd.dispatch_menu("menu_meter").text

    def test_unknown_menu(self, tmp_db) -> None:
        assert cmd.dispatch_menu("menu_nope").text == "未知菜单项。"
        assert cmd.dispatch_menu(None).text == "未知菜单项。"

    def test_reply_title_is_set_for_card_commands(self, tmp_db) -> None:
        assert cmd.dispatch_text("/今日").title == "📅 今日用电"
