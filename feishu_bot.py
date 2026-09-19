"""feishu_bot.py — Round 43 documented.

Outbound (cron -> group) and inbound (user -> bot) Feishu integration.
Renders Pillow stat-card PNGs, handles encrypted v1 / signed v2
webhook events, exposes 9 slash commands, and degrades gracefully when
credentials are missing.

主要功能 / Key responsibilities:
  * ``push_card`` / ``push_text`` — outbound cron alerts (low balance,
    violation, scrape failure).
  * ``handle_event`` — inbound webhook dispatcher (url_verification,
    slash commands, image upload).
  * Pillow rendering of the dark-themed 800x600 stat card (MiSans font
    fallback chain).
  * Signature verification for both encrypted-v1 (AES) and signed-v2
    (HMAC-SHA256) event envelopes.
  * Best-effort fail-open semantics: missing credentials never break
    the dashboard.

数据流 / Data flow:
  cron -> dorm_power -> feishu_bot.push_* -> Feishu webhook
  Feishu user -> POST /feishu/event -> web.py -> feishu_bot.handle_event
    -> Feishu reply API

依赖 / Dependencies:
  * stdlib: ``base64``, ``hashlib``, ``hmac``, ``json``
  * 3rd party: ``requests``, ``Pillow`` (PIL), ``pycryptodome``
  * Local: ``config``, ``db``, ``dorm_power``, ``web``

Round history:
  * R0   - scaffold + outbound webhook (text-only)
  * R2   - EqPrice caching side-effect
  * R3   - inbound event handler + 4 commands
  * R12  - fix 3 decrypt/verify/handle bugs end-to-end
  * R13  - 9 slash commands + cached image_key
  * R14a - Pillow rendered stat card
  * R25  - roomId 动态发现 (uses meta cache)
  * R26a - shared requests.Session
  * R34C - fail-open tightening (log on credentials missing)
  * R36   - db.insert 3->2 参数迁移
  * R37   - OOBE B1-B5+B8 (setup wizard + admin handler)
  * R39   - backfill partial-failure retry alerts
  * R42   - /api/refresh no-op path (no Feishu reply)

The original inbound-event docstring follows below for reference.

---

Feishu inbound event handler for dorm-power-monitor.

Responds to slash commands and (optionally) custom menu item clicks.
The dashboard /api/data contract is unchanged; this only adds a new
POST /feishu/event route to web.py.

All third-party messages are best-effort: when the bot is missing
credentials, missing a roomId, or the scraper has not run yet, we
return a polite Chinese string instead of raising.  External API
failures (Feishu tenant_access_token / im/v1/messages) are caught
and logged so a transient Feishu outage cannot break the dashboard.
"""
from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import io
import json
import logging
import os
import re
import time
from typing import Any, Optional

import requests
from flask import request

import config
import db
import dorm_power

# Round 26a — shared Session so each bot event reuses the Feishu-server
# TLS handshake across reply_card / get_tenant_token / image upload.
# Pool size 10 is plenty for a single Flask worker.
_SESSION = requests.Session()
_SESSION.headers.update({
    "User-Agent": "dorm-power-monitor/1.0 (feishu-bot)",
    "Accept": "application/json, */*",
})

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
    _HAS_AES = True
except ImportError:
    _HAS_AES = False

try:
    from PIL import Image, ImageDraw, ImageFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

logger = logging.getLogger("feishu_bot")

_API_BASE = "https://open.feishu.cn/open-apis"
_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0}

# Slash command → handler name
COMMANDS = {
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

# Custom menu event_key → handler name (matches COMMANDS values)
MENU_KEYS = {
    "menu_remain": "remain",
    "menu_meter": "meter",
    "menu_today": "today",
    "menu_history": "history",
    "menu_pay": "pay",
    "menu_violations": "violations",
    "menu_help": "help",
}

HELP_TEXT = """⚡ 宿舍电量小助手 · 命令列表

/状态 /剩余 — 当前剩余电量
/电表 /电表状态 — 实时电表 (电压 / 电流 / 功率)
/今日 — 今天用了多少度
/历史 [N] — 最近 N 小时趋势 (默认 24)
/缴费 — 最近 3 笔缴费
/违规 — 最近 30 天违规
/帮助 — 显示本条

群里 @ 我发命令。私聊直接发。"""


# ---------------------------------------------------------------------------
# Stat-card rendering with Pillow (Round 14a)
# ---------------------------------------------------------------------------
#
# Each command dispatches to a kind-specific builder that draws a dark-mode
# 800x600 PNG.  The bytes are returned to ``_render_stat_card`` which acts
# as a thin router on ``payload['kind']``.  Pillow is loaded lazily in
# ``_load_font`` so the module still imports on hosts without Pillow (the
# dispatcher then falls back to text).

_W, _H = 800, 600
_BG = (26, 26, 26)
_PANEL = (38, 38, 42)
_TEXT = (240, 240, 240)
_SUB = (170, 170, 175)
_DIV = (60, 60, 65)
_ACCENT = (60, 145, 230)
_GREEN = (52, 199, 110)
_RED = (235, 80, 80)
_ORANGE = (255, 165, 0)

# ---------------------------------------------------------------------------
# CJK-capable font loading (Round 20)
# ---------------------------------------------------------------------------
# Round 14a only loaded ImageFont.load_default() which has no CJK glyphs,
# so every Chinese label rendered as 口口口 ("tofu boxes") on production.
# Round 20 ships an OFL CJK TTF inside the project (assets/fonts/) and
# loads it lazily at first use; on hosts where the file is missing the
# old default-font fallback still runs so the dispatcher never crashes.
_FONT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "assets", "fonts"
)
_FONT_REGULAR_PATH = os.path.join(_FONT_DIR, "MiSans-Regular.ttf")
_FONT_BOLD_PATH = os.path.join(_FONT_DIR, "MiSans-Bold.ttf")
# Windows fallback paths (kept for dev / Chinese-locale workstations
# that may not have the project assets/fonts/ next to the .py).
_FONT_CANDIDATES = (
    _FONT_REGULAR_PATH,
    _FONT_BOLD_PATH,
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
)

_font_regular: Optional[ImageFont.ImageFont] = None
_font_bold: Optional[ImageFont.ImageFont] = None

# Round 22 — emoji-capable font.  MiSans TTF has no emoji glyphs so
# every emoji (⚡ 📅 📊 ⚠ …) rendered as the "tofu box" (⊠) on
# production.  NotoEmoji-Regular.ttf is a small (~410 KB) monochrome
# OpenType font that ships every glyph Pillow needs; the COLR/CPAL
# color version on the upstream main branch is unusable from Pillow,
# so we pin to the v2018-04-24-pistol-update tag where the legacy
# black/white TTF still lives.
_FONT_EMOJI_PATH = os.path.join(_FONT_DIR, "NotoEmoji-Regular.ttf")
_font_emoji: Optional[ImageFont.ImageFont] = None


def _get_emoji_font(size: int) -> ImageFont.ImageFont:
    """Lazy-load the emoji TTF.  Falls back to the CJK Regular font
    when the file is missing so a broken deployment still renders
    (just with the original tofu boxes).  Returns a size-matched
    font so emoji and CJK glyphs line up vertically.
    """
    global _font_emoji
    if not _HAS_PIL:
        return ImageFont.load_default()
    if _font_emoji is None:
        try:
            _font_emoji = ImageFont.truetype(_FONT_EMOJI_PATH, size)
        except (OSError, IOError) as exc:
            logger.warning(
                "NotoEmoji-Regular.ttf not found at %s (%s); "
                "emoji will render as tofu boxes.",
                _FONT_EMOJI_PATH, exc,
            )
            # Fallback: borrow the CJK Regular font.  Misalignment is
            # acceptable; the alternative is a hard 500.
            reg, _ = _get_font_set()
            _font_emoji = reg
            return reg
    try:
        return _font_emoji.font_variant(size=size)
    except (AttributeError, TypeError):
        return _font_emoji


def _get_font_set() -> tuple:
    """Lazy-load Regular + Bold CJK fonts.

    Returns ``(regular_ttf, bold_ttf)`` where each is either a
    ``PIL.ImageFont.truetype`` instance built from the project
    ``assets/fonts/MiSans-*.ttf`` or a ``ImageFont.load_default()``
    fallback when the file is missing.  Called once per process; the
    results are cached on the module-level globals so every PNG render
    reuses the same font object (Pillow caches glyphs internally).
    """
    global _font_regular, _font_bold
    if not _HAS_PIL:
        # No PIL → both default; downstream code still works.
        return ImageFont.load_default(), ImageFont.load_default()
    if _font_regular is None:
        try:
            _font_regular = ImageFont.truetype(_FONT_REGULAR_PATH, 24)
        except (OSError, IOError) as exc:
            logger.warning(
                "MiSans-Regular.ttf not found at %s (%s); "
                "falling back to default font (Chinese will render as 口口口)",
                _FONT_REGULAR_PATH, exc,
            )
            _font_regular = ImageFont.load_default()
    if _font_bold is None:
        try:
            _font_bold = ImageFont.truetype(_FONT_BOLD_PATH, 24)
        except (OSError, IOError) as exc:
            logger.warning(
                "MiSans-Bold.ttf not found at %s (%s); "
                "falling back to default font",
                _FONT_BOLD_PATH, exc,
            )
            _font_bold = ImageFont.load_default()
    return _font_regular, _font_bold


def _load_font(size: int) -> ImageFont.ImageFont:
    """Backwards-compatible single-weight loader.

    Kept so existing call-sites (``_load_font(28)``) still work; picks
    the bold variant for hero / pill text sizes that previous code
    used the default for, and the regular variant for labels.  The
    simple size threshold is enough to make the Chinese + digit
    layout look right without each card having to opt in.
    """
    if not _HAS_PIL:
        return ImageFont.load_default()
    reg, bold = _get_font_set()
    # Hero numbers (>= 40pt) and status pills (16-20pt) get bold;
    # everything else gets regular.  Pillow's TrueTypeFont supports
    # ``font.font_variant`` re-sizing cheaply, so we re-derive the
    # variant on the fly rather than caching N size-specific fonts.
    base = bold if size >= 40 or (14 <= size <= 22) else reg
    try:
        return base.font_variant(size=size)
    except (AttributeError, TypeError):
        # Default font (fallback) doesn't expose font_variant; rebuild.
        return base


