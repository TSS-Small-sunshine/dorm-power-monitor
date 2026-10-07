"""R67 — Multi-device responsive (iPad / PC / Mobile) CSS.

Background
==========
R51b shipped a dark-themed dashboard for desktop.  R67 layers a
four-breakpoint responsive system on top of ``dashboard.css`` /
``oobe.css`` / ``admin.css`` so the same product renders sanely on:

* iPhone / Android phones (max-width: 767px)
* iPad portrait (768–1023)
* iPad landscape / small laptop (≥1024 but <1440)
* Desktop / Wide monitor (≥1440)

R67 also ships touch-target hardening (44 px Apple HIG), iOS
zoom prevention (16 px input font-size), backdrop-filter perf
kill-switch for phones, and iOS safe-area inset support.

This test is **AST-based static guard** only — no rendering, no
browser, no JS eval.  It scans CSS files for media-query presence
and key selectors so we know the responsive layer is intact
without booting a device emulator.

Coverage (16 static testcases):

  T1  — All three CSS files exist and are non-empty.
  T2  — Each CSS file declares at least 4 ``@media`` rules.
  T3  — Each CSS file declares all 4 named breakpoints
        (mobile ≤767, tablet 768–1023, desktop 1024–1439, wide ≥1440).
  T4  — dashboard.css includes an orientation media query.
  T5  — dashboard.css includes ``(hover: hover)`` or
        ``(hover: none)`` so touch affordances are gated.
  T6  — dashboard.css includes ``prefers-reduced-motion``.
  T7  — At least one CSS file declares ``min-height: 44px``
        for touch targets.
  T8  — At least one CSS file declares ``font-size: 16px`` on
        inputs (iOS zoom prevention).
  T9  — At least one CSS file uses ``repeat(auto-fit`` or
        ``minmax(`` for responsive grid layouts.
  T10 — Each CSS file caps container width with ``max-width``
        (centered wrapper).
  T11 — dashboard.css includes the safe-area ``env(...)`` query
        for iOS notch handling.
  T12 — dashboard.css disables ``backdrop-filter`` on mobile.
  T13 — dashboard.css exposes the ``--chart-dpr-cap`` custom
        property that R67's chart perf plan calls for.
  T14 — dashboard.css keeps every R66 / R51b selector untouched
        (no removal of ``.topnav``, ``.hero``, ``.status-bar``,
        ``.panel``, ``.data-table``, ``.toast``, ``.range-btn``,
        ``.refresh-btn``, ``.icon-btn``).
  T15 — ``prefers-reduced-motion`` block exists in oobe.css and
        admin.css too (not only dashboard.css).
  T16 — ``py_compile`` + ``ast.parse`` pass on the test file
        itself (i.e. the AST guard itself is syntactically valid).
"""
from __future__ import annotations

import ast
import py_compile
import re
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
CSS_DIR = PROJ_DIR / "static" / "css"
THIS_TEST = PROJ_DIR / "tests" / "modern" / "test_round67.py"

CSS_FILES = ("dashboard.css", "oobe.css", "admin.css")


def _read(name: str) -> str:
    return (CSS_DIR / name).read_text(encoding="utf-8")


def _count_media(css: str) -> int:
    """Count @media rule blocks (open-brace counted, comment-aware)."""
    stripped = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    return len(re.findall(r"@media\b[^{]*\{", stripped))


# =========================================================================
# T1 — every CSS file exists on disk and is non-empty
# =========================================================================
class TestCSSFilesExist(unittest.TestCase):
    """R67 touches three CSS files; all must exist and be non-trivial."""

    def test_each_css_file_on_disk(self) -> None:
        for fname in CSS_FILES:
            p = CSS_DIR / fname
            self.assertTrue(p.exists(), f"missing CSS file: {p}")
            self.assertGreater(
                p.stat().st_size, 200,
                f"{fname} suspiciously small ({p.stat().st_size} bytes)",
            )


# =========================================================================
# T2 — every CSS file declares >= 4 @media rules
# =========================================================================
class TestMediaQueryCount(unittest.TestCase):
    """R67 expects at least 4 @media rules per CSS file."""

    def test_each_has_at_least_4_media_rules(self) -> None:
        for fname in CSS_FILES:
            css = _read(fname)
            count = _count_media(css)
            self.assertGreaterEqual(
                count, 4,
                f"{fname} must have >= 4 @media rules (got {count})",
            )


