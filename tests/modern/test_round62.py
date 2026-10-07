"""R62 — split web.py HTML/CSS/JS into templates/ + static/.

Background
==========
Pre-R62 ``web.py`` was 3654 lines and contained three giant triple-quoted
HTML strings (``INDEX_HTML`` 1527 lines, ``OOBE_HTML`` 244 lines,
``ADMIN_HTML`` 309 lines) that embedded inline CSS + JS.  R62 keeps the
Python business logic untouched and only changes how the templates are
loaded:

  * Each HTML string moves to ``templates/{dashboard,oobe,admin}.html``
    with the Jinja placeholders intact.
  * CSS inside ``<style>...</style>`` moves to ``static/css/*.css``.
  * JS inside ``<script>...</script>`` moves to ``static/js/*.js``.
  * Jinja placeholders that lived inside ``<script>`` blocks are moved
    out: dashboard uses ``window.__DASHBOARD_DATA__``, oobe uses a
    ``data-step`` attribute on ``<body>``.
  * Fonts at ``assets/fonts/`` are *copied* to ``static/fonts/`` so
    Flask's default static serving works.  The originals stay because
    ``feishu_bot.py`` (out of scope for R62) hard-codes ``assets/fonts``.
  * ``web.py`` keeps the module-level ``INDEX_HTML`` / ``OOBE_HTML`` /
    ``ADMIN_HTML`` constants but loads them via a single ``_load_template``
    helper.  The route handlers still call
    ``render_template_string(INDEX_HTML, rows=...)``.

Coverage (7 static testcases):

  T1 ``test_templates_exist``
       — ``templates/{dashboard,oobe,admin}.html`` all on disk.

  T2 ``test_static_assets_exist``
       — ``static/css/*.css``, ``static/js/*.js``, ``static/fonts/*.ttf``
         all on disk.

  T3 ``test_web_py_loads_from_disk``
       — ``web.py`` has a ``_load_template(name)`` function.

  T4 ``test_dashboard_html_has_jinja``
       — ``templates/dashboard.html`` retains at least one
         Jinja placeholder + the ``__DASHBOARD_DATA__`` injection block.

  T5 ``test_css_not_inline_in_html``
       — None of the three templates has an inline ``<style>`` block.

  T6 ``test_js_not_inline_in_html``
       — Each template references its external JS file via ``<script src=>``.

  T7 ``test_py_compile``
       — ``py_compile`` + ``ast.parse`` both pass on ``web.py`` and the
         test file itself.
"""
from __future__ import annotations

import ast
import py_compile
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
WEB_PY = PROJ_DIR / "web.py"
TEMPLATES = PROJ_DIR / "templates"
CSS_DIR = PROJ_DIR / "static" / "css"
JS_DIR = PROJ_DIR / "static" / "js"
FONTS_DIR = PROJ_DIR / "static" / "fonts"
THIS_TEST = PROJ_DIR / "tests" / "modern" / "test_round62.py"

TEMPLATE_NAMES = ("dashboard", "oobe", "admin")
CSS_FILES = tuple(f"{n}.css" for n in TEMPLATE_NAMES)
JS_FILES = tuple(f"{n}.js" for n in TEMPLATE_NAMES)
FONT_FILES = (
    "MiSans-Regular.ttf",
    "MiSans-Bold.ttf",
    "NotoEmoji-Regular.ttf",
)


# =========================================================================
# T1 — templates exist on disk
# =========================================================================
class TestTemplatesExist(unittest.TestCase):
    """Each template file must exist in templates/."""

    def test_templates_exist(self) -> None:
        for name in TEMPLATE_NAMES:
            p = TEMPLATES / f"{name}.html"
            self.assertTrue(
                p.exists(),
                f"missing template: {p}",
            )
            self.assertGreater(
                p.stat().st_size, 0,
                f"empty template: {p}",
            )