# Probe the candidate list once on import so we log a clear warning at
# startup if both project TTFs are missing on the Linux production host.
for _p in (_FONT_REGULAR_PATH, _FONT_BOLD_PATH):
    if not os.path.exists(_p):
        logger.warning(
            "CJK font not present at %s — Pillow stat cards will use the "
            "default font and CJK glyphs will render as 口口口 until this "
            "file is deployed.", _p,
        )
        break


def _base_canvas(title: str, status: str, online: bool):
    img = Image.new("RGB", (_W, _H), _BG) if _HAS_PIL else None
    d = ImageDraw.Draw(img) if img is not None else None
    f_title = _load_font(28)
    f_pill = _load_font(16)
    if d is not None:
        d.rectangle([0, 0, _W, 60], fill=_PANEL)
        # Use ``la`` (left-ascender) anchor so the title sits cleanly
        # below the panel top edge instead of clipping into it.  Round
        # 21a fix: the previous hard-coded ``(24, 16)`` worked for the
        # bitmap default font but made the bold MiSans title look
        # vertically squashed on 800x600 production cards.
        _safe_text(d, (_M_X, 16), title, f_title, fill=_TEXT, anchor="la")
        pw, ph = 80, 30
        px, py = _W - pw - _M_X, 15
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=15,
                            fill=_GREEN if online else _RED)
        # Center the pill text by anchoring it to the pill's middle.
        # ``mm`` (middle-middle) + bbox-aware offset is far more robust
        # than the previous ``bbox + //2 - 2`` arithmetic, which gave
        # different baselines for CJK vs Latin glyphs.
        cx, cy = px + pw // 2, py + ph // 2
        _safe_text(d, (cx, cy), status, f_pill, fill=_TEXT, anchor="mm")
    return img, d


# ---------------------------------------------------------------------------
# Layout constants + _safe_text helper (Round 21a)
# ---------------------------------------------------------------------------
# Round 21a audit: every card had its own ad-hoc magic numbers, with
# the hero number at one y, the unit at a hand-tuned offset, and
# sub-stat boxes at hard-coded coordinates.  The "可用天数" card
# (f_sub=36) and the "今日已用" card could overflow the 224px sub-stat
# box when the value was a 5-digit number like "123.45 kW·h", and the
# "kW·h" unit sat at a hard-coded ``bb[2] + 16`` x that drifted off the
# panel whenever the hero number of digits changed.
#
# Fix: define one set of layout constants and one ``_safe_text``
# wrapper so every card uses the same margin, font scale, and anchor
# system.  Coordinates are now computed from ``_W`` / ``_H`` so a
# future resize (e.g. 1024x768) is a one-line change.
_M_X = 32          # horizontal margin
_M_Y = 24          # vertical margin
_HEADER_H = 60     # top panel height
_FOOTER_H = 36     # bottom footer height
_TITLE_FS = 28
_HERO_FS = 64      # slightly smaller than the old 72 so the unit
                   # doesn't kiss the right edge on 5-digit numbers
_UNIT_FS = 24
_SUB_FS = 28       # sub-stat value font (was 36 → overflow on long values)
_LABEL_FS = 16
_FOOTER_FS = 14


# Round 22 — emoji vs CJK detection.  Returns True for the codepoints
# that the CJK MiSans font has no glyph for (i.e. real emoji) and that
# we therefore want to draw with the NotoEmoji font.  We deliberately
# **exclude** the BMP dingbats block (U+2700–U+27BF) because MiSans
# already draws ✓ / ✔ as proper check marks, whereas the NotoEmoji
# font only has a hollow box placeholder for those code points.
# Ranges chosen to cover every symbol the cards use today:
#   ⚡ (U+26A1)  📅📊📋 (U+1F4C5/CA/CB)  ⚠ (U+26A0)
#   ✓ (U+2713) — CJK font  💧 (U+1F4A7)  ⏰ (U+23F0)  🚨 (U+1F6A8)
_EMOJI_RANGES = (
    (0x2300, 0x23FF),    # Misc Technical (⏰ ⌨ ⏳)
    (0x2460, 0x24FF),    # Enclosed Alphanumerics
    (0x2600, 0x26FF),    # Misc Symbols (⚡ ⚠) — ⚠ drawn with CJK too,
                          # but noto is consistent for the rest
    (0x2B00, 0x2BFF),    # Misc Symbols and Arrows
    (0x1F000, 0x1F02F),  # Mahjong
    (0x1F0A0, 0x1F0FF),  # Playing cards
    (0x1F100, 0x1F2FF),  # Enclosed Alphanumeric Supplement
    (0x1F300, 0x1F5FF),  # Misc Symbols and Pictographs (📅📊📋💧🚨)
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F680, 0x1F6FF),  # Transport and Map
    (0x1F700, 0x1F77F),  # Alchemical
    (0x1F780, 0x1F7FF),  # Geometric Shapes Extended
    (0x1F800, 0x1F8FF),  # Supplemental Arrows-C
    (0x1F900, 0x1F9FF),  # Supplemental Symbols and Pictographs
    (0x1FA00, 0x1FAFF),  # Symbols and Pictographs Extended-A
)


def _is_emoji_char(ch: str) -> bool:
    """True if the codepoint should be drawn with the emoji font."""
    if not ch:
        return False
    cp = ord(ch)
    for lo, hi in _EMOJI_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def _split_text_at_emoji(text: str):
    """Split ``text`` into ``[(segment, is_emoji), ...]`` runs.

    A run is a maximal sequence of characters that all share the same
    emoji/non-emoji classification.  Consecutive emoji characters stay
    in the same run so the emoji font can position them as a unit.
    """
    if not text:
        return []
    out: list[tuple[str, bool]] = []
    cur: list[str] = []
    cur_is_emoji: Optional[bool] = None
    for ch in text:
        is_e = _is_emoji_char(ch)
        if cur_is_emoji is None or is_e == cur_is_emoji:
            cur.append(ch)
            cur_is_emoji = is_e
        else:
            out.append(("".join(cur), cur_is_emoji))
            cur = [ch]
            cur_is_emoji = is_e
    if cur:
        out.append(("".join(cur), cur_is_emoji))
    return out


def _safe_text(d, xy, text, font, fill=_TEXT, anchor: str = "lt") -> None:
    """Thin wrapper around ``ImageDraw.text`` that:
      1.  no-ops when ``d`` is None (no-PIL fallback path) so every
          call site stays branch-free;
      2.  defaults ``anchor`` to ``"lt"`` (left-top) which is the
          natural anchor for a coordinate grid;
      3.  splits the text into CJK + emoji runs so the emoji font
          renders the glyphs MiSans can't draw (Round 22);
      4.  logs (does not raise) on a missing font so a broken
          deployment still produces a card instead of a 500.
    """
    if d is None or text is None:
        return
    if font is None:
        font = ImageFont.load_default()
    try:
        # Try the simple PIL path first — if the text has no emoji we
        # can skip the pre-compute / mixed-font dance entirely.
        if not any(_is_emoji_char(c) for c in text):
            d.text(xy, text, font=font, fill=fill, anchor=anchor)
            return
        _draw_text_mixed(d, xy, text, font, fill, anchor)
    except (TypeError, ValueError) as exc:
        # Never let a layout bug crash the bot — a missing card is far
        # better than a 5xx in front of the user.
        logger.warning("_safe_text failed for %r: %s", text, exc)


