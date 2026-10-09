"""PNG 卡片渲染（Pillow + MiSans + NotoEmoji）。

为什么需要它
============

飞书**群卡片**可以直接发 JSON（``card_builder`` 那套），但**私聊机器人**的
9 个命令回复在 legacy 里是**渲染成 PNG 再上传**的 —— 因为命令输出是「多行
键值文本」，卡片 JSON 的排版反而更差。QQ 侧同理：QQ 没有卡片消息，富媒体
（图片）是最接近的形态。

视觉参数（沿用 legacy，别随意改）
================================

======================  ==========================================
画布                     800 × 600，深色 ``#1A1A1A``
顶栏                     60px 高，``#26262A``，标题 + 状态胶囊
字体                     MiSans Regular / Bold（>=40pt 或 14–22pt 用 Bold）
Emoji                    NotoEmoji（MiSans 没有 emoji 字形）
页脚                     更新时间 + 品牌
======================  ==========================================

字体缺失时的行为
================

**不抛异常**：退回 ``ImageFont.load_default()`` 并记 WARNING ——
中文会变成「口口口」，但用户至少能收到一张卡片，而不是 5xx。

📌 ``scripts/ast_guard.py`` 的 R8 教训：Windows 上写文件容易带 BOM，
字体路径一律用 ``Path`` 拼接，不写死分隔符。
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from starwatt import timeutil
from starwatt.config import get_settings

logger = logging.getLogger("starwatt.notify")

__all__ = [
    "BG",
    "HEIGHT",
    "MARGIN_X",
    "PANEL",
    "SUB",
    "TEXT",
    "WIDTH",
    "card_lines",
    "emoji_font",
    "font_dir",
    "is_emoji_char",
    "load_font",
    "new_canvas",
    "render_card",
    "render_lines",
    "render_stat_card",
    "render_text",
    "split_runs",
    "to_png",
]

#: 画布尺寸（legacy 的 800×600 —— 飞书与 QQ 都能正常显示）
WIDTH, HEIGHT = 800, 600

#: 配色（legacy 原值）
BG = (26, 26, 26)
PANEL = (38, 38, 42)
TEXT = (240, 240, 240)
SUB = (170, 170, 175)
ACCENT = (60, 145, 230)
GREEN = (52, 199, 110)
RED = (235, 80, 80)
ORANGE = (255, 165, 0)

#: 版式
MARGIN_X = 32
MARGIN_Y = 24
HEADER_H = 60
FOOTER_H = 36
TITLE_FS = 28
HERO_FS = 64
BODY_FS = 22
LABEL_FS = 16
FOOTER_FS = 14
LINE_H = 34  # 正文行高

#: 需要交给 emoji 字体的码点区间（MiSans 没有这些字形）
#: ⚠️ 刻意**排除** BMP 装饰符号块 U+2700–U+27BF：MiSans 能正常画 ✓/✔，
#: 而 NotoEmoji 只会给一个空心方框。
EMOJI_RANGES: tuple[tuple[int, int], ...] = (
    (0x2300, 0x23FF),  # ⏰ ⌨ ⏳
    (0x2460, 0x24FF),
    (0x2600, 0x26FF),  # ⚡ ⚠
    (0x2B00, 0x2BFF),
    (0x1F000, 0x1F02F),
    (0x1F0A0, 0x1F0FF),
    (0x1F100, 0x1F2FF),
    (0x1F300, 0x1F5FF),  # 📅 📊 📋 💧 🚨
    (0x1F600, 0x1F64F),
    (0x1F680, 0x1F6FF),
    (0x1F700, 0x1F77F),
    (0x1F780, 0x1F7FF),
    (0x1F800, 0x1F8FF),
    (0x1F900, 0x1F9FF),
    (0x1FA00, 0x1FAFF),
)

#: 字体文件名（M3 步骤 3.3：仓库只保留 ``static/fonts/`` 一份）
FONT_REGULAR = "MiSans-Regular.ttf"
FONT_BOLD = "MiSans-Bold.ttf"
FONT_EMOJI = "NotoEmoji-Regular.ttf"

#: 字体缓存（``(path, size) -> font``；Pillow 内部还会缓存字形）
_font_cache: dict[tuple[str, int], Any] = {}


def font_dir() -> Path:
    """字体目录（``<项目根>/static/fonts``）。"""
    return Path(get_settings().project_root) / "static" / "fonts"


def _load(path: Path, size: int) -> Any:
    key = (str(path), size)
    cached = _font_cache.get(key)
    if cached is not None:
        return cached
    try:
        font = ImageFont.truetype(str(path), size)
    except OSError as exc:
        logger.warning(
            "字体加载失败（%s）：%s —— 退回默认字体（中文会显示为方框）", path, exc
        )
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def load_font(size: int) -> Any:
    """按字号取字体；大字号（>=40）与胶囊/标签（14–22）用 Bold。"""
    bold = size >= 40 or 14 <= size <= 22
    name = FONT_BOLD if bold else FONT_REGULAR
    return _load(font_dir() / name, size)


def emoji_font(size: int) -> Any:
    """取 emoji 字体（缺失时退回 Regular —— 会画成方框，但不崩）。"""
    path = font_dir() / FONT_EMOJI
    if not path.exists():
        return load_font(size)
    return _load(path, size)


# ---------------------------------------------------------------------------
# 文本：CJK + emoji 混排
# ---------------------------------------------------------------------------
def is_emoji_char(ch: str) -> bool:
    """该字符是否应该交给 emoji 字体画。"""
    if not ch:
        return False
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in EMOJI_RANGES)


def split_runs(text: str) -> list[tuple[str, bool]]:
    """按 emoji/非 emoji 切成 ``[(片段, 是否emoji), ...]``（连续的同类合并）。"""
    if not text:
        return []
    runs: list[tuple[str, bool]] = []
    current: list[str] = []
    current_is_emoji: bool | None = None
    for ch in text:
        is_e = is_emoji_char(ch)
        if current_is_emoji is None or is_e == current_is_emoji:
            current.append(ch)
            current_is_emoji = is_e
            continue
        runs.append(("".join(current), bool(current_is_emoji)))
        current, current_is_emoji = [ch], is_e
    if current:
        runs.append(("".join(current), bool(current_is_emoji)))
    return runs


def draw_text(draw, xy, text: str, font, fill=TEXT, anchor: str = "lt") -> None:
    """画一段文本，自动把 emoji 交给 emoji 字体。

    混合排版时需要先算总宽，否则 ``r`` / ``m`` 锚点会错位（legacy Round 22
    的实现，这里保留同一套算法）。
    """
    if draw is None or not text:
        return
    if font is None:
        font = ImageFont.load_default()

    runs = split_runs(text)
    if not any(is_e for _, is_e in runs):  # 没有 emoji → 走最简单的路径
        try:
            draw.text(xy, text, font=font, fill=fill, anchor=anchor)
        except (TypeError, ValueError) as exc:  # pragma: no cover - 布局异常不该崩
            logger.warning("文本绘制失败（%r）：%s", text, exc)
        return

    size = int(getattr(font, "size", BODY_FS) or BODY_FS)
    efont = emoji_font(size)

    # 1) 先量总宽/总高（用于 r/m 锚点）
    min_x = min_y = 0
    max_x = max_y = 0
    cursor = 0
    for segment, is_e in runs:
        current = efont if is_e else font
        box = draw.textbbox((0, 0), segment, font=current)
        min_x = min(min_x, cursor + box[0])
        max_x = max(max_x, cursor + box[2])
        min_y = min(min_y, box[1])
        max_y = max(max_y, box[3])
        cursor += box[2] - box[0]
    total_w, total_h = max_x - min_x, max_y - min_y

    # 2) 把锚点换算成「左上角」坐标
    x, y = xy
    horiz = anchor[0] if anchor else "l"
    vert = anchor[1] if len(anchor) > 1 else "t"
    dx = -min_x if horiz == "l" else (-max_x if horiz == "r" else -(min_x + total_w // 2))
    dy = -min_y if vert == "t" else (-max_y if vert == "b" else -(min_y + total_h // 2))

    # 3) 逐段绘制（此时统一用 lt）
    cursor = 0
    for segment, is_e in runs:
        current = efont if is_e else font
        try:
            draw.text((x + dx + cursor, y + dy), segment, font=current, fill=fill, anchor="lt")
        except (TypeError, ValueError) as exc:  # pragma: no cover
            logger.warning("emoji 片段绘制失败（%r）：%s", segment, exc)
        box = draw.textbbox((0, 0), segment, font=current)
        cursor += box[2] - box[0]


def wrap_lines(draw, text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行（超长值不能溢出卡片）。"""
    if draw is None or not text:
        return [text]
    out: list[str] = []
    for raw in str(text).split("\n"):
        if not raw:
            out.append("")
            continue
        current = ""
        for ch in raw:
            candidate = current + ch
            try:
                width = draw.textlength(candidate, font=font)
            except (TypeError, ValueError):  # pragma: no cover
                width = len(candidate) * 12
            if current and width > max_width:
                out.append(current)
                current = ch
            else:
                current = candidate
        out.append(current)
    return out