# =========================================================================
# T2 — static assets exist on disk
# =========================================================================
class TestStaticAssetsExist(unittest.TestCase):
    """CSS, JS, and font files must be in static/."""

    def test_css_files_exist(self) -> None:
        for name in CSS_FILES:
            p = CSS_DIR / name
            self.assertTrue(
                p.exists(),
                f"missing CSS: {p}",
            )
            self.assertGreater(
                p.stat().st_size, 100,
                f"CSS suspiciously small: {p}",
            )

    def test_js_files_exist(self) -> None:
        for name in JS_FILES:
            p = JS_DIR / name
            self.assertTrue(
                p.exists(),
                f"missing JS: {p}",
            )
            self.assertGreater(
                p.stat().st_size, 100,
                f"JS suspiciously small: {p}",
            )

    def test_font_files_exist(self) -> None:
        for name in FONT_FILES:
            p = FONTS_DIR / name
            self.assertTrue(
                p.exists(),
                f"missing font: {p}",
            )
            self.assertGreater(
                p.stat().st_size, 100_000,
                f"font suspiciously small (not a real TTF?): {p}",
            )


# =========================================================================
# T3 — web.py has _load_template helper + file-loader calls
# =========================================================================
class TestWebPyLoadsFromDisk(unittest.TestCase):
    """web.py must define _load_template and call it for each template."""

    def test_web_py_has_load_template_function(self) -> None:
        src = WEB_PY.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        self.assertIn(
            "_load_template", fn_names,
            "web.py must define _load_template(name) function",
        )

    def test_web_py_uses_load_template_for_constants(self) -> None:
        """Each template constant must be assigned via _load_template."""
        src = WEB_PY.read_text(encoding="utf-8")
        for const_name, page_name in (
            ("INDEX_HTML",  "dashboard"),
            ("OOBE_HTML",   "oobe"),
            ("ADMIN_HTML",  "admin"),
        ):
            self.assertIn(
                f'{const_name} = _load_template("{page_name}.html")',
                src,
                f"{const_name} must be loaded from templates/{page_name}.html",
            )

    def test_web_py_no_giant_triple_quoted_html(self) -> None:
        """The three big triple-quoted blocks must be GONE from web.py."""
        src = WEB_PY.read_text(encoding="utf-8")
        # The big HTML blocks used to start with their header line directly
        # after `NAME = """<!doctype html>`.  Make sure none remain.
        for const_name in ("INDEX_HTML", "OOBE_HTML", "ADMIN_HTML"):
            bad = f'{const_name} = """<!doctype html>'
            self.assertNotIn(
                bad, src,
                f"{const_name} still inline as triple-quoted block — "
                f"should be loaded from disk",
            )


