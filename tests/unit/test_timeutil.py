"""``starwatt.timeutil`` 单元测试（Q10 —— 显式锁定 Asia/Shanghai）。"""
from __future__ import annotations

from datetime import datetime, timezone

from starwatt import timeutil


class TestNowCst:
    def test_returns_naive_datetime(self) -> None:
        now = timeutil.now_cst()
        assert isinstance(now, datetime)
        assert now.tzinfo is None, "必须是朴素时间（Q10 约定，零数据迁移的前提）"

    def test_equals_shanghai_wall_clock(self) -> None:
        """核心保证：结果 = UTC now 转上海，而**不是**本机时区的 now。"""
        expected = datetime.now(timezone.utc).astimezone(timeutil.CST).replace(tzinfo=None)
        delta = abs((timeutil.now_cst() - expected).total_seconds())
        assert delta < 2, f"与上海墙钟偏差 {delta}s"

    def test_is_consistent_with_today(self) -> None:
        assert timeutil.today_cst() == timeutil.now_cst().date()


class TestStamp:
    def test_stamp_format_matches_legacy(self) -> None:
        """写库格式必须与旧数据一致：``YYYY-MM-DD HH:MM:SS``（19 字符）。"""
        text = timeutil.stamp()
        assert len(text) == 19
        assert text[4] == "-" and text[7] == "-"
        assert text[10] == " " and text[13] == ":" and text[16] == ":"

    def test_stamp_parses_back(self) -> None:
        parsed = timeutil.parse_stamp(timeutil.stamp())
        assert parsed is not None
        assert parsed.date() == timeutil.today_cst()

    def test_to_stamp_roundtrip(self) -> None:
        moment = datetime(2026, 10, 6, 12, 0, 0)
        assert timeutil.to_stamp(moment) == "2026-10-06 12:00:00"
        assert timeutil.parse_stamp("2026-10-06 12:00:00") == moment


class TestParseStamp:
    def test_parses_valid(self) -> None:
        assert timeutil.parse_stamp("2026-10-06 12:00:00") == datetime(2026, 10, 6, 12, 0, 0)

    def test_strips_whitespace(self) -> None:
        assert timeutil.parse_stamp("  2026-10-06 12:00:00  ") is not None

    def test_returns_none_for_none(self) -> None:
        assert timeutil.parse_stamp(None) is None

    def test_returns_none_for_empty(self) -> None:
        assert timeutil.parse_stamp("") is None
        assert timeutil.parse_stamp("   ") is None

    def test_returns_none_for_garbage(self) -> None:
        """不抛异常 —— 旧代码里散落的 try/except 统一收敛到这里。"""
        assert timeutil.parse_stamp("nope") is None
        assert timeutil.parse_stamp("2026-13-45 99:99:99") is None
        assert timeutil.parse_stamp(12345) is None

    def test_returns_none_for_iso_with_t(self) -> None:
        """ISO 的 ``T`` 分隔符不被接受（旧代码用 ``fromisoformat``，格式不同）。"""
        assert timeutil.parse_stamp("2026-10-06T12:00:00") is None


class TestTimezoneObject:
    def test_cst_is_shanghai(self) -> None:
        assert str(timeutil.CST) == "Asia/Shanghai"

    def test_cst_offset_is_8_hours(self) -> None:
        """中国自 1991 年起无夏令时，固定 UTC+8。"""
        offset = datetime(2026, 6, 1, tzinfo=timezone.utc).astimezone(timeutil.CST).utcoffset()
        assert offset is not None and offset.total_seconds() == 8 * 3600