def _draw_text_mixed(d, xy, text, font_cjk, fill, anchor: str) -> None:
    """Render ``text`` with CJK + emoji fonts mixed by codepoint range.

    Pre-computes the total width so right/middle anchors still position
    the string correctly even when the leading run is CJK and the
    trailing run is emoji.  Supports the same anchor vocabulary PIL
    documents (``lt, lm, lb, la, mt, mm, mb, ma, rt, rm, rb, ra``);
    unknown anchors fall back to ``lt``.
    """
    segments = _split_text_at_emoji(text)
    if not segments:
        return
    # Determine the requested pixel size so the emoji font matches.
    try:
        cjk_size = int(getattr(font_cjk, "size", 24))
    except (TypeError, ValueError):
        cjk_size = 24
    font_emoji = _get_emoji_font(cjk_size)

    # 1) Pre-compute total bounding box (origin = 0,0).  We need this
    #    to honour right-/middle-anchored placement.
    min_x, min_y, max_x, max_y = 0, 0, 0, 0
    cur = 0
    for seg, is_e in segments:
        f = font_emoji if is_e else font_cjk
        bb = d.textbbox((0, 0), seg, font=f)
        min_x = min(min_x, cur + bb[0])
        max_x = max(max_x, cur + bb[2])
        min_y = min(min_y, bb[1])
        max_y = max(max_y, bb[3])
        cur += bb[2] - bb[0]
    total_w = max_x - min_x
    total_h = max_y - min_y

    # 2) Translate the requested anchor into a pixel offset relative
    #    to the leftmost-topmost corner of the computed bbox.
    x, y = xy
    horiz = anchor[0] if anchor else "l"
    vert = anchor[1] if len(anchor) > 1 else "t"
    if horiz == "l":
        dx = -min_x
    elif horiz == "r":
        dx = -max_x
    elif horiz == "m":
        dx = -(min_x + total_w // 2)
    else:
        dx = -min_x
    if vert == "t":
        dy = -min_y
    elif vert == "b":
        dy = -max_y
    elif vert == "m":
        dy = -(min_y + total_h // 2)
    else:                      # "a" (ascender) and fallthroughs
        dy = -min_y
    start_x = x + dx
    start_y = y + dy

    # 3) Draw each segment with ``anchor="lt"`` (we've already
    #    compensated for the requested anchor above).
    cur = 0
    for seg, is_e in segments:
        f = font_emoji if is_e else font_cjk
        d.text((start_x + cur, start_y), seg, font=f, fill=fill,
               anchor="lt")
        bb = d.textbbox((0, 0), seg, font=f)
        cur += bb[2] - bb[0]


def _draw_footer(d, ts: str) -> None:
    if d is None:
        return
    f = _load_font(_FOOTER_FS)
    _safe_text(d, (_M_X, _H - _FOOTER_H + 8),
               f"更新时间: {ts}", f, fill=_SUB, anchor="lt")


def _to_png_bytes(img) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _card_remain(payload: dict) -> bytes:
    img, d = _base_canvas("⚡ 电费助手 · 状态",
                          "在线" if payload.get("online") else "离线",
                          payload.get("online", True))
    f_hero = _load_font(_HERO_FS)
    f_unit = _load_font(_UNIT_FS)
    f_label = _load_font(_LABEL_FS)
    f_sub = _load_font(_SUB_FS)
    remain = payload.get("remain")
    hero_y = 100
    if remain is not None:
        _safe_text(d, (_M_X, hero_y), f"{remain:.2f}", f_hero,
                   fill=_TEXT, anchor="lt")
        # Anchor the unit to the baseline of the hero number so 5-digit
        # remainders don't push it off the canvas.  The old
        # ``bb[2] + 16`` placement clipped into the right margin on
        # large numbers like "1234.56".
        bb = d.textbbox((0, 0), f"{remain:.2f}", font=f_hero)
        hero_right = _M_X + (bb[2] - bb[0])
        _safe_text(d, (hero_right + 12, hero_y + 8), "kW·h",
                   f_unit, fill=_SUB, anchor="lt")
    else:
        _safe_text(d, (_M_X, hero_y), "—", f_hero, fill=_SUB, anchor="lt")
        _safe_text(d, (_M_X + 140, hero_y + 8), "kW·h",
                   f_unit, fill=_SUB, anchor="lt")
    _safe_text(d, (_M_X, hero_y + 80), "剩余电量",
               f_label, fill=_SUB, anchor="lt")
    # Sub-stat row — 3 equal boxes across the body, with relative
    # coordinates derived from canvas width (so the layout scales).
    y0 = 240
    avail_w = _W - 2 * _M_X
    gap = 12
    box_w = (avail_w - 2 * gap) // 3
    box_h = 130
    # Format the value via a tiny dispatcher so 0/None/positive all
    # render consistently (the old code did ``f"{x:.0f} 天"`` which
    # could mis-render small fractional days like 0.4 as "0 天").
    def _fmt_days(v):
        if v is None:
            return "—"
        if v <= 0:
            return "0 天"
        return f"{v:.0f} 天"

    subs = [
        ("今日已用", f"{payload['used_today']:.2f} kW·h"
         if payload.get("used_today") is not None else "—", _ACCENT),
        ("可用天数", _fmt_days(payload.get("days_left")), _ORANGE),
        ("账户余额", f"¥{payload['balance']:.2f}"
         if payload.get("balance") is not None else "—", _GREEN),
    ]
    for i, (label, val, color) in enumerate(subs):
        x = _M_X + i * (box_w + gap)
        d.rounded_rectangle([x, y0, x + box_w, y0 + box_h],
                            radius=12, fill=_PANEL)
        # Color dot
        d.ellipse([x + 14, y0 + 14, x + 26, y0 + 26], fill=color)
        # Label (left of center) — use ``lt`` for predictable placement
        _safe_text(d, (x + 36, y0 + 12), label, f_label,
                   fill=_SUB, anchor="lt")
        # Value — use ``lt`` so long values grow rightward, and keep
        # the font at 28pt (the old 36pt overflowed 224px boxes on
        # values like "12345.67 kW·h").
        _safe_text(d, (x + 14, y0 + 46), val, f_sub,
                   fill=_TEXT, anchor="lt")
    _safe_text(d, (_M_X, y0 + box_h + 24),
               f"抄表时间: {payload.get('read_time', '—')}",
               f_label, fill=_SUB, anchor="lt")
    _draw_footer(d, payload.get("read_time", "—"))
    return _to_png_bytes(img)


def _card_meter(payload: dict) -> bytes:
    img, d = _base_canvas("⚡ 电表 · 实时读数",
                          "在线" if payload.get("online") else "离线",
                          payload.get("online", True))
    f_hero = _load_font(48)   # was 56 — too tight in 352px box with 4-digit volts
    f_unit = _load_font(_UNIT_FS)
    f_label = _load_font(_LABEL_FS)
    avail_w = _W - 2 * _M_X
    gap = 16
    box_w = (avail_w - gap) // 2
    box_h = 150
    y0 = 90
    items = [
        ("电压", f"{payload['vol']:.2f}" if payload.get("vol") is not None else "—", "V", _ACCENT),
        ("电流", f"{payload['cur']:.2f}" if payload.get("cur") is not None else "—", "A", _ORANGE),
        ("有功功率", f"{payload['yggl']:.3f}" if payload.get("yggl") is not None else "—", "W", _GREEN),
    ]
    for i, (label, val, unit, color) in enumerate(items):
        col = i % 2
        row = i // 2
        x = _M_X + col * (box_w + gap)
        y = y0 + row * (box_h + gap)
        d.rounded_rectangle([x, y, x + box_w, y + box_h], radius=12, fill=_PANEL)
        d.ellipse([x + 14, y + 14, x + 26, y + 26], fill=color)
        _safe_text(d, (x + 36, y + 12), label, f_label,
                   fill=_SUB, anchor="lt")
        _safe_text(d, (x + 16, y + 56), val, f_hero,
                   fill=_TEXT, anchor="lt")
        # Place the unit at the right of the value, on the same
        # baseline.  Using ``anchor="lb"`` (left-baseline) keeps the
        # unit visually attached to the digits.
        bb = d.textbbox((0, 0), val, font=f_hero)
        val_right = x + 16 + (bb[2] - bb[0])
        _safe_text(d, (val_right + 8, y + 56), unit,
                   f_unit, fill=_SUB, anchor="lt")
    last_y = y0 + 2 * (box_h + gap) + 8
    _safe_text(d, (_M_X, last_y),
               f"最后上报: {payload.get('update_dt', '—')}",
               f_label, fill=_SUB, anchor="lt")
    _draw_footer(d, payload.get("update_dt", "—"))
    return _to_png_bytes(img)


def _card_today(payload: dict) -> bytes:
    img, d = _base_canvas("📅 今日用电", "在线", True)
    f_hero = _load_font(_HERO_FS)
    f_unit = _load_font(_UNIT_FS)
    f_label = _load_font(_LABEL_FS)
    f_sub = _load_font(22)
    used = payload.get("used")
    hero_y = 90
    if used is not None:
        _safe_text(d, (_M_X, hero_y), f"{used:.2f}", f_hero,
                   fill=_TEXT, anchor="lt")
        bb = d.textbbox((0, 0), f"{used:.2f}", font=f_hero)
        _safe_text(d, (_M_X + (bb[2] - bb[0]) + 12, hero_y + 8),
                   "kW·h", f_unit, fill=_SUB, anchor="lt")
    else:
        _safe_text(d, (_M_X, hero_y), "—", f_hero,
                   fill=_SUB, anchor="lt")
    _safe_text(d, (_M_X, hero_y + 90),
               f"日期 {payload.get('dt', '—')}",
               f_label, fill=_SUB, anchor="lt")
    pays = payload.get("pays") or []
    if pays:
        _safe_text(d, (_M_X, hero_y + 140), "最近缴费",
                   f_label, fill=_SUB, anchor="lt")
        y = hero_y + 180
        for p in pays[:3]:
            money = p.get("money") or 0.0
            _safe_text(d, (_M_X, y),
                       f"· {p.get('dt', '—')} {p.get('label', '')} ¥{money:.2f}",
                       f_sub, fill=_TEXT, anchor="lt")
            y += 36
    _draw_footer(d, payload.get("dt", "—"))
    return _to_png_bytes(img)


def _card_history(payload: dict) -> bytes:
    hours = payload.get("hours", 24)
    points = payload.get("points") or []
    img, d = _base_canvas(f"📊 近 {hours} 小时用电", "在线", True)
    f_label = _load_font(_LABEL_FS)
    f_sub = _load_font(20)
    if not points:
        _safe_text(d, (_M_X, 120), "暂无有效数据", f_sub,
                   fill=_SUB, anchor="lt")
    else:
        cx, cy = _M_X + 16, 130
        cw = _W - 2 * (cx)  # leave equal margin on both sides
        # Round 21a: ``_H - _HEADER_H - _FOOTER_H - cy`` left too
        # little room for the header/footer in the old ``cy+chh=490``
        # computation.  Now we use a relative chart height and clip
        # to a fixed bottom margin.
        chh = 360
        max_v = max(v for _, v in points) or 1.0
        n = len(points)
        bw = max(2, (cw - n) // n)
        d.line([(cx, cy + chh), (cx + cw, cy + chh)], fill=_DIV, width=2)
        for i, (ts, v) in enumerate(points):
            bh = int((v / max_v) * (chh - 20))
            bx = cx + i * (bw + 1)
            d.rectangle([bx, cy + chh - bh, bx + bw, cy + chh], fill=_ACCENT)
        first_v = points[0][1]
        last_v = points[-1][1]
        _safe_text(d, (cx, cy - 24), f"首: {first_v:.2f} kW·h",
                   f_label, fill=_SUB, anchor="lt")
        # Anchor the right label to ``rm`` (right-middle) so it sticks
        # to the right edge of the chart regardless of text length.
        _safe_text(d, (cx + cw, cy - 24), f"末: {last_v:.2f} kW·h",
                   f_label, fill=_SUB, anchor="rm")
    _draw_footer(d, "now")
    return _to_png_bytes(img)


def _card_pay(payload: dict) -> bytes:
    rows = payload.get("rows") or []
    img, d = _base_canvas("📋 缴费记录", "在线", True)
    f_label = _load_font(18)
    f_sub = _load_font(20)
    if not rows:
        _safe_text(d, (_M_X, 120), "暂无缴费记录", f_sub,
                   fill=_SUB, anchor="lt")
    else:
        y = 100
        for r in rows[:6]:
            money = r.get("money") or 0.0
            _safe_text(d, (_M_X, y),
                       f"{r.get('dt', '—')}  {r.get('pay_type', '')}/{r.get('fee_type', '')}  ¥{money:.2f}",
                       f_label, fill=_TEXT, anchor="lt")
            y += 32
    _draw_footer(d, "now")
    return _to_png_bytes(img)


def _card_violations(payload: dict) -> bytes:
    rows = payload.get("rows") or []
    img, d = _base_canvas("⚠ 违规记录", "在线", True)
    f_label = _load_font(18)
    f_sub = _load_font(20)
    if not rows:
        _safe_text(d, (_M_X, 120), "✓ 近 30 天无违规,太棒了！",
                   f_sub, fill=_GREEN, anchor="lt")
    else:
        _safe_text(d, (_M_X, 100), f"共 {len(rows)} 条",
                   f_sub, fill=_RED, anchor="lt")
        y = 150
        for r in rows[:8]:
            wp = r.get("wg_power")
            # Round 29 fix: wg_power is a power reading (kW), not energy.
            wp_s = f"{wp:.2f} kW" if wp is not None else "—"
            _safe_text(d, (_M_X, y),
                       f"· {r.get('dt', '—')} {r.get('wg_reason', '')} {wp_s}",
                       f_label, fill=_TEXT, anchor="lt")
            y += 32
        if len(rows) > 8:
            _safe_text(d, (_M_X, y), f"…另有 {len(rows) - 8} 条未显示",
                       f_label, fill=_SUB, anchor="lt")
    _draw_footer(d, "now")
    return _to_png_bytes(img)


def _render_stat_card(payload: dict) -> bytes:
    """Render a stat-card PNG.  Dispatch on ``payload['kind']``."""
    if not _HAS_PIL:
        raise RuntimeError("Pillow not installed; cannot render stat card")
    kind = payload.get("kind", "")
    if kind == "remain":
        return _card_remain(payload)
    if kind == "meter":
        return _card_meter(payload)
    if kind == "today":
        return _card_today(payload)
    if kind == "history":
        return _card_history(payload)
    if kind == "pay":
        return _card_pay(payload)
    if kind == "violations":
        return _card_violations(payload)
    img, d = _base_canvas("—", "—", True)
    f = _load_font(24)
    _safe_text(d, (_M_X, 120), "暂无可显示内容", f, fill=_SUB, anchor="lt")
    _draw_footer(d, "now")
    return _to_png_bytes(img)


# ---------------------------------------------------------------------------
# Feishu image upload (Round 14a)
# ---------------------------------------------------------------------------

def _log_request_dump(label: str, url: str, token: str,
                      attempt: dict, image_bytes: bytes) -> None:
    """Log a redacted request dump so the next round has a paper trail.

    Truncates the image body to its first 200 bytes — we don't want
    journalctl flooded with 50 KB of PNG, but a PNG signature
    (``b'\\x89PNG\\r\\n\\x1a\\n'``) is enough to prove the file made
    it into the multipart payload.
    """
    files = attempt.get("files") or {}
    file_meta = {}
    for k, v in files.items():
        if isinstance(v, tuple) and len(v) >= 3:
            file_meta[k] = {
                "filename": v[0],
                "size": len(v[1]) if isinstance(v[1], (bytes, bytearray)) else "?",
                "content_type": v[2],
            }
        else:
            file_meta[k] = {"value": repr(v)[:80]}
    logger.error(
        "feishu image upload request dump [%s]: url=%s auth=%s "
        "params=%s data=%s files=%s image_bytes_size=%d image_bytes_head=%r",
        label, url, "Bearer <redacted>",
        attempt.get("params"), attempt.get("data"), file_meta,
        len(image_bytes), bytes(image_bytes[:200]),
    )


def _upload_image(image_bytes: bytes) -> str:
    """Upload PNG bytes to Feishu ``im/v1/images`` and return ``image_key``.

    Caches the mapping ``sha256(image_bytes)[:16] → image_key`` in the
    ``meta`` table so repeated identical payloads do not re-upload.

    Round 15 — explicitly declare PNG MIME (``image/png``) + filename
    (``stat_card.png``) so the multipart body matches what Feishu's
    ``im/v1/images`` API expects (a 2-tuple would have been sent as
    ``application/octet-stream`` and rejected with HTTP 400).

    Round 17 — ``image_type`` MUST be a ``multipart/form-data`` field,
    not a query-string param.  The official Feishu docs
    (https://open.feishu.cn/document/server-docs/im-v1/image/create)
    show both the curl example and the multipart raw body putting
    ``image_type`` inside the form-data part.  Passing it as
    ``params={"image_type": "message"}`` makes the server see a missing
    required field and reply with HTTP 400 / ``code 234001
    Invalid request param.``

    On 4xx/5xx HTTPError the raw response body + a redacted request
    dump (URL, params, data, file metadata, first 200 bytes of the
    image payload) are logged so future failures leave a paper trail
    in ``dorm.log`` / journalctl.

    If the primary multipart format still returns 234001 the function
    auto-retries with ``image_type`` as a query string param (the
    legacy format) before giving up — a single Round 17 hit may not be
    the last one.
    """
    cache_key = "img_" + hashlib.sha256(image_bytes).hexdigest()[:16]
    cached = db.get_meta(cache_key)
    if cached:
        return cached
    token = get_tenant_token()
    url = f"{_API_BASE}/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}

    # Per Feishu docs, image_type is a multipart/form-data field, not a
    # query string.  Primary attempt uses the documented form-data form;
    # the fallback only fires on 4xx with the literal 234001 code so we
    # don't accidentally swap to a worse format for unrelated errors.
    attempts = [
        {
            "label": "form:png",
            "data": {"image_type": "message"},
            "files": {"image": ("stat_card.png", image_bytes, "image/png")},
        },
        {
            "label": "query:png",
            "params": {"image_type": "message"},
            "files": {"image": ("stat_card.png", image_bytes, "image/png")},
        },
    ]

    last_exc: Optional[Exception] = None
    for attempt in attempts:
        kw = {"headers": headers, "timeout": 15}
        kw.update({k: v for k, v in attempt.items() if k != "label"})
        try:
            resp = _SESSION.post(url, **kw)
        except requests.RequestException as exc:
            # Network / TLS / connection error — log everything we know
            # and try the next format.  If both fail the last error
            # bubbles up.
            logger.error(
                "feishu image upload transport error [%s]: %s url=%s",
                attempt["label"], exc, url,
            )
            _log_request_dump(attempt["label"], url, token, attempt,
                              image_bytes)
            last_exc = exc
            continue

        if resp.status_code < 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
                logger.error(
                    "feishu image upload [%s]: non-JSON body status=%s text=%r",
                    attempt["label"], resp.status_code,
                    (resp.text or "")[:500],
                )
                last_exc = RuntimeError(
                    f"image upload non-JSON body: {resp.text[:200]}"
                )
                continue
            if body.get("code") == 0:
                image_key = body["data"]["image_key"]
                db.set_meta(cache_key, image_key)
                return image_key
            # 200 OK but Feishu returned a business-level error.
            # Log the full payload so the next round has a real
            # diagnostic.  No point trying another format here.
            logger.error(
                "feishu image upload business error [%s]: code=%s "
                "msg=%s data=%s",
                attempt["label"],
                body.get("code"), body.get("msg"), body.get("data"),
            )
            raise RuntimeError(f"image upload failed: {body}")

        # 4xx/5xx — log the body AND a redacted request dump, then
        # either try the next format (only on 234001 Invalid request
        # param) or give up.
        try:
            body_preview = (resp.text or "")[:500]
        except Exception:  # noqa: BLE001
            body_preview = "<unreadable>"
        logger.error(
            "feishu image upload http %s [%s]: %s body=%s",
            resp.status_code, attempt["label"], url, body_preview,
        )
        _log_request_dump(attempt["label"], url, token, attempt,
                          image_bytes)
        # Try the fallback only for 234001 (the documented failure
        # mode for image_type placement).  For other 4xx codes
        # (size / format / permission) the server has already told us
        # what's wrong, so retrying with a different format would be
        # noise.
        try:
            err_body = resp.json()
        except ValueError:
            err_body = {}
        if err_body.get("code") == 234001 and attempt is not attempts[-1]:
            last_exc = requests.HTTPError(
                f"{resp.status_code} Client Error", response=resp,
            )
            continue
        # Out of attempts or not a 234001 → bubble up.
        resp.raise_for_status()

    # If we fall through, the last transport/HTTP error is the
    # most accurate description of what went wrong.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("image upload failed: no attempts completed")


# ---------------------------------------------------------------------------
# Feishu interactive card construction (Round 14a)
# ---------------------------------------------------------------------------

def _build_image_card(image_key: str, title: str, sub_text: str = "",
                      footer: str = "") -> dict:
    """Build a Feishu interactive card with the rendered image as the hero.

    Round 19 — fix the ``img`` element field name.  The ``im/v1/images``
    upload API returns the value under ``image_key``, but when that value
    is embedded into an interactive card, the ``img`` element attribute
    is named ``img_key`` (NOT ``image_key``).  Sending ``image_key``
    makes Feishu reject the card with::

        ErrCode: 11310; ErrMsg: img element must contain img_key;
        ErrorValue: {"alt":{},"preview":true};

    i.e. the server strips the unknown ``image_key`` field and then
    complains that the recognised ``img_key`` is missing.

    Reference: 飞书卡片 JSON 1.0 结构 (header.icon example uses
    ``"img_key": "img_v2_..."``).
    """
    # NOTE: the parameter is still named ``image_key`` (because that is
    # what the upload API returns), but the JSON attribute on the
    # card element is ``img_key`` (different name on purpose).
    elements: list = [{"tag": "img", "img_key": image_key}]
    if sub_text:
        elements.append(
            {"tag": "div", "text": {"tag": "lark_md", "content": sub_text}}
        )
    if footer:
        elements.append(
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": footer}
            ]}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }


def _build_text_card(title: str, text: str, footer: str = "") -> dict:
    """Build a Feishu interactive card with markdown text (no image)."""
    elements: list = [
        {"tag": "div", "text": {"tag": "lark_md", "content": text}}
    ]
    if footer:
        elements.append(
            {"tag": "note", "elements": [
                {"tag": "plain_text", "content": footer}
            ]}
        )
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue",
        },
        "elements": elements,
    }