# =========================================================================
# T3 — every CSS file declares all 4 named breakpoints
# =========================================================================
class TestFourBreakpoints(unittest.TestCase):
    """Each CSS must cover Mobile/Tablet/Desktop/Wide breakpoints."""

    BREAKPOINTS = {
        "mobile":  re.compile(r"@media[^{]*max-width:\s*767"),
        "tablet":  re.compile(
            r"@media[^{]*min-width:\s*768[^{]*max-width:\s*1023"
        ),
        "desktop": re.compile(
            r"@media[^{]*min-width:\s*1024[^{]*max-width:\s*1439"
        ),
        "wide":    re.compile(r"@media[^{]*min-width:\s*1440"),
    }

    def test_all_breakpoints_in_each_css(self) -> None:
        for fname in CSS_FILES:
            css = _read(fname)
            for label, pat in self.BREAKPOINTS.items():
                self.assertRegex(
                    css, pat,
                    f"{fname} missing {label} breakpoint media query",
                )


# =========================================================================
# T4 — dashboard.css has orientation media query
# =========================================================================
class TestOrientationMediaQuery(unittest.TestCase):
    """Tablet portrait/landscape switching needs an orientation query."""

    def test_dashboard_has_orientation_query(self) -> None:
        css = _read("dashboard.css")
        self.assertRegex(
            css, r"@media[^{]*orientation:\s*(landscape|portrait)",
            "dashboard.css must include an orientation media query",
        )


# =========================================================================
# T5 — dashboard.css gates hover affordances with (hover: hover|hover: none)
# =========================================================================
class TestHoverCapabilityGating(unittest.TestCase):
    """Hover effects must only fire on mouse/trackpad devices."""

    def test_dashboard_gates_hover(self) -> None:
        css = _read("dashboard.css")
        has_hover_hover = bool(
            re.search(r"@media[^{]*\(\s*hover:\s*hover\s*\)", css)
        )
        has_hover_none = bool(
            re.search(r"@media[^{]*\(\s*hover:\s*none\s*\)", css)
        )
        self.assertTrue(
            has_hover_hover or has_hover_none,
            "dashboard.css must gate hover with @media (hover: hover) "
            "or @media (hover: none)",
        )


# =========================================================================
# T6 — dashboard.css honors prefers-reduced-motion
# =========================================================================
class TestReducedMotionDashboard(unittest.TestCase):
    """Reduced-motion users get animation-disabled styles."""

    def test_dashboard_reduced_motion(self) -> None:
        css = _read("dashboard.css")
        self.assertIn(
            "prefers-reduced-motion",
            css,
            "dashboard.css must include prefers-reduced-motion",
        )


# =========================================================================
# T7 — touch target min-height 44px somewhere
# =========================================================================
class TestTouchTarget44(unittest.TestCase):
    """Apple HIG / WCAG: tap targets >= 44x44 px."""

    def test_min_height_44_present(self) -> None:
        found = False
        for fname in CSS_FILES:
            css = _read(fname)
            if "44px" in css and re.search(r"min-height:\s*44px", css):
                found = True
                break
        self.assertTrue(
            found,
            "at least one CSS file must declare min-height: 44px for tap targets",
        )


# =========================================================================
# T8 — iOS zoom prevention: input font-size 16px
# =========================================================================
class TestInputFontSize16(unittest.TestCase):
    """iOS zooms the viewport on focus if input font < 16 px."""

    def test_input_16px_present(self) -> None:
        """iOS zooms the viewport on focus if input font < 16 px.

        R67 declares ``font-size: 16px`` inside input rule blocks.
        The selector may be ``input``, ``.filter-bar input``, or
        ``input[type="text"]`` — we accept any block whose selector
        references ``input`` and whose body declares 16 px.
        """
        found = False
        for fname in CSS_FILES:
            css = _read(fname)
            # Iterate every rule block whose selector mentions input;
            # check whether its body declares font-size: 16px.
            for m in re.finditer(
                r"([^{}]*?input[^{}]*?)\{([^{}]*)\}",
                css,
            ):
                selector, body = m.group(1), m.group(2)
                if "input" not in selector:
                    continue
                if re.search(r"font-size:\s*16px", body):
                    found = True
                    break
            if found:
                break
        self.assertTrue(
            found,
            "at least one CSS file must declare font-size: 16px on inputs",
        )


# =========================================================================
# T9 — responsive grid via auto-fit / minmax
# =========================================================================
class TestResponsiveGrid(unittest.TestCase):
    """Card grid must adapt column count without JS — use auto-fit/minmax."""

    def test_auto_fit_or_minmax_present(self) -> None:
        found = False
        for fname in CSS_FILES:
            css = _read(fname)
            if "auto-fit" in css or re.search(r"minmax\s*\(", css):
                found = True
                break
        self.assertTrue(
            found,
            "at least one CSS file must use auto-fit or minmax() for "
            "responsive grids",
        )


# =========================================================================
# T10 — each CSS caps container width with max-width
# =========================================================================
class TestCenteredMaxWidth(unittest.TestCase):
    """Page wrappers are centered via max-width + margin: 0 auto."""

    def test_max_width_in_each(self) -> None:
        for fname in CSS_FILES:
            css = _read(fname)
            self.assertRegex(
                css, r"max-width:\s*\d+",
                f"{fname} must declare at least one max-width container",
            )