# ---------------------------------------------------------------------------
# 画布
# ---------------------------------------------------------------------------
def new_canvas(title: str, status: str | None = None, *, online: bool = True):
    """新建画布：深色底 + 顶栏（标题 + 可选状态胶囊）。

    Returns:
        ``(image, draw)``。
    """
    image = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, WIDTH, HEADER_H], fill=PANEL)
    draw_text(draw, (MARGIN_X, HEADER_H // 2), title, load_font(TITLE_FS), fill=TEXT, anchor="lm")

    if status:
        pill_w, pill_h = 80, 30
        px, py = WIDTH - pill_w - MARGIN_X, (HEADER_H - pill_h) // 2
        draw.rounded_rectangle(
            [px, py, px + pill_w, py + pill_h], radius=15, fill=GREEN if online else RED
        )
        draw_text(
            draw,
            (px + pill_w // 2, py + pill_h // 2),
            status,
            load_font(LABEL_FS),
            fill=TEXT,
            anchor="mm",
        )
    return image, draw


def to_png(image) -> bytes:
    """编码成 PNG 字节。"""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_footer(draw, footer: str) -> None:
    draw_text(
        draw,
        (MARGIN_X, HEIGHT - FOOTER_H + 10),
        footer,
        load_font(FOOTER_FS),
        fill=SUB,
        anchor="lt",
    )


def default_footer() -> str:
    """默认页脚：更新时间（CST）。"""
    return f"更新时间: {timeutil.stamp()}"


# ---------------------------------------------------------------------------
# 渲染入口
# ---------------------------------------------------------------------------
def render_lines(
    title: str,
    lines: list[str],
    *,
    status: str | None = None,
    online: bool = True,
    footer: str | None = None,
) -> bytes:
    """把标题 + 若干行文本渲染成 PNG。

    超过卡片高度的行会被截断（并留一行「…」提示），不抛异常。
    """
    image, draw = new_canvas(title, status, online=online)
    font = load_font(BODY_FS)
    max_width = WIDTH - 2 * MARGIN_X
    max_lines = max(1, (HEIGHT - HEADER_H - FOOTER_H - MARGIN_Y) // LINE_H)

    wrapped: list[str] = []
    for line in lines:
        wrapped.extend(wrap_lines(draw, line, font, max_width))
    if len(wrapped) > max_lines:
        wrapped = wrapped[: max_lines - 1] + ["…"]

    y = HEADER_H + MARGIN_Y
    for line in wrapped:
        if line:
            draw_text(draw, (MARGIN_X, y), line, font, fill=TEXT if y == HEADER_H + MARGIN_Y else SUB)
        y += LINE_H

    _draw_footer(draw, footer if footer is not None else default_footer())
    return to_png(image)


def render_text(text: str, *, title: str = "StarWatt 星瓦", footer: str | None = None) -> bytes:
    """把一段多行文本渲染成 PNG（命令回复的通用形态）。"""
    return render_lines(title, str(text or "").split("\n"), footer=footer)


def render_stat_card(
    title: str,
    rows: list[tuple[str, str]],
    *,
    status: str | None = None,
    online: bool = True,
    footer: str | None = None,
) -> bytes:
    """渲染「键值对」卡片（标签左、值右，适合命令输出）。"""
    image, draw = new_canvas(title, status, online=online)
    label_font = load_font(BODY_FS)
    value_font = load_font(28)
    max_value_width = WIDTH - 2 * MARGIN_X - 220

    y = HEADER_H + MARGIN_Y + 10
    for label, value in rows:
        if y > HEIGHT - FOOTER_H - LINE_H:
            draw_text(draw, (MARGIN_X, y), "…", label_font, fill=SUB)
            break
        draw_text(draw, (MARGIN_X, y), label, label_font, fill=SUB)
        chunks = wrap_lines(draw, value, value_font, max_value_width)
        for index, chunk in enumerate(chunks):
            draw_text(
                draw,
                (WIDTH - MARGIN_X, y + index * LINE_H),
                chunk,
                value_font,
                fill=TEXT,
                anchor="rt",
            )
        y += LINE_H * max(1, len(chunks))

    _draw_footer(draw, footer if footer is not None else default_footer())
    return to_png(image)


# ---------------------------------------------------------------------------
# 飞书卡片 → PNG（QQ 富媒体与飞书私聊共用）
# ---------------------------------------------------------------------------
def _payload_of(card: Any) -> dict:
    if isinstance(card, dict) and isinstance(card.get("card"), dict):
        return card["card"]
    return card if isinstance(card, dict) else {}


def _node_text(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("content") or "")
    return str(node or "")


def _element_lines(element: Any) -> list[str]:
    """把一个卡片元素拆成若干行文本（``column_set`` 压成一行）。"""
    if not isinstance(element, dict):
        return []
    tag = element.get("tag")
    if tag == "div":
        return _node_text(element.get("text")).split("\n")
    if tag == "note":
        return [" · ".join(_node_text(item) for item in element.get("elements") or [])]
    if tag == "column_set":
        cells: list[str] = []
        for column in element.get("columns") or []:
            for child in (column or {}).get("elements") or []:
                text = " ".join(_element_lines(child)).strip()
                if text:
                    cells.append(text)
        return ["　".join(cells)] if cells else []
    if tag == "hr":
        return ["──────────"]
    return []


def card_lines(card: Any) -> list[str]:
    """卡片 → 纯文本行（渲染器与调试用）。

    📌 与 :func:`starwatt.notify.qq.card_to_text` 的区别：那个要的是
    「一段可直接发出去的纯文本」（还会压掉 URL，因为 QQ 群禁 URL），
    这里要的是**带层级的行**，方便逐行排版。
    """
    payload = _payload_of(card)
    lines: list[str] = []
    header = payload.get("header") or {}
    title = _node_text(header.get("title"))
    subtitle = _node_text(header.get("subtitle"))
    if title:
        lines.append(title)
    if subtitle:
        lines.append(subtitle)
    for element in payload.get("elements") or []:
        lines.extend(_element_lines(element))
    return [line for line in lines if line is not None]


def render_card(card: Any, *, footer: str | None = None) -> bytes:
    """把飞书卡片渲染成 PNG（页脚优先用卡片自带的 note）。"""
    payload = _payload_of(card)
    header = payload.get("header") or {}
    title = _node_text(header.get("title")) or "StarWatt 星瓦"

    body: list[str] = []
    subtitle = _node_text(header.get("subtitle"))
    if subtitle:
        body.append(subtitle)
    note = ""
    for element in payload.get("elements") or []:
        if isinstance(element, dict) and element.get("tag") == "note":
            note = " · ".join(_node_text(item) for item in element.get("elements") or [])
            continue
        body.extend(_element_lines(element))

    return render_lines(title, body, footer=footer if footer is not None else (note or None))
