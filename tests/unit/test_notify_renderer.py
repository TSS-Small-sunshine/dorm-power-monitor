"""``starwatt.notify.renderer`` —— PNG 卡片渲染（Pillow）。

渲染没有「冻结的字节快照」（M0 的 ``cards.json`` 只覆盖 JSON 卡片），
所以这里钉的是**契约级属性**：PNG 魔数、尺寸、不抛异常、emoji 走 emoji 字体、
超长文本不溢出。视觉参数一旦改动，这些属性仍应成立。
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from starwatt.notify import card_builder as cb
from starwatt.notify import renderer

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png))


def _summary_card(**body) -> dict:
    payload = {"remainEq": "12.34", "totalEq": "100.0", "dt": "2026-10-06 12:00:00"}
    payload.update(body)
    return cb.build_summary_card(payload, "6号楼-1-119")


# ===========================================================================
# 基础属性
# ===========================================================================
class TestCanvas:
    def test_png_magic_and_size(self, tmp_db) -> None:
        png = renderer.render_text("hello")
        assert png.startswith(PNG_MAGIC)
        image = _open(png)
        assert image.size == (renderer.WIDTH, renderer.HEIGHT) == (800, 600)
        assert image.mode == "RGB"

    def test_font_dir_is_the_single_static_copy(self, tmp_db) -> None:
        """M3 步骤 3.3：字体只有 ``static/fonts/`` 一份。"""
        directory = renderer.font_dir()
        assert directory.name == "fonts" and directory.parent.name == "static"
        for name in (renderer.FONT_REGULAR, renderer.FONT_BOLD, renderer.FONT_EMOJI):
            assert (directory / name).exists(), name

    def test_fonts_are_cached(self, tmp_db) -> None:
        assert renderer.load_font(22) is renderer.load_font(22)

    def test_bold_threshold(self, tmp_db) -> None:
        """大字号与标签用 Bold（与 legacy 同一套阈值）。"""
        assert renderer.load_font(64).path.endswith(renderer.FONT_BOLD)
        assert renderer.load_font(16).path.endswith(renderer.FONT_BOLD)
        assert renderer.load_font(28).path.endswith(renderer.FONT_REGULAR)


class TestEmojiHandling:
    @pytest.mark.parametrize("ch", ["⚡", "📅", "🚨", "⏰", "💧"])
    def test_emoji_detected(self, ch) -> None:
        assert renderer.is_emoji_char(ch) is True

    @pytest.mark.parametrize("ch", ["A", "宿", "1", "—"])
    def test_non_emoji(self, ch) -> None:
        assert renderer.is_emoji_char(ch) is False

    def test_check_mark_is_not_emoji(self) -> None:
        """🔑 ``✓`` 归 MiSans 画（NotoEmoji 只会给空心方框）—— legacy 的教训。"""
        assert renderer.is_emoji_char("✓") is False

    def test_split_runs_merges_consecutive(self) -> None:
        assert renderer.split_runs("⚡⚡ 剩余") == [("⚡⚡", True), (" 剩余", False)]

    def test_split_runs_empty(self) -> None:
        assert renderer.split_runs("") == []

    def test_mixed_text_renders(self, tmp_db) -> None:
        assert renderer.render_text("⚡ 当前剩余: 12.34 kW·h").startswith(PNG_MAGIC)


class TestWrapping:
    def test_wrap_splits_long_line(self, tmp_db) -> None:
        _image, draw = renderer.new_canvas("t")
        lines = renderer.wrap_lines(
            draw, "字" * 200, renderer.load_font(renderer.BODY_FS), 400
        )
        assert len(lines) > 1
        assert "".join(lines) == "字" * 200

    def test_wrap_keeps_existing_newlines(self, tmp_db) -> None:
        _image, draw = renderer.new_canvas("t")
        assert renderer.wrap_lines(draw, "a\nb", renderer.load_font(16), 400) == ["a", "b"]


# ===========================================================================
# 三个渲染入口
# ===========================================================================
class TestRenderEntries:
    def test_render_card(self, tmp_db) -> None:
        png = renderer.render_card(_summary_card())
        assert png.startswith(PNG_MAGIC)
        assert len(png) > 5000  # 有内容，不是空白画布

    def test_render_card_accepts_bare_card(self, tmp_db) -> None:
        assert renderer.render_card(_summary_card()["card"]).startswith(PNG_MAGIC)

    def test_render_card_uses_note_as_footer(self, tmp_db) -> None:
        card = {
            "card": {
                "header": {"title": {"content": "T"}},
                "elements": [
                    {"tag": "note", "elements": [{"tag": "plain_text", "content": "页脚"}]}
                ],
            }
        }
        assert renderer.render_card(card).startswith(PNG_MAGIC)

    def test_render_text_empty(self, tmp_db) -> None:
        """空文本也要出一张卡（不能让用户收到空白）。"""
        assert renderer.render_text("").startswith(PNG_MAGIC)

    def test_render_stat_card(self, tmp_db) -> None:
        png = renderer.render_stat_card(
            "⚡ 实时电表", [("电压", "220.10 V"), ("电流", "0.45 A")], status="在线"
        )
        assert png.startswith(PNG_MAGIC)

    def test_render_stat_card_truncates_overflow(self, tmp_db) -> None:
        rows = [(f"行{i}", f"值{i}") for i in range(40)]
        assert renderer.render_stat_card("很多行", rows).startswith(PNG_MAGIC)

    def test_render_lines_truncates(self, tmp_db) -> None:
        png = renderer.render_lines("标题", [f"第 {i} 行" for i in range(100)])
        assert png.startswith(PNG_MAGIC)

    def test_long_value_does_not_crash(self, tmp_db) -> None:
        assert renderer.render_text("x" * 5000).startswith(PNG_MAGIC)

    def test_default_footer_has_timestamp(self, tmp_db, frozen_now) -> None:
        assert renderer.default_footer() == "更新时间: 2026-10-06 12:00:00"


class TestCardLines:
    def test_lines_include_header_and_body(self, tmp_db) -> None:
        lines = renderer.card_lines(_summary_card())
        assert lines[0] == "⚡ 6号楼-1-119"
        assert "剩余 12.34 kW·h" in lines

    def test_column_set_is_one_line(self, tmp_db) -> None:
        card = {
            "card": {
                "elements": [
                    {
                        "tag": "column_set",
                        "columns": [
                            {"elements": [{"tag": "div", "text": {"content": "🎁 免费"}}]},
                            {"elements": [{"tag": "div", "text": {"content": "💰 充值"}}]},
                        ],
                    }
                ]
            }
        }
        assert renderer.card_lines(card) == ["🎁 免费　💰 充值"]

    def test_hr_becomes_a_divider(self, tmp_db) -> None:
        assert renderer.card_lines({"card": {"elements": [{"tag": "hr"}]}}) == [
            "──────────"
        ]

    def test_non_dict_card(self) -> None:
        assert renderer.card_lines(None) == []
        assert renderer.card_lines("x") == []


class TestFontFallback:
    def test_missing_font_does_not_raise(self, tmp_db, monkeypatch) -> None:
        """字体缺失时退回默认字体（中文变方框），但**绝不抛异常**。"""
        monkeypatch.setattr(renderer, "font_dir", lambda: Path("__missing__"))
        renderer._font_cache.clear()
        try:
            assert renderer.render_text("中文").startswith(PNG_MAGIC)
        finally:
            renderer._font_cache.clear()