# =========================================================================
# T11 — dashboard.css exposes iOS safe-area env() inset
# =========================================================================
class TestSafeAreaInsets(unittest.TestCase):
    """iPhone notch / home indicator needs env(safe-area-inset-*)."""

    def test_dashboard_safe_area(self) -> None:
        css = _read("dashboard.css")
        self.assertIn(
            "env(safe-area-inset",
            css,
            "dashboard.css must use env(safe-area-inset-*) for iOS notch",
        )


# =========================================================================
# T12 — backdrop-filter disabled on mobile
# =========================================================================
class TestBackdropFilterPerfKillSwitch(unittest.TestCase):
    """Backdrop-filter on phones tanks scroll FPS; R67 must disable it."""

    def test_dashboard_kills_backdrop_on_mobile(self) -> None:
        css = _read("dashboard.css")
        # Find every @media block whose condition includes max-width: 767,
        # then check whether any of those blocks contain
        # `backdrop-filter: none`.
        blocks = re.findall(
            r"@media\s*\([^{}]*?max-width:\s*767[^{}]*?\)\s*\{([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}",
            css,
        )
        self.assertGreater(
            len(blocks), 0,
            "dashboard.css must contain at least one @media block for "
            "max-width: 767px",
        )
        killed = any(
            "backdrop-filter" in blk and "none" in blk
            for blk in blocks
        )
        self.assertTrue(
            killed,
            "dashboard.css must set `backdrop-filter: none` inside the "
            "max-width: 767px media block",
        )


# =========================================================================
# T13 — chart DPR cap custom property for chart perf
# =========================================================================
class TestChartDPRCap(unittest.TestCase):
    """R67 introduces --chart-dpr-cap so dashboard.js can throttle canvas."""

    def test_chart_dpr_cap_declared(self) -> None:
        css = _read("dashboard.css")
        self.assertIn(
            "--chart-dpr-cap",
            css,
            "dashboard.css must declare --chart-dpr-cap custom property",
        )


# =========================================================================
# T14 — R51b / R66 selectors preserved
# =========================================================================
class TestLegacySelectorsPreserved(unittest.TestCase):
    """R67 is purely additive; the original selectors must still resolve."""

    REQUIRED_SELECTORS = (
        ".topnav", ".topnav-inner", ".brand", ".brand-mark", ".tabs",
        ".tab", ".nav-actions", ".icon-btn", ".refresh-btn",
        ".hero", ".hero-label", ".hero-value", ".hero-meta",
        ".status-dot", ".grid", ".grid-3", ".card", ".card-label",
        ".card-value", ".card-foot", ".panel", ".panel-head",
        ".panel-title", ".range-group", ".range-btn", ".chart-wrap",
        ".filter-bar", ".btn", ".btn-primary", ".data-table",
        ".list-row", ".list-left", ".list-right", ".list-title",
        ".list-value", ".tag", ".meter-grid", ".status-bar", ".toast",
        ".toast-container", ".empty-state",
    )

    def test_dashboard_keeps_legacy_selectors(self) -> None:
        css = _read("dashboard.css")
        for sel in self.REQUIRED_SELECTORS:
            self.assertIn(
                sel, css,
                f"dashboard.css must still contain selector {sel}",
            )


# =========================================================================
# T15 — reduced-motion block in oobe.css AND admin.css
# =========================================================================
class TestReducedMotionAllCSS(unittest.TestCase):
    """Reduced-motion rules must apply to every page, not just dashboard."""

    def test_oobe_reduced_motion(self) -> None:
        css = _read("oobe.css")
        self.assertIn(
            "prefers-reduced-motion",
            css,
            "oobe.css must include prefers-reduced-motion",
        )

    def test_admin_reduced_motion(self) -> None:
        css = _read("admin.css")
        self.assertIn(
            "prefers-reduced-motion",
            css,
            "admin.css must include prefers-reduced-motion",
        )


# =========================================================================
# T16 — py_compile + ast.parse on the test file itself
# =========================================================================
class TestSelfCompiles(unittest.TestCase):
    """The AST guard itself must be syntactically valid Python."""

    def test_py_compile(self) -> None:
        py_compile.compile(str(THIS_TEST), doraise=True)

    def test_ast_parse(self) -> None:
        tree = ast.parse(THIS_TEST.read_text(encoding="utf-8"))
        self.assertIsInstance(tree, ast.Module)
        # At least 8 top-level class statements.
        classes = [n for n in tree.body if isinstance(n, ast.ClassDef)]
        self.assertGreaterEqual(
            len(classes), 8,
            f"expected >= 8 test classes, found {len(classes)}",
        )


if __name__ == "__main__":
    unittest.main()