# =========================================================================
# T4 — dashboard.html keeps Jinja + adds data injection
# =========================================================================
class TestDashboardJinja(unittest.TestCase):
    """dashboard.html must still render Jinja from server side."""

    def test_dashboard_has_jinja_placeholder(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        # Must have at least one of the original Jinja placeholders.
        has = any(
            needle in html
            for needle in (
                "{{ stats", "{{ rows", "{{ daily_elec",
                "{{ hours", "{{ initialLatestTs", "{{ run_status",
                "{{ violations", "{{ pay_history",
                "{% for", "{% if",
            )
        )
        self.assertTrue(
            has,
            "templates/dashboard.html lost all its Jinja placeholders — "
            "render_template_string will produce blank pages",
        )

    def test_dashboard_has_data_injection_block(self) -> None:
        """dashboard.html must inject __DASHBOARD_DATA__ for the JS file."""
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn(
            "window.__DASHBOARD_DATA__",
            html,
            "dashboard.html must define window.__DASHBOARD_DATA__ "
            "so dashboard.js can read its initial state",
        )

    def test_oobe_has_step_placeholder(self) -> None:
        """oobe.html keeps the {{ step }} Jinja (on body data-step)."""
        html = (TEMPLATES / "oobe.html").read_text(encoding="utf-8")
        self.assertIn(
            'data-step="{{ step }}"',
            html,
            "oobe.html must keep {{ step }} as body data-step attribute",
        )

    def test_oobe_js_reads_from_dataset(self) -> None:
        """oobe.js must read step from document.body.dataset.step."""
        js = (JS_DIR / "oobe.js").read_text(encoding="utf-8")
        self.assertIn(
            "document.body.dataset.step",
            js,
            "oobe.js must read step from body dataset (no inline Jinja)",
        )


# =========================================================================
# T5 — no inline <style> blocks in any template
# =========================================================================
class TestCssNotInline(unittest.TestCase):
    """CSS must be external; templates must not have inline <style> blocks."""

    def test_no_inline_style_in_dashboard(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        self.assertNotIn(
            "<style>", html,
            "templates/dashboard.html still has an inline <style> block",
        )

    def test_no_inline_style_in_oobe(self) -> None:
        html = (TEMPLATES / "oobe.html").read_text(encoding="utf-8")
        self.assertNotIn(
            "<style>", html,
            "templates/oobe.html still has an inline <style> block",
        )

    def test_no_inline_style_in_admin(self) -> None:
        html = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
        self.assertNotIn(
            "<style>", html,
            "templates/admin.html still has an inline <style> block",
        )

    def test_css_files_have_real_rules(self) -> None:
        """Each CSS file should contain real CSS, not be empty."""
        for name in TEMPLATE_NAMES:
            css = (CSS_DIR / f"{name}.css").read_text(encoding="utf-8")
            # :root { is the universal design-system anchor used by all 3
            # templates — a strong "real CSS" signal.
            self.assertIn(
                ":root", css,
                f"static/css/{name}.css looks empty — no :root rule",
            )


# =========================================================================
# T6 — each template references its external JS file
# =========================================================================
class TestJsExternal(unittest.TestCase):
    """Each template must <script src=> its external JS file."""

    def test_dashboard_references_external_js(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn(
            'src="/static/js/dashboard.js"',
            html,
            "dashboard.html must <script src=/static/js/dashboard.js>",
        )

    def test_oobe_references_external_js(self) -> None:
        html = (TEMPLATES / "oobe.html").read_text(encoding="utf-8")
        self.assertIn(
            'src="/static/js/oobe.js"',
            html,
            "oobe.html must <script src=/static/js/oobe.js>",
        )

    def test_admin_references_external_js(self) -> None:
        html = (TEMPLATES / "admin.html").read_text(encoding="utf-8")
        self.assertIn(
            'src="/static/js/admin.js"',
            html,
            "admin.html must <script src=/static/js/admin.js>",
        )

    def test_js_files_have_no_jinja(self) -> None:
        """JS files must NOT contain Jinja — it's a static-asset contract."""
        for name in TEMPLATE_NAMES:
            js = (JS_DIR / f"{name}.js").read_text(encoding="utf-8")
            self.assertNotIn(
                "{{", js,
                f"static/js/{name}.js contains Jinja placeholder — "
                f"server-side substitution does NOT happen for static files",
            )
            self.assertNotIn(
                "{%", js,
                f"static/js/{name}.js contains Jinja control block — "
                f"server-side substitution does NOT happen for static files",
            )


# =========================================================================
# T7 — py_compile + ast.parse passes
# =========================================================================
class TestPyCompile(unittest.TestCase):
    """web.py and the test itself must parse cleanly."""

    def test_py_compile_passes(self) -> None:
        py_compile.compile(str(WEB_PY), doraise=True)
        py_compile.compile(str(THIS_TEST), doraise=True)
        # AST parse catches structural problems py_compile might miss.
        ast.parse(WEB_PY.read_text(encoding="utf-8"))
        ast.parse(THIS_TEST.read_text(encoding="utf-8"))
        print(f"  [py_compile OK] web.py + test_round62.py")


# =========================================================================
# Run with ``python -m unittest tests.modern.test_round62``
# =========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)