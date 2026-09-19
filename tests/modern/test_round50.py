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


def _extract_index_html(src: str) -> str:
    """Return the body of the INDEX_HTML triple-quoted block."""
    pattern = r'INDEX_HTML\s*=\s*"""(.*?)"""'
    m = re.search(pattern, src, re.DOTALL)
    assert m, "INDEX_HTML not found in web.py"
    return m.group(1)


# =========================================================================
# T1 — INDEX_HTML uses nav-tab class and topnav container
# =========================================================================
class TestIndexHtmlUsesNavTab(unittest.TestCase):
    """INDEX_HTML must use the new nav-tab + topnav structure."""

    def test_index_html_uses_nav_tab_not_nav_item(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        # nav-tab must be present (it's the new class name)
        self.assertIn(
            "nav-tab",
            html,
            "INDEX_HTML must use nav-tab class (R50 redesign)",
        )
        # topnav container must be present
        self.assertIn(
            "topnav",
            html,
            "INDEX_HTML must contain .topnav container (R50 redesign)",
        )
        # Old .nav-item class should be GONE from the visible HTML.
        # The .bottom-nav-item class is the mobile bottom nav and stays.
        # We use a regex to count only standalone .nav-item (not
        # .bottom-nav-item).  Allow up to 2 occurrences (defensive —
        # comments / docs may still mention the old name as historical
        # context).
        nav_item_only = re.findall(r"(?<!bottom-)nav-item", html)
        self.assertLessEqual(
            len(nav_item_only), 2,
            f"INDEX_HTML still uses standalone .nav-item "
            f"{len(nav_item_only)} times — should be reduced to ~0 "
            "(R50 redesign replaced nav-item with nav-tab).",
        )


# =========================================================================
# T2 — required DOM IDs preserved for JS to find
# =========================================================================
class TestRequiredDomIdsPreserved(unittest.TestCase):
    """All backend integration IDs must remain in the HTML."""

    def test_required_dom_ids_preserved(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        # All these IDs are referenced by JS in the <script> block.
        # Removing any of them would cause silent JS errors at runtime.
        required_ids = [
            "status-pill",
            "chart",
            "chart-daily",
            "records-tbody",
            "records-start",
            "records-end",
            "records-query",
            "refresh-btn",
            "theme-toggle",
            "theme-modal",
            "stat-remain",
            "stat-hourly",
            "stat-daily",
            "stat-monthly-projection",
            "card-monthly-projection",
            "meter-vol",
            "meter-cur",
            "meter-yggl",
            "meter-status-pill",
            "meter-update-dt",
            "range-group",
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
        html = _extract_index_html(src)

        # R48 fix: replace ' ' with 'T' AND append '+08:00' so JS Date
        # treats the naive-CST string as Asia/Shanghai wall clock.
        self.assertIn(
            "replace(' ', 'T')",
            html,
            "R48 setPill fix missing — must replace space with T in ts",
        )
        # The actual +08:00 fragment (with or without leading +)
        self.assertIn(
            "+08:00",
            html,
            "R48 setPill fix missing — must append +08:00 to parsed ts",
        )

    def test_queryrecords_plus08_fix_preserved(self) -> None:
        """queryRecords datetime-local range check must use +08:00 (R48)."""
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        # queryRecords compares datetime-local values; R48 forces them
        # to be parsed as CST by appending '+08:00'.
        # We expect at least 2 occurrences (range comparison + T→space replace).
        plus08_count = html.count("+08:00")
        self.assertGreaterEqual(
            plus08_count, 2,
            f"Expected at least 2 '+08:00' uses (setPill + queryRecords), "
            f"got {plus08_count}",
        )

    def test_t_to_space_replace_preserved(self) -> None:
        """R46 — records.ts uses space, datetime-local uses T.  Must replace."""
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        self.assertIn(
            ".replace('T', ' ')",
            html,
            "R46 T→space replace missing in queryRecords — records.ts "
            "is stored with space separator, must convert from T.",
        )


# =========================================================================
# T4 — Glassmorphism CSS present
# =========================================================================
class TestGlassmorphismCssPresent(unittest.TestCase):
    """DeepSeek-style glassmorphism + gradient must be present."""

    def test_glassmorphism_css_present(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        required = [
            "backdrop-filter",
            "blur(",
            "linear-gradient(135deg",
            "rgba(255, 255, 255",
            "#4D8BFF",        # DeepSeek blue accent
            "#7B6FFF",        # gradient end
            "var(--accent)",
            "var(--bg-card)",
            "var(--gradient-accent)",
            "border-radius",
            "tabular-nums",
        ]
        for needle in required:
            self.assertIn(
                needle,
                html,
                f"INDEX_HTML missing CSS fragment: {needle!r}",
            )

        # Hero value must be 72px font size (the spec's "big number").
        self.assertIn(
            "font-size: 72px",
            html,
            "Hero value must be 72px (DeepSeek-style big number)",
        )

    def test_hero_structure_present(self) -> None:
        """Hero section, top nav, stat grid all present."""
        src = WEB_PY.read_text(encoding="utf-8")
        html = _extract_index_html(src)

        # Required structural elements
        for cls in ["hero", "hero-value", "hero-status", "stat-grid",
                    "nav-tab", "status-bar", "bottom-nav-item",
                    "theme-modal", "toast-container"]:
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