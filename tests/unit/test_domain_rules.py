"""``starwatt.domain.thresholds`` + ``pricing`` —— 阈值判定与单价（纯函数）。

这两个模块是「颜色分区 / 离线 / stale / 单价」的**唯一实现**，判定口径错了
不会报错、只会让用户看到错的颜色或错的金额 —— 所以边界逐个钉死。
"""
from __future__ import annotations

import pytest

from starwatt.domain import pricing, thresholds


# ===========================================================================
# 颜色分区
# ===========================================================================
class TestColorZone:
    @pytest.mark.parametrize(
        ("remain", "expected"),
        [
            (None, "blue"),  # 数据没拿到 → 不吓人
            (0, "red"),
            (29.99, "red"),
            (30, "orange"),  # 边界：严格小于
            (79.99, "orange"),
            (80, "blue"),
            (199.99, "blue"),
            (200, "green"),
            (999, "green"),
        ],
    )
    def test_matches_cards_fixture(self, remain, expected) -> None:
        assert thresholds.color_zone(remain) == expected

    def test_custom_thresholds(self) -> None:
        assert thresholds.color_zone(5, red_below=10, orange_below=20, blue_below=30) == "red"
        assert thresholds.color_zone(15, red_below=10, orange_below=20, blue_below=30) == "orange"
        assert thresholds.color_zone(25, red_below=10, orange_below=20, blue_below=30) == "blue"
        assert thresholds.color_zone(35, red_below=10, orange_below=20, blue_below=30) == "green"


class TestIsLow:
    def test_below_threshold(self) -> None:
        assert thresholds.is_low(29.99) is True

    def test_at_threshold(self) -> None:
        assert thresholds.is_low(30) is False  # 严格小于

    def test_missing(self) -> None:
        assert thresholds.is_low(None) is False

    def test_custom_threshold(self) -> None:
        assert thresholds.is_low(50, red_below=60) is True


# ===========================================================================
# 离线判定
# ===========================================================================
class TestIsOffline:
    @pytest.mark.parametrize("label", ["在线", "正常", "通讯正常"])
    def test_online_labels(self, label) -> None:
        assert thresholds.is_offline(label) is False

    @pytest.mark.parametrize("label", ["离线", "未知", "", "  ", None])
    def test_offline_labels(self, label) -> None:
        """🔑 缺字段 / 空串也算离线（B6：宁可误报也不能静默失败）。"""
        assert thresholds.is_offline(label) is True

    def test_whitespace_is_stripped(self) -> None:
        assert thresholds.is_offline(" 在线 ") is False

    def test_custom_online_values(self) -> None:
        assert thresholds.is_offline("ONLINE", ("ONLINE",)) is False
        assert thresholds.is_offline("在线", ("ONLINE",)) is True


# ===========================================================================
# stale 判定
# ===========================================================================
class TestIsStale:
    def test_within_threshold(self) -> None:
        assert thresholds.is_stale(3600, 2) is False

    def test_at_threshold(self) -> None:
        assert thresholds.is_stale(7200, 2) is True  # 含边界

    def test_beyond_threshold(self) -> None:
        assert thresholds.is_stale(3 * 3600, 2) is True

    def test_never_scraped_is_not_stale(self) -> None:
        """从未成功过 → 那不是「陈旧」，是「还没开始」。"""
        assert thresholds.is_stale(None, 2) is False

    def test_custom_hours(self) -> None:
        assert thresholds.is_stale(3600 * 5, 8) is False


# ===========================================================================
# 单价与金额
# ===========================================================================
class TestParsePrice:
    def test_first_source_wins(self) -> None:
        assert pricing.parse_price("0.62", "0.99") == 0.62

    def test_skips_empty_sources(self) -> None:
        assert pricing.parse_price(None, "", "  ", "0.75") == 0.75

    def test_falls_back_to_default(self) -> None:
        assert pricing.parse_price(None, "") == pricing.DEFAULT_EQPRICE == 0.5

    def test_unparsable_values_are_skipped(self) -> None:
        assert pricing.parse_price("abc", "0.88") == 0.88

    def test_rounds_to_four_decimals(self) -> None:
        assert pricing.parse_price("0.5123456") == 0.5123

    def test_accepts_numbers(self) -> None:
        assert pricing.parse_price(0.62) == 0.62


class TestMoney:
    def test_normal(self) -> None:
        assert pricing.money(30.0, 0.5) == 15.0

    def test_rounds_to_two_decimals(self) -> None:
        assert pricing.money(12.345, 0.5) == 6.17

    @pytest.mark.parametrize(("kwh", "price"), [(None, 0.5), (10.0, None), (None, None)])
    def test_missing_inputs_give_none(self, kwh, price) -> None:
        """缺失 → ``None``（前端渲染「—」），而不是骗人的 ``0.00``。"""
        assert pricing.money(kwh, price) is None
