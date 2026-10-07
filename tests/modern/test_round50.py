"""Round 50 — UI 重设计 (DeepSeek 风格 + 数据 dashboard).

Background
==========
Pre-R50 the dashboard was MCSM 风格的深色 sidebar + topbar + card-grid
组合。R50 重做：

  * 顶部 tab nav (`.topnav` + `.nav-tab`) 替代左侧 sidebar
  * Hero 区大字 72px 数字 (gradient text)
  * 玻璃质感 card (backdrop-filter blur)
  * 浅蓝渐变背景 (DeepSeek 探索未知风格)
  * 4 个 stat card (本月费用 / 1小时 / 日均 / 当前)
  * 浅色 / 暗色 双主题切换 + 6 accent colors
  * 保留所有 R46-R49 修复:
    - R46 datetime-local T→space 替换
    - R48 setPill + queryRecords +'+08:00' 时区修复
    - R49 db.py UNIQUE(ts) + INSERT OR REPLACE

Coverage (5 static testcases):

  T1  ``test_index_html_uses_nav_tab_not_nav_item``
        — INDEX_HTML 含 nav-tab class, topnav 容器
        — nav-item 引用应大幅减少 (允许残留 = 2 在注释中)

  T2  ``test_required_dom_ids_preserved``
        — status-pill / chart / chart-daily / records-* /
          refresh-btn / theme-toggle 全部保留

  T3  ``test_r48_fix_and_r46_fix_preserved``
        — setPill / queryRecords 的 '+08:00' 修复保留
        — T→space 替换保留

  T4  ``test_glassmorphism_css_present``
        — backdrop-filter blur / rgba 玻璃 / gradient / accent 色
          全部存在

  T5  ``test_py_compile_passes``
        — py_compile web.py + test_round50.py (无 runtime)
"""
from __future__ import annotations

import ast
import py_compile
import re
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
WEB_PY = PROJ_DIR / "web.py"
THIS_TEST = PROJ_DIR / "tests" / "modern" / "test_round50.py"
# R62 refactor: INDEX_HTML moved out of web.py into templates/dashboard.html.
TEMPLATE_PATH = PROJ_DIR / "templates" / "dashboard.html"
# R51b refactor: visual styling moved out of the inline <style> block
# in dashboard.html and into a dedicated stylesheet.  The
# glassmorphism / design-token / typography checks now grep this file.
CSS_PATH = PROJ_DIR / "static" / "css" / "dashboard.css"
# R51b refactor: client-side JS (setPill / queryRecords / R46 T→space
# fix / R48 +08:00 fix / withViewTransition wrapper) moved out of the
# inline <script> block in the template and into a real module.  The
# TZ-aware parsing checks now grep this file rather than the HTML.
JS_PATH = PROJ_DIR / "static" / "js" / "dashboard.js"


def _extract_index_html() -> str:
    """Return dashboard HTML.

    Pre-R62 the INDEX_HTML triple-quoted block lived inline inside web.py
    and could be ripped out of the source via regex.  R62 split it out
    into ``templates/dashboard.html`` and web.py now loads it via
    ``_load_template("dashboard.html")``.  This helper therefore just
    reads the file directly (no more regex on web.py).
    """
    assert TEMPLATE_PATH.is_file(), (
        f"R62 dashboard template missing: {TEMPLATE_PATH}"
    )
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _load_static_css() -> str:
    """Return the R51b external dashboard stylesheet contents.

    R51b split visual styling out of the inline ``<style>`` block in
    ``dashboard.html`` into ``static/css/dashboard.css``.  The
    glassmorphism / design-token checks now grep this file rather
    than the HTML template.
    """
    assert CSS_PATH.is_file(), (
        f"R51b dashboard stylesheet missing: {CSS_PATH}"
    )
    return CSS_PATH.read_text(encoding="utf-8")


def _load_static_js() -> str:
    """Return the R51b external dashboard script contents.

    R51b split the client-side JS out of the inline ``<script>`` block
    in ``dashboard.html`` into ``static/js/dashboard.js``.  The R46
    ``T -> space`` datetime-local flip and the R48 ``+08:00`` CST
    wall-clock fix in ``setPill`` / ``queryRecords`` now live here —
    these checks therefore grep this file rather than the HTML template.
    """
    assert JS_PATH.is_file(), (
        f"R51b dashboard script missing: {JS_PATH}"
    )
    return JS_PATH.read_text(encoding="utf-8")