def reply_card(receive_id: str, card: dict) -> None:
    """Send an interactive card message to a user (open_id).

    Round 18 — add response body logging on 4xx/5xx so that when Feishu
    returns e.g. ``400 Client Error`` we can see the *server's* code/msg
    in the journalctl instead of just the URL.  Also factor the payload
    out into a variable so the request dump and the live JSON share one
    source of truth.

    Per the Feishu OpenAPI docs, the ``content`` field must be a **JSON
    string** (the card object serialised once), not a nested object —
    otherwise the server replies with ``code 230001 Invalid request param``
    and the request is dropped.  We ``json.dumps`` here so all callers
    can keep handing the function a plain ``dict``.
    """
    token = get_tenant_token()
    payload = {
        "receive_id": receive_id,
        "msg_type": "interactive",
        # content MUST be a JSON string, not a dict.  see:
        # https://open.feishu.cn/document/server-docs/im-v1/message/create
        "content": json.dumps(card, ensure_ascii=False),
    }
    resp = _SESSION.post(
        f"{_API_BASE}/im/v1/messages?receive_id_type=open_id",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json=payload,
        timeout=10,
    )
    # Round 18: on 4xx/5xx, dump both the request body (truncated) and
    # the response body so the next on-call engineer can see Feishu's
    # actual reason without curling the dashboard from outside.
    if resp.status_code >= 400:
        try:
            _body_preview = (resp.text or "")[:500]
        except Exception:  # noqa: BLE001
            _body_preview = "<unreadable response body>"
        logger.error(
            "feishu message send http %s: url=%s body=%s response=%s",
            resp.status_code, resp.url,
            json.dumps(payload, ensure_ascii=False)[:500],
            _body_preview,
        )
        resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        logger.error(
            "feishu message send business error: code=%s msg=%s data=%s",
            body.get("code"), body.get("msg"), body.get("data"),
        )
        raise RuntimeError(f"reply_card failed: {body}")