# =========================================================================
# T1 — INDEX_HTML uses nav-tab class and topnav container
# =========================================================================
class TestIndexHtmlUsesNavTab(unittest.TestCase):
    """INDEX_HTML must use the new nav-tab + topnav structure."""

    def test_index_html_uses_nav_tab_not_nav_item(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html()

        # R51b renamed .nav-tab -> .tab (Linear / Vercel pill style).
        self.assertIn(
            'class="tab"',
            html,
            "INDEX_HTML must use class=\"tab\" on top-nav buttons "
            "(R51b redesign renamed .nav-tab to .tab)",
        )
        # .topnav container must still be present (R51b kept the R50
        # topnav structure — only the inner buttons were renamed).
        self.assertIn(
            "topnav",
            html,
            "INDEX_HTML must contain .topnav container (R50→R51b redesign)",
        )
        # Old .nav-tab class should be GONE from the visible HTML.
        # We use a regex to count only standalone .nav-tab (not
        # .bottom-nav-tab).  Allow up to 2 occurrences defensively for
        # comments / docs that may still mention the R50 name.
        nav_tab_only = re.findall(r"(?<!bottom-)nav-tab", html)
        self.assertLessEqual(
            len(nav_tab_only), 2,
            f"INDEX_HTML still uses standalone .nav-tab "
            f"{len(nav_tab_only)} times — R51b renamed it to .tab.",
        )


# =========================================================================
# T2 — required DOM IDs preserved for JS to find
# =========================================================================
class TestRequiredDomIdsPreserved(unittest.TestCase):
    """All backend integration IDs must remain in the HTML."""

    def test_required_dom_ids_preserved(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html()

        # All these IDs are referenced by JS in dashboard.js (R51b
        # moved JS out of the inline <script> tag).  Removing any of
        # them would cause silent JS errors at runtime.
        #
        # R51b removed two R50-era ids that no longer exist:
        #   - theme-modal      (R51b collapses theme picker to a
        #                       simple toggle; no modal markup).
        #   - card-monthly-projection (replaced by <dorm-stat-card>
        #                              web component which carries
        #                              its own data-* attrs instead
        #                              of a wrapping card id).
        # R51b added several ids the JS hooks (setPill / setLastUpdate
        # / showToast / records rendering) now rely on.
        required_ids = [
            # Core R50 IDs still present in R51b (JS-bindable).
            "status-pill",
            "chart",
            "chart-daily",
            "records-tbody",
            "records-start",
            "records-end",
            "records-query",
            "refresh-btn",
            "theme-toggle",
            "stat-remain",
            "stat-hourly",
            "stat-daily",
            "stat-monthly-projection",
            "meter-vol",
            "meter-cur",
            "meter-yggl",
            "meter-status-pill",
            "meter-update-dt",
            "range-group",
            # R51b/R66 IDs JS depends on.
            "last-update",          # setLastUpdate() — "采集时间" hero meta
            "status-bar",           # R51b bottom status strip
            "status-bar-dot",       # setPill() flips dot color
            "status-bar-text",      # setPill() writes "已连接/已离线"
            "hero-status-dot",      # setPill() flips hero dot color
            "hero-status-text",     # setPill() writes hero text
            "toast-container",      # showToast() appendChild target
            "records-empty",        # R51b records empty-state
            "stat-readtime",        # R51b read-time card value
        ]
        for id_name in required_ids:
            self.assertIn(
                f'id="{id_name}"',
                html,
                f"INDEX_HTML missing required id={id_name!r}",
            )


# =========================================================================
# T3 — R46/R48 critical fixes preserved
# =========================================================================
class TestR46R48FixesPreserved(unittest.TestCase):
    """R46 T→space fix and R48 +08:00 fix must both survive the redesign."""

    def test_setpill_plus08_fix_preserved(self) -> None:
        """setPill must still parse ts as CST via +08:00 (R48)."""
        src = WEB_PY.read_text(encoding="utf-8")
        js = _load_static_js()

        # R51b moved setPill out of the inline <script> in dashboard.html
        # and into static/js/dashboard.js — the R48 CST parsing fix
        # (`replace ' ' with 'T'` + `append '+08:00'`) now lives there.
        # R48 fix: replace ' ' with 'T' AND append '+08:00' so JS Date
        # treats the naive-CST string as Asia/Shanghai wall clock.
        self.assertIn(
            "replace(' ', 'T')",
            js,
            "R48 setPill fix missing in static/js/dashboard.js — "
            "must replace space with T in ts",
        )
        # The actual +08:00 fragment (with or without leading +)
        self.assertIn(
            "+08:00",
            js,
            "R48 setPill fix missing in static/js/dashboard.js — "
            "must append +08:00 to parsed ts",
        )

    def test_queryrecords_plus08_fix_preserved(self) -> None:
        """queryRecords datetime-local range check must use +08:00 (R48)."""
        src = WEB_PY.read_text(encoding="utf-8")
        js = _load_static_js()

        # R51b moved queryRecords out of the inline <script> into
        # static/js/dashboard.js.  queryRecords compares datetime-local
        # values; R48 forces them to be parsed as CST by appending
        # '+08:00'.
        # We expect: setPill (1) + queryRecords range comparison (2
        # occurrences on `new Date(s + '+08:00') > new Date(e + '+08:00')`)
        # = 3 total.  Require at least 2.
        plus08_count = js.count("+08:00")
        self.assertGreaterEqual(
            plus08_count, 2,
            f"Expected at least 2 '+08:00' uses in static/js/dashboard.js "
            f"(setPill + queryRecords), got {plus08_count}",
        )

    def test_t_to_space_replace_preserved(self) -> None:
        """R46 — records.ts uses space, datetime-local uses T.  Must replace."""
        src = WEB_PY.read_text(encoding="utf-8")
        js = _load_static_js()

        # R51b moved queryRecords into static/js/dashboard.js — the
        # R46 T→space datetime-local flip is now on lines that build
        # the URL query (`s.replace('T', ' ')`) and on the human
        # readable range label.
        self.assertIn(
            ".replace('T', ' ')",
            js,
            "R46 T→space replace missing in static/js/dashboard.js "
            "queryRecords — records.ts is stored with space separator, "
            "must convert from T.",
        )


# =========================================================================
# T4 — Glassmorphism CSS present
# =========================================================================
class TestGlassmorphismCssPresent(unittest.TestCase):
    """DeepSeek-style glassmorphism + gradient must be present."""

    def test_glassmorphism_css_present(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html()
        css = _load_static_css()

        # R51b moved visual styling out of the inline <style> block in
        # the template and into static/css/dashboard.css.  This test
        # now grep's the external stylesheet for the design-system
        # tokens / glass / typography markers.
        #
        # R51b also shifted the accent palette from DeepSeek blue
        # (#4D8BFF / #7B6FFF) to Linear/Vercel indigo (#6366f1) — we
        # assert against the R51b token names instead of the R50 hexes.
        required = [
            # Design-token definitions in :root of dashboard.css
            "--accent:",
            "--bg-card:",
            "--bg-page:",
            # Glassmorphism (frosted topnav + bottom status bar)
            "backdrop-filter",
            "blur(",
            # Tokens consumed by component CSS
            "var(--accent)",
            "var(--bg-card)",
            # General visual language
            "border-radius",
            "tabular-nums",
        ]
        for needle in required:
            self.assertIn(
                needle,
                css,
                f"static/css/dashboard.css missing CSS fragment: {needle!r}",
            )

        # The hero big-number scales by viewport; the "72px" R50 spec
        # is the wide-screen landing size (>=1440px).  R51b uses 64px
        # for desktop, 56px for tablet, 44px for mobile, 36px for small
        # phones — 72px is only emitted on the wide-screen media query.
        self.assertIn(
            "font-size: 72px",
            css,
            "static/css/dashboard.css must declare 72px hero font-size "
            "(R50 spec → R51b wide-screen landing size)",
        )

    def test_hero_structure_present(self) -> None:
        """Hero section, top nav, stat grid all present."""
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html()

        # R51b rewrote the hero / topnav classes for the Linear /
        # Vercel pill style.  The test still asserts that *all* the
        # major structural containers exist — just spelled with their
        # R51b names:
        #   .hero / .hero-label / .hero-value / .hero-unit / .hero-meta
        #   .topnav / .tabs / .tab
        #   .status-bar / .toast-container
        #   .grid / .grid-3   (replaces R50 .stat-grid)
        # Required structural elements
        for cls in ["hero", "hero-label", "hero-value", "hero-unit",
                    "hero-meta", "topnav", "tabs", "tab",
                    "status-bar", "toast-container",
                    "grid", "grid-3"]:
            self.assertIn(
                cls,
                html,
                f"INDEX_HTML missing class={cls!r}",
            )


# =========================================================================
# T5 — py_compile passes (no runtime execution)
# =========================================================================
class TestPyCompilePasses(unittest.TestCase):
    """web.py + test_round50.py must parse without syntax errors."""

    def test_py_compile_passes(self) -> None:
        # web.py — full file including the giant INDEX_HTML string.
        py_compile.compile(str(WEB_PY), doraise=True)
        # test_round50.py — the test itself.
        py_compile.compile(str(THIS_TEST), doraise=True)

        # Also do a full AST parse of web.py so we catch any structural
        # issues that py_compile might miss (it only compiles the
        # module-level code).
        src = WEB_PY.read_text(encoding="utf-8")
        ast.parse(src)
        print(f"  [py_compile OK] web.py + test_round50.py + ast.parse")


# =========================================================================
# Run with ``python -m unittest tests.modern.test_round50``
# =========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)