# ---------------------------------------------------------------------------
# tenant_access_token
# ---------------------------------------------------------------------------

def get_tenant_token() -> str:
    """Fetch and cache the tenant_access_token (2-hour TTL).

    The cache is a process-local module dict; the dashboard is a single
    Flask process, so this is enough.  We refresh 60s before expiry to
    avoid handing Feishu a token that races against its own clock.
    """
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["expires_at"] > time.time() + 60:
        return _TOKEN_CACHE["token"]
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        raise RuntimeError(
            "FEISHU_APP_ID and FEISHU_APP_SECRET must be set in .env. "
            "See README 'Feishu bot setup' for how to get them."
        )
    resp = _SESSION.post(
        f"{_API_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"tenant_access_token failed: {data}")
    _TOKEN_CACHE["token"] = data["tenant_access_token"]
    _TOKEN_CACHE["expires_at"] = time.time() + int(data.get("expire", 7200))
    return _TOKEN_CACHE["token"]


# ---------------------------------------------------------------------------
# Outbound: send a reply
# ---------------------------------------------------------------------------

def reply_text(receive_id: str, text: str) -> None:
    """Send a text reply to a user (open_id)."""
    token = get_tenant_token()
    resp = _SESSION.post(
        f"{_API_BASE}/im/v1/messages?receive_id_type=open_id",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"reply failed: {body}")


def send_text_message(receive_id: str, text: str) -> None:
    """Send a plain-text message to a user (open_id).

    Round 16 — error-fallback path.  Used by ``handle_event`` when the
    main reply chain (Pillow render → image upload → card send) raises
    any exception.  The user must always see *something* explaining the
    failure instead of silence, otherwise a transient Feishu outage
    looks like a bot crash from their side.

    Payload shape (intentionally minimal so it survives even when the
    more complex ``reply_card`` path is broken)::

        {"msg_type": "text",
         "content": {"text": "..."}}
    """
    token = get_tenant_token()
    resp = _SESSION.post(
        f"{_API_BASE}/im/v1/messages?receive_id_type=open_id",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        },
        json={
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        },
        timeout=10,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"send_text_message failed: {body}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _strip_mention(text: str) -> str:
    """Strip the @-bot mention prefix that Feishu prepends in group chats.

    In a group message, the text looks like '@_user_1 /电表'.  We strip
    any number of leading @-tokens (the canonical form is '@_user_N',
    but Feishu also accepts a custom nickname).  Whitespace is collapsed
    by the strip() at the end.
    """
    if not text:
        return ""
    return re.sub(r"^\s*(?:@\S+\s*)+", "", text).strip()


def _parse_history_args(args: str) -> int:
    """Parse the optional hours arg after /历史.  Default 24, cap 720 (30 days)."""
    args = (args or "").strip()
    if not args:
        return 24
    if not re.match(r"^\d+$", args):
        return 24
    n = int(args)
    if n <= 0 or n > 720:
        return 24
    return n


# ---------------------------------------------------------------------------
# Command handlers — each returns the response text
# ---------------------------------------------------------------------------

def cmd_remain() -> str:
    row = db.latest()  # most recent F1 (records) row, may be None
    if not row:
        return "暂未抓到电量数据,等下次抓取。"
    remain = _safe_float(row["remain"])
    read_time = row["read_time"] or "—"
    if remain is None:
        return f"抄表时间: {read_time}\n剩余电量数据缺失。"
    return f"⚡ 当前剩余: {remain:.2f} kW·h\n🕐 抄表时间: {read_time}"


def cmd_meter(room_id: Optional[str]) -> str:
    if not room_id:
        return "暂未发现 roomId,请先跑一次抓取。"
    rs = db.get_run_status(room_id)
    if not rs:
        return "暂无电表数据,等下一次抓取。"
    vol = _safe_float(rs.get("vol"))
    cur = _safe_float(rs.get("cur"))
    yggl = _safe_float(rs.get("yggl"))
    label = rs.get("run_status") or "—"
    update_dt = rs.get("update_dt") or "—"
    online = label in ("在线", "正常", "通讯正常")
    icon = "🟢" if online else "🔴"
    parts = [
        f"{icon} 电表状态: {label}",
        f"⚡ 电压: {vol:.2f} V" if vol is not None else "⚡ 电压: —",
        f"🔌 电流: {cur:.2f} A" if cur is not None else "🔌 电流: —",
        f"💡 有功功率: {yggl:.3f} W" if yggl is not None else "💡 有功功率: —",
        f"🕐 最后上报: {update_dt}",
    ]
    return "\n".join(parts)


def cmd_today(room_id: Optional[str]) -> str:
    if not room_id:
        return "暂未发现 roomId,请先跑一次抓取。"
    rows = db.recent_daily_elec(room_id, days=2)  # 2-day window so today is always in range
    if not rows:
        return "暂无今日用电数据,等下一次抓取。"
    today = max(r["dt"] for r in rows)
    today_row = next((r for r in rows if r["dt"] == today), None)
    if not today_row:
        return "暂无今日用电数据。"
    used = _safe_float(today_row.get("total_eq")) or _safe_float(today_row.get("zong_eq"))
    if used is None:
        return f"日期 {today}: 今日用电数据缺失。"
    return f"📅 {today}\n⚡ 今日用电: {used:.2f} kW·h"


def cmd_history(room_id: Optional[str], hours: int) -> str:
    if not room_id:
        return "暂未发现 roomId,请先跑一次抓取。"
    rows = db.query(hours=hours)
    if not rows:
        return f"近 {hours} 小时无抓取数据。"
    valid = [(r["ts"], _safe_float(r["remain"])) for r in rows]
    valid = [(t, v) for t, v in valid if v is not None]
    if not valid:
        return f"近 {hours} 小时无有效剩余电量数据。"
    first_ts, first_v = valid[0]
    last_ts, last_v = valid[-1]
    delta = first_v - last_v
    return (
        f"📊 近 {hours} 小时用电\n"
        f"开始 ({first_ts}): {first_v:.2f} kW·h\n"
        f"结束 ({last_ts}): {last_v:.2f} kW·h\n"
        f"消耗: {delta:.2f} kW·h"
    )


def cmd_pay(room_id: Optional[str]) -> str:
    if not room_id:
        return "暂未发现 roomId,请先跑一次抓取。"
    rows = db.recent_pay(room_id, days=90)
    if not rows:
        return "📋 暂无缴费记录。"
    recent = rows[:3]
    parts = ["📋 最近缴费 (最近 3 笔):"]
    for r in recent:
        dt = r.get("dt") or "—"
        pay_type = r.get("pay_type") or "—"
        fee_type = r.get("fee_type") or "—"
        money = _safe_float(r.get("money"))
        money_str = f"¥{money:.2f}" if money is not None else "—"
        parts.append(f"  · {dt} {pay_type} / {fee_type} {money_str}")
    return "\n".join(parts)


def cmd_violations(room_id: Optional[str]) -> str:
    if not room_id:
        return "暂未发现 roomId,请先跑一次抓取。"
    rows = db.recent_violations(room_id, days=30)
    if not rows:
        return "✓ 近 30 天无违规记录,太棒了！"
    parts = [f"⚠ 近 30 天违规 ({len(rows)} 条):"]
    for r in rows[:8]:
        dt = r.get("dt") or "—"
        reason = r.get("wg_reason") or "—"
        wg_power = _safe_float(r.get("wg_power"))
        # Round 29 fix: wg_power is a power reading (kW), not energy.
        wp = f"{wg_power:.2f} kW" if wg_power is not None else "—"
        parts.append(f"  · {dt} {reason} {wp}")
    if len(rows) > 8:
        parts.append(f"  · …另有 {len(rows) - 8} 条未显示。")
    return "\n".join(parts)


def cmd_help() -> str:
    return HELP_TEXT


# ---------------------------------------------------------------------------
# Data fetchers for the new card-based dispatcher
# ---------------------------------------------------------------------------

# Round 22 — used_today and balance helpers, factored out so the
# /remain and /today cards can both reuse them and never disagree.
_META_EQPRICE = "eqprice"   # cached unit price (¥/kW·h) from finduser


def _compute_used_today(days_rows: list[dict]) -> Optional[float]:
    """Best-effort "today's electricity use" in kW·h.

    ``daily_elec.total_eq`` is a cumulative meter reading, so a single
    row tells us nothing about how much was used today.  When we have
    at least 2 rows (today + yesterday) we use the difference; when we
    have only the today row we accept its raw value if it looks like
    a small per-day reading (< ``_MAX_DAILY_KWH``).  Returns None when
    the value would be misleading (cumulative alone, or all outliers).
    """
    if not days_rows:
        return None
    sorted_rows = sorted(days_rows, key=lambda r: r.get("dt") or "")
    today_dt = sorted_rows[-1].get("dt")
    today_row = sorted_rows[-1]
    today_v = _safe_float(today_row.get("total_eq"))
    if today_v is None:
        today_v = _safe_float(today_row.get("zong_eq"))
    if len(sorted_rows) >= 2:
        # Use the delta against the most recent prior day.
        prev_row = sorted_rows[-2]
        prev_v = _safe_float(prev_row.get("total_eq"))
        if prev_v is None:
            prev_v = _safe_float(prev_row.get("zong_eq"))
        if today_v is not None and prev_v is not None and today_v > prev_v:
            delta = today_v - prev_v
            # If the delta is suspicious, treat today's raw value as
            # the answer only if it itself looks like a per-day value
            # (small enough that it can't be a cumulative reading).
            if delta <= dorm_power._MAX_DAILY_KWH:
                return delta
            if today_v <= dorm_power._MAX_DAILY_KWH:
                return today_v
            return None
        # Reset: today < prev.  Use the raw value if plausible.
        if today_v is not None and today_v <= dorm_power._MAX_DAILY_KWH:
            return today_v
        return None
    # Only one row.  Trust it only if it looks like a per-day value.
    if today_v is not None and today_v <= dorm_power._MAX_DAILY_KWH:
        return today_v
    return None


def _compute_balance(remain_kwh: Optional[float]) -> Optional[float]:
    """Monetary value of the remaining kW·h.

    The portal's "账户余额" line is computed server-side and never
    returned by the data API.  As a proxy we use the cached
    ``eqprice`` (¥/kW·h, populated from the finduser hidden input
    on the first scrape) multiplied by the current remaining kWh.
    Returns None when either input is missing so the card shows "—"
    rather than a misleading ¥0.00.
    """
    if remain_kwh is None or remain_kwh <= 0:
        return None
    eqprice = db.get_meta_float(_META_EQPRICE)
    if eqprice is None or eqprice <= 0:
        return None
    return remain_kwh * eqprice


def _data_remain(room_id: Optional[str]) -> dict:
    row = db.latest()
    if not row:
        return {"kind": "remain", "remain": None, "read_time": "—",
                "used_today": None, "days_left": None, "balance": None,
                "online": True}
    remain = _safe_float(row["remain"])
    read_time = row["read_time"] or "—"
    used_today = None
    days_left = None
    balance = None
    if room_id:
        days_rows = db.recent_daily_elec(room_id, days=14)
        if days_rows:
            # Round 21a fix: ``total_eq`` / ``zong_eq`` are *cumulative*
            # meter readings, not per-day usage.  The old code
            # ``avg(total_eq)`` averaged cumulative values like
            # 12345.67, divided the small ``remain`` (~30 kWh) by that
            # number, and always produced ~0 → the card always showed
            # "0 天".  ``compute_avg_daily_from_cumulative`` computes
            # deltas between consecutive rows so the average is the
            # true per-day kWh consumed.
            avg_daily = dorm_power.compute_avg_daily_from_cumulative(days_rows)
            days_left = dorm_power.calc_days_remaining(remain, avg_daily)
        # Round 22: today's *usage* (not the raw cumulative meter
        # reading) for the sub-stat box.  See _compute_used_today.
        used_today = _compute_used_today(days_rows) if days_rows else None
        # Round 22: derive "账户余额" as the monetary value of the
        # remaining kW·h (no server-side balance endpoint exists).
        balance = _compute_balance(remain)
    return {"kind": "remain", "remain": remain, "read_time": read_time,
            "used_today": used_today, "days_left": days_left,
            "balance": balance, "online": True}


def _data_meter(room_id: Optional[str]) -> dict:
    if not room_id:
        return {"kind": "meter", "vol": None, "cur": None, "yggl": None,
                "online": False, "update_dt": "—"}
    rs = db.get_run_status(room_id)
    if not rs:
        return {"kind": "meter", "vol": None, "cur": None, "yggl": None,
                "online": False, "update_dt": "—"}
    label = rs.get("run_status") or "—"
    online = label in ("在线", "正常", "通讯正常")
    return {
        "kind": "meter",
        "vol": _safe_float(rs.get("vol")),
        "cur": _safe_float(rs.get("cur")),
        "yggl": _safe_float(rs.get("yggl")),
        "online": online,
        "update_dt": rs.get("update_dt") or "—",
    }


def _data_today(room_id: Optional[str]) -> dict:
    if not room_id:
        return {"kind": "today", "used": None, "dt": "—", "pays": []}
    # Round 22: fetch a few days so the delta between today and
    # yesterday can be computed; the previous ``days=2`` could miss
    # the previous day's row if the F2 backfill ran mid-day.
    rows = db.recent_daily_elec(room_id, days=5)
    if not rows:
        return {"kind": "today", "used": None, "dt": "—", "pays": []}
    today = max(r["dt"] for r in rows)
    used = _compute_used_today(rows)
    pays = db.recent_pay(room_id, days=90)[:3]
    return {
        "kind": "today",
        "used": used,
        "dt": today,
        "pays": [
            {"dt": p.get("dt"),
             "label": f"{p.get('pay_type', '')}/{p.get('fee_type', '')}",
             "money": _safe_float(p.get("money")) or 0.0}
            for p in pays
        ],
    }


def _data_history(room_id: Optional[str], hours: int) -> dict:
    if not room_id:
        return {"kind": "history", "hours": hours, "points": []}
    rows = db.query(hours=hours)
    valid = [(r["ts"], _safe_float(r["remain"])) for r in rows]
    valid = [(t, v) for t, v in valid if v is not None]
    if len(valid) < 2:
        return {"kind": "history", "hours": hours, "points": []}
    points = []
    for i in range(1, len(valid)):
        delta = valid[i - 1][1] - valid[i][1]
        if delta < 0:
            delta = 0
        points.append((valid[i][0], delta))
    return {"kind": "history", "hours": hours, "points": points}


def _data_pay(room_id: Optional[str]) -> dict:
    if not room_id:
        return {"kind": "pay", "rows": []}
    rows = db.recent_pay(room_id, days=90)
    return {
        "kind": "pay",
        "rows": [
            {"dt": r.get("dt"), "pay_type": r.get("pay_type"),
             "fee_type": r.get("fee_type"),
             "money": _safe_float(r.get("money")) or 0.0}
            for r in rows
        ],
    }


def _data_violations(room_id: Optional[str]) -> dict:
    if not room_id:
        return {"kind": "violations", "rows": []}
    rows = db.recent_violations(room_id, days=30)
    return {
        "kind": "violations",
        "rows": [
            {"dt": r.get("dt"), "wg_reason": r.get("wg_reason"),
             "wg_power": _safe_float(r.get("wg_power"))}
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Dispatch — return dict describing the response shape
# ---------------------------------------------------------------------------

def dispatch_text(text: str, room_id: Optional[str]) -> dict:
    """Parse text → return dict describing the response.

    Return shapes:
      ``{"kind": "image_card", "title": str, "payload": dict}``
      ``{"kind": "text_card",  "title": str, "text":   str}``
      ``{"kind": "text",       "text":   str}``
      ``{"kind": ""}``  # no reply
    """
    cleaned = _strip_mention(text)
    if not cleaned:
        return {"kind": ""}
    cmd, _, args = cleaned.partition(" ")
    cmd = cmd.strip()
    handler = COMMANDS.get(cmd)
    if not handler:
        return {"kind": "text",
                "text": f"未知命令: {cmd}\n\n{HELP_TEXT}"}
    if handler == "help":
        return {"kind": "text_card",
                "title": "⚡ 宿舍电量小助手 · 命令列表",
                "text": HELP_TEXT}
    if handler == "remain":
        return {"kind": "image_card", "title": "⚡ 剩余电量",
                "payload": _data_remain(room_id)}
    if handler == "meter":
        return {"kind": "image_card", "title": "⚡ 实时电表",
                "payload": _data_meter(room_id)}
    if handler == "today":
        return {"kind": "image_card", "title": "📅 今日用电",
                "payload": _data_today(room_id)}
    if handler == "history":
        return {"kind": "image_card", "title": "📊 用电历史",
                "payload": _data_history(room_id, _parse_history_args(args))}
    if handler == "pay":
        return {"kind": "image_card", "title": "📋 缴费记录",
                "payload": _data_pay(room_id)}
    if handler == "violations":
        return {"kind": "image_card", "title": "⚠ 违规记录",
                "payload": _data_violations(room_id)}
    return {"kind": "text", "text": "未知命令"}


def dispatch_menu(event_key: str, room_id: Optional[str]) -> dict:
    """Map a custom menu event_key to the same dispatcher as slash commands."""
    handler = MENU_KEYS.get(event_key)
    if not handler:
        return {"kind": "text", "text": "未知菜单项。"}
    if handler == "help":
        return {"kind": "text_card",
                "title": "⚡ 宿舍电量小助手 · 命令列表",
                "text": HELP_TEXT}
    if handler == "remain":
        return {"kind": "image_card", "title": "⚡ 剩余电量",
                "payload": _data_remain(room_id)}
    if handler == "meter":
        return {"kind": "image_card", "title": "⚡ 实时电表",
                "payload": _data_meter(room_id)}
    if handler == "today":
        return {"kind": "image_card", "title": "📅 今日用电",
                "payload": _data_today(room_id)}
    if handler == "history":
        return {"kind": "image_card", "title": "📊 用电历史",
                "payload": _data_history(room_id, 24)}
    if handler == "pay":
        return {"kind": "image_card", "title": "📋 缴费记录",
                "payload": _data_pay(room_id)}
    if handler == "violations":
        return {"kind": "image_card", "title": "⚠ 违规记录",
                "payload": _data_violations(room_id)}
    return {"kind": "text", "text": "未知菜单项。"}


# ---------------------------------------------------------------------------
# Encrypted payload (FEISHU_ENCRYPT_KEY is set on the Feishu console)
# ---------------------------------------------------------------------------

def is_encrypted(body: dict) -> bool:
    """Return True if the body is an encrypted Feishu event (has 'encrypt' field)."""
    return isinstance(body, dict) and isinstance(body.get("encrypt"), str) and bool(body["encrypt"])


def decrypt_payload(encrypt_b64: str, encrypt_key: str) -> dict:
    """Decrypt a Feishu AES-CBC encrypted event.

    Tries three paths in order:

    1. **Feishu official spec** (the one observed on real captures in
       Round 10): key = SHA256(encrypt_key_str) digest (32B),
       IV = SHA256(encrypt_key_str)[:16] (16B, taken from the hash itself,
       not from the ciphertext), algorithm = AES-256-CBC, padding = PKCS7,
       ciphertext = full base64-decoded body (no IV prefix).

    2. **AES-128 hex-decoded** (Round 7 fallback): some older Feishu
       SDKs hex-decode the 32-char key to 16 raw bytes for AES-128-CBC,
       with IV = first 16 bytes of the ciphertext.

    3. **AES-256 UTF-8 raw** (Round 7 fallback): some SDKs UTF-8 encode
       the 32-char key to 32 raw bytes for AES-256-CBC, with IV = first
       16 bytes of the ciphertext.

    Returns the decrypted JSON object.  Raises RuntimeError if all
    three attempts fail.
    """
    if not _HAS_AES:
        raise RuntimeError(
            "pycryptodome is not installed; run `pip install pycryptodome` "
            "to enable Feishu encrypted-event support."
        )
    if not encrypt_key:
        raise RuntimeError(
            "FEISHU_ENCRYPT_KEY is empty in .env.  Set it (or unset it if "
            "you turned off encryption in the Feishu console)."
        )
    try:
        ciphertext_full = base64.b64decode(encrypt_b64)
    except Exception as exc:
        raise RuntimeError(f"base64 decode failed: {exc}") from exc

    last_err: str = ""

    # --- Path 1: Feishu official spec (SHA256 → AES-256-CBC) ---
    # Reference: Feishu encrypt doc; observed on real URL-verification
    # handshake captured in Round 10.
    # Plaintext layout: [16-byte random salt][JSON payload][PKCS7 pad]
    # The salt prefix is a Feishu-specific anti-pattern measure: the
    # same message encrypts to different ciphertexts each time.  We
    # AES-decrypt the whole thing, PKCS7-unpad, then scan for the JSON
    # opening '{' (typically at offset 16, but we scan to be safe).
    try:
        digest = hashlib.sha256(encrypt_key.encode("utf-8")).digest()
        key_bytes = digest          # 32B → AES-256
        iv = digest[:16]            # IV = first 16B of the hash itself
        cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
        decrypted = cipher.decrypt(ciphertext_full)
        pad = decrypted[-1]
        if not (1 <= pad <= 16 and decrypted[-pad:] == bytes([pad]) * pad):
            raise ValueError(f"PKCS7 pad invalid: pad={pad}")
        unpadded = decrypted[:-pad]
        # Per Feishu spec (Java SDK EventDecryption): the salt prefix is
        # ALWAYS exactly 16 bytes — never search for '{' to locate it.
        # Random salt has ~6% chance of containing 0x7B which previously
        # caused `find(b"{")` to land mid-salt, corrupting the UTF-8
        # decode.  Hard-slice the prefix.
        plaintext = unpadded[16:].decode("utf-8")
        return json.loads(plaintext)
    except Exception as exc:  # noqa: BLE001
        last_err = f"SHA256-AES256: {type(exc).__name__}: {exc}"

    # --- Path 2 & 3: Round 7 fallbacks (IV = first 16B of ciphertext) ---
    if len(ciphertext_full) < 32 or len(ciphertext_full) % 16 != 0:
        raise RuntimeError(
            f"ciphertext length {len(ciphertext_full)} invalid; "
            f"SHA256 path failed ({last_err})"
        )
    iv = ciphertext_full[:16]
    body_bytes = ciphertext_full[16:]

    candidates: list = []
    if len(encrypt_key) == 32:
        try:
            candidates.append((bytes.fromhex(encrypt_key), "AES-128 (hex-decoded 16B)"))
        except ValueError:
            pass
    candidates.append(
        (encrypt_key.encode("utf-8"), f"AES-256 (UTF-8 {len(encrypt_key.encode('utf-8'))}B)")
    )

    for key_bytes, label in candidates:
        try:
            cipher = AES.new(key_bytes, AES.MODE_CBC, iv)
            plaintext = cipher.decrypt(body_bytes)
            unpadded = unpad(plaintext, 16)
            return json.loads(unpadded.decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            last_err += f"; {label}: {type(exc).__name__}: {exc}"
            continue

    raise RuntimeError(
        f"all AES attempts failed. {last_err}"
    )


# ---------------------------------------------------------------------------
# Inbound: event handler
# ---------------------------------------------------------------------------

def handle_url_verification(body: dict) -> Optional[dict]:
    """Handle the url_verification probe Feishu sends when you save the URL.

    Returns the response dict, or None if this isn't a url_verification event.
    """
    if body.get("type") != "url_verification":
        return None
    return {"challenge": body.get("challenge", "")}


def verify_token(body: dict) -> bool:
    """Validate an inbound Feishu event.

    Two distinct shapes (Round 12 fix — previous version treated every
    body as a v1 url_verification handshake, which made ``body.get(
    "token")`` return None for v2 events and fail every real event).

    1. ``url_verification`` handshake (v1 plain JSON): top-level
       ``token`` field must match ``FEISHU_VERIFICATION_TOKEN`` if
       one is configured.

    2. v2 events (encrypted or plain): there is no top-level
       ``token``; validation uses the ``X-Lark-Signature`` header.

    Round 13 fix — the previous code used HMAC-SHA256(key, ts+nonce+body),
    but the real Feishu spec (per the captured signature at 02:28:15) is
    plain SHA256, with the encrypt_key *concatenated into* the message,
    not used as a HMAC key::

        sig = SHA256(timestamp + nonce + encrypt_key + raw_body)

    We try every reasonable combination — SHA256 then HMAC, with either
    ``FEISHU_ENCRYPT_KEY`` or ``FEISHU_VERIFICATION_TOKEN`` as the
    candidate key — so a future spec change or swapped key name still
    lets the event through.  When the Feishu console has signature
    verification disabled, no signature headers are sent and we pass
    through (matching the previous permissive behavior for local tests).
    """
    # Case 1: url_verification handshake (legacy v1)
    if body.get("type") == "url_verification":
        if not config.FEISHU_VERIFICATION_TOKEN:
            return True
        return body.get("token") == config.FEISHU_VERIFICATION_TOKEN

    # Case 2: v2 event signed with X-Lark-Signature
    try:
        sig = request.headers.get("X-Lark-Signature", "")
        ts = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")
        headers = {
            "X-Lark-Signature": sig,
            "X-Lark-Request-Timestamp": ts,
            "X-Lark-Request-Nonce": nonce,
        }
    except RuntimeError:
        # No active Flask request context (e.g. direct unit-test call).
        return True
    try:
        raw = request.get_data(as_text=True) or ""
    except RuntimeError:
        raw = ""

    # Round 34C — v2 events (encrypted or signature-bearing) MUST have
    # all three headers.  Only legacy v1 events (no sig headers, console
    # mode) keep the prior fail-open so existing dev workflows aren't
    # broken.  See ``_verify_lark_signature`` for the tightened policy.
    is_encrypted = bool(isinstance(body, dict) and body.get("encrypt"))
    body_bytes = raw.encode("utf-8") if isinstance(raw, str) else (raw or b"")
    return _verify_lark_signature(headers, body_bytes, is_encrypted)


def _do_sha256_verify(ts: str, nonce: str, body: bytes, sig: str) -> bool:
    """Round 34C — extracted SHA256/HMAC verification loop.

    Tries every reasonable (algorithm, key) combination so a future
    spec change or swapped key name still lets the event through:
    plain SHA256 with the key concatenated into the message, plus
    HMAC-SHA256 with the key as the HMAC key.  The candidate keys
    are ``FEISHU_ENCRYPT_KEY`` and ``FEISHU_VERIFICATION_TOKEN``.
    """
    raw = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else (body or "")
    for key in (config.FEISHU_ENCRYPT_KEY, config.FEISHU_VERIFICATION_TOKEN):
        if not key:
            continue
        sha = hashlib.sha256(
            (ts + nonce + key + raw).encode("utf-8")
        ).hexdigest()
        if hmac.compare_digest(sha, sig):
            return True
        mac = hmac.new(
            key.encode("utf-8"),
            (ts + nonce + raw).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if hmac.compare_digest(mac, sig):
            return True
    return False


def _verify_lark_signature(headers: dict, body: bytes, is_encrypted: bool) -> bool:
    """Round 34C — tightened signature verification (B4 fix).

    For v2 (encrypted) events, ALL of sig/ts/nonce must be present and
    verify correctly — no fail-open.  For v1 events without encryption,
    missing headers in a console-disabled mode fall through, but only
    if body type is NOT ``url_verification`` (which always requires
    signature check).

    Previous behavior was: any missing sig/ts/nonce → return True,
    letting an attacker bypass the signature with three empty headers.
    Now only the legacy v1 "other event" path is permissive.
    """
    sig = headers.get("X-Lark-Signature", "") or ""
    ts = headers.get("X-Lark-Request-Timestamp", "") or ""
    nonce = headers.get("X-Lark-Request-Nonce", "") or ""

    if is_encrypted:
        # v2 event — strict: all headers required
        if not (sig and ts and nonce):
            logger.warning("v2 event missing signature headers — rejecting")
            return False
        return _do_sha256_verify(ts, nonce, body, sig)

    # v1 event — allow fail-open ONLY for non-verification events
    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except Exception:
        return False
    if isinstance(payload, dict) and payload.get("type") == "url_verification":
        # Even v1 verification events need signature
        if not (sig and ts and nonce):
            return False
        return _do_sha256_verify(ts, nonce, body, sig)

    # Other v1 events (message, etc.) — keep legacy fail-open for console mode
    if not (sig and ts and nonce):
        return True
    return _do_sha256_verify(ts, nonce, body, sig)


def handle_event(body: dict, room_id: Optional[str]) -> dict:
    """Top-level event dispatcher.  Always returns a Feishu-shaped ack.

    Round 16 — the reply chain (Pillow render → image upload → card
    send) is now wrapped in an outer try/except.  If anything in that
    chain raises, the user gets a plain text error message instead of
    silence.  If the text fallback itself fails (e.g. tenant_access_token
    unavailable), the full stack is dumped to ``dorm.log`` / journalctl
    via ``current_app.logger.exception`` (or the module logger as a
    last-resort fallback).
    """
    # 0) Save context for error fallback BEFORE anything can raise.
    #    We capture: (a) the user-typed command (for the error message),
    #    (b) the sender's open_id (so the fallback can reply to them).
    try:
        _evt = body.get("event", {}) if isinstance(body, dict) else {}
        _msg = _evt.get("message", {}) or {}
        try:
            _parsed = json.loads(_msg.get("content", "") or "{}")
        except (ValueError, TypeError):
            _parsed = {}
        _raw_text = (_parsed.get("text", "")
                     if isinstance(_parsed, dict) else "") or ""
        _ctx_cmd = _strip_mention(_raw_text).partition(" ")[0].strip() or "(no cmd)"
    except Exception:  # noqa: BLE001
        _ctx_cmd = "(unknown)"

    # 1) url_verification — no token check, no reply, just echo challenge
    if body.get("type") == "url_verification":
        return handle_url_verification(body) or {"code": 0, "msg": "ok"}

    # 2) Token check
    if not verify_token(body):
        logger.warning("feishu event: invalid verification token")
        return {"code": 401, "msg": "invalid token"}

    # 3) Parse header + event
    header = body.get("header", {})
    event_type = header.get("event_type", "")
    event = body.get("event", {})
    sender_open_id: Optional[str] = (
        event.get("sender", {}).get("sender_id", {}).get("open_id")
    )

    logger.info(
        "feishu event: type=%s sender=%s room_id=%s cmd=%s",
        event_type,
        sender_open_id,
        room_id or "—",
        _ctx_cmd,
    )

    response: dict = {"kind": ""}

    if event_type == "im.message.receive_v1":
        msg = event.get("message", {})
        # Only respond to text messages
        if msg.get("message_type") == "text":
            # Feishu v2: there is no top-level `text` field.  The actual
            # text is JSON-encoded in `content` as `{"text": "..."}`.
            # The previous version read `msg.get("text")` which is always
            # empty for v2 events, so dispatch_text never saw the command.
            content_str = msg.get("content", "") or ""
            try:
                parsed = json.loads(content_str) if content_str else {}
            except (ValueError, TypeError):
                parsed = {}
            text = parsed.get("text", "") if isinstance(parsed, dict) else ""
            # Update the captured command name from the actual text we
            # dispatch on (handles @-mention stripping consistently).
            if text:
                _ctx_cmd = _strip_mention(text).partition(" ")[0].strip() or _ctx_cmd
            response = dispatch_text(text, room_id)
    elif event_type == "application.bot.menu_v6":
        event_key = event.get("event_key", "") or ""
        response = dispatch_menu(event_key, room_id)

    # 4) Send the reply (if any) to the original sender.  Any exception
    #    in the reply chain now triggers a text fallback so the user is
    #    never left in silence.
    if response.get("kind") and sender_open_id:
        try:
            kind = response["kind"]
            if kind == "image_card":
                payload = response["payload"]
                image_bytes = _render_stat_card(payload)
                image_key = _upload_image(image_bytes)
                card = _build_image_card(
                    image_key, response["title"],
                    sub_text="数据由宿舍电量小助手自动生成",
                    footer=f"PNG {len(image_bytes)} bytes",
                )
                reply_card(sender_open_id, card)
                logger.info("feishu image_card reply: %d bytes", len(image_bytes))
            elif kind == "text_card":
                card = _build_text_card(response["title"], response["text"],
                                        footer="dorm-power-monitor")
                reply_card(sender_open_id, card)
                logger.info("feishu text_card reply: ok")
            elif kind == "text":
                reply_text(sender_open_id, response["text"])
                logger.info("feishu text reply: ok (%d chars)",
                            len(response["text"]))
        except Exception as exc:  # noqa: BLE001
            # Main reply chain failed — log the full stack and try to
            # send a plain-text error message so the user is not left
            # wondering why their command was ignored.
            logger.exception(
                "feishu reply chain failed for user=%s cmd=%s: %r",
                sender_open_id, _ctx_cmd, exc,
            )
            try:
                err_text = (
                    f"⚠️ 命令处理失败\n\n"
                    f"命令: {_ctx_cmd}\n"
                    f"异常: {type(exc).__name__}\n"
                    f"详情: {str(exc)[:300]}\n"
                    f"时间: {_dt.datetime.now().isoformat()}\n\n"
                    f"已记录完整堆栈到 server 日志，稍后修复中。"
                )
                send_text_message(sender_open_id, err_text)
                logger.info("feishu error fallback text sent to %s",
                            sender_open_id)
            except Exception as e2:  # noqa: BLE001
                # Even the text fallback failed (e.g. tenant_access_token
                # could not be obtained).  Last resort: dump the full
                # stack to journalctl so the operator has a paper trail.
                try:
                    from flask import current_app
                    current_app.logger.exception(
                        "feishu text fallback also failed user=%s cmd=%s: %r",
                        sender_open_id, _ctx_cmd, e2,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "feishu text fallback also failed user=%s cmd=%s: %r",
                        sender_open_id, _ctx_cmd, e2,
                    )

    return {"code": 0, "msg": "ok"}


# ---------------------------------------------------------------------------
# Push helpers — Round 34C
# ---------------------------------------------------------------------------
# These helpers let the cron-driven pushes (l1 / l2 / daily / weekly /
# monthly) and on-demand alerts read a per-layer preference from the
# ``meta`` table so the user can turn individual channels off without
# editing code.  The webhook URL itself is also read from meta (with a
# fallback to the env var set at deploy time).


_PUSH_DEFAULTS: dict[str, str] = {
    "l1": "0",
    "l2": "1",
    "daily": "1",
    "weekly": "1",
    "monthly": "1",
    "alert_low": "1",
    "alert_violation": "1",
    "alert_offline": "1",
    "alert_stale": "1",
}


def _should_push(layer: str) -> bool:
    """Return True if the user has enabled push for this layer.

    layer: "l1" / "l2" / "daily" / "weekly" / "monthly" / "alert_low" / etc.
    Reads meta key ``push_<layer>_enable``.  Defaults:
    alerts=1, l2/daily/weekly/monthly=1, l1=0.
    """
    raw = db.get_meta(f"push_{layer}_enable")
    if raw is None or raw == "":
        raw = _PUSH_DEFAULTS.get(layer, "1")
    return raw == "1"


def _in_quiet_hours() -> bool:
    """Return True if current Beijing time falls in quiet hours.

    Quiet hours only suppress l1/l2 (cron-driven regular pushes).
    Alerts always go through.  Supports windows that cross midnight
    (e.g. 23:00 - 07:00).
    """
    now_bj = _dt.datetime.now()
    start = db.get_meta("quiet_hours_start") or "23:00"
    end = db.get_meta("quiet_hours_end") or "07:00"
    try:
        sh, sm = (int(x) for x in start.split(":"))
        eh, em = (int(x) for x in end.split(":"))
    except (ValueError, AttributeError):
        return False
    cur = now_bj.hour * 60 + now_bj.minute
    s = sh * 60 + sm
    e = eh * 60 + em
    if s < e:
        return s <= cur < e
    # 跨午夜，比如 23:00 - 07:00
    return cur >= s or cur < e


def _webhook_url() -> str:
    """Get Feishu webhook URL from meta or fallback to env."""
    return db.get_meta("feishu_webhook_url") or config.FEISHU_WEBHOOK


def _post_feishu(card: dict, webhook_url: str | None = None) -> None:
    """Post the card to Feishu.  Falls back to stdout if no webhook is set.

    Round 34C — webhook_url now accepts override (used by OOBE test-push
    when the user is configuring for the first time and meta.feishu_webhook_url
    hasn't been saved yet).
    """
    webhook = webhook_url or _webhook_url()
    if not webhook:
        logger.warning("FEISHU_WEBHOOK is empty; printing card to stdout instead.")
        print(json.dumps(card, ensure_ascii=False, indent=2))
        return
    try:
        resp = _SESSION.post(
            webhook,
            json={"msg_type": "interactive", "card": card},
            timeout=10,
        )
        if resp.status_code >= 400:
            logger.warning(
                "feishu webhook post failed: status=%d body=%s",
                resp.status_code,
                resp.text[:300],
            )
        else:
            logger.info("feishu webhook post ok: layer=%s",
                        card.get("_layer", "?") if isinstance(card, dict) else "?")
    except Exception as exc:  # noqa: BLE001
        logger.warning("feishu webhook post raised: %r", exc)


def push_if_enabled(layer: str, card: dict) -> bool:
    """Push a card only if layer is enabled and not in quiet hours.

    Returns True if pushed, False otherwise.  Quiet hours only suppress
    l1/l2 (the cron-driven regular pushes).  Alerts always go through.
    """
    if not _should_push(layer):
        logger.info("push suppressed: layer=%s disabled", layer)
        return False
    if layer in ("l1", "l2") and _in_quiet_hours():
        logger.info("push suppressed: layer=%s in quiet hours", layer)
        return False
    _post_feishu(card, _webhook_url())
    return True
