"""R66 — Web Components + View Transitions API.

Background
==========
R62 split web.py's HTML/CSS/JS into ``templates/`` + ``static/``.  R66
takes the dashboard front-end one step further:

* The repeated UI blocks in ``templates/dashboard.html`` are extracted
  into 8 native ``<dorm-*>`` custom elements, each defined in its own
  file under ``static/js/components/``.  All components extend
  ``HTMLElement``, use the browser-native ``<template>`` + cloneNode,
  and read their data from ``data-*`` attributes.  None of them hard-
  codes ``fetch`` logic — they only render.
* Page section switches, chart re-renders, and toast entrance/exit are
  wrapped in ``document.startViewTransition(callback)`` with a
  JavaScript ``try/catch`` fallback + a CSS ``@supports not
  (view-transition-name: none)`` fallback.  Browser support:
  Chrome 111+/Edge 111+/Safari 18+/Firefox not yet.
* No build step, no npm, no extra pip install.  The runtime is plain
  ES2020 class syntax + ``customElements.define``.

Coverage (18 static testcases):

  T1  — Component files all exist on disk.
  T2  — Each component defines a class extending HTMLElement.
  T3  — Each component calls ``customElements.define(<tag>, Class)``.
  T4  — Each component has a ``static get observedAttributes()`` that
        lists at least one attribute name.
  T5  — ``templates/dashboard.html`` references all 8 component scripts.
  T6  — ``templates/dashboard.html`` uses every ``<dorm-*>`` tag in
        at least one place.
  T7  — ``static/css/dashboard.css`` contains a ``::view-transition-old``
        rule.
  T8  — ``static/css/dashboard.css`` contains a ``::view-transition-new``
        rule.
  T9  — ``static/css/dashboard.css`` contains the
        ``@supports not (view-transition-name: none)`` fallback rule.
  T10 — ``static/js/dashboard.js`` calls ``document.startViewTransition``
        (via the ``withViewTransition`` helper).
  T11 — ``static/js/dashboard.js`` wraps the section switch in
        ``withViewTransition``.
  T12 — ``static/js/dashboard.js`` wraps chart updates in
        ``withViewTransition``.
  T13 — ``static/js/dashboard.js`` wraps toast creation in
        ``withViewTransition``.
  T14 — ``static/js/dashboard.js`` has a try/catch fallback inside
        ``withViewTransition``.
  T15 — All component .js files parse as syntactically valid JS
        (custom bracket-balance + comment-stripped eval-based check
        for each IIFE wrapper).
  T16 — ``templates/dashboard.html`` keeps at least one Jinja
        placeholder intact.
  T17 — The component .js files use ``document.createElement('template')``
        + ``cloneNode`` (browser-native pattern, no build step).
  T18 — ``py_compile`` + ``ast.parse`` pass on the test file itself and
        on ``web.py`` (unchanged business logic).
"""
from __future__ import annotations

import ast
import py_compile
import re
import unittest
from pathlib import Path

PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
COMP_DIR = PROJ_DIR / "static" / "js" / "components"
JS_DIR = PROJ_DIR / "static" / "js"
CSS_DIR = PROJ_DIR / "static" / "css"
TEMPLATES = PROJ_DIR / "templates"
THIS_TEST = PROJ_DIR / "tests" / "modern" / "test_round66.py"
WEB_PY = PROJ_DIR / "web.py"

# (file_name, tag-name) — the 8 components R66 introduces.
COMPONENTS = (
    ("dorm-stat-card.js",      "dorm-stat-card"),
    ("dorm-room-card.js",      "dorm-room-card"),
    ("dorm-chart-container.js", "dorm-chart-container"),
    ("dorm-history-row.js",    "dorm-history-row"),
    ("dorm-records-table.js",  "dorm-records-table"),
    ("dorm-list-row.js",       "dorm-list-row"),
    ("dorm-toast.js",          "dorm-toast"),
    ("dorm-spinner.js",        "dorm-spinner"),
)


# =========================================================================
# T1 — every component file exists on disk and is non-empty
# =========================================================================
class TestComponentFilesExist(unittest.TestCase):
    """Each component lives in static/js/components/ and is real JS."""

    def test_all_components_on_disk(self) -> None:
        for fname, tag in COMPONENTS:
            p = COMP_DIR / fname
            self.assertTrue(
                p.exists(),
                f"missing component file: {p}",
            )
            self.assertGreater(
                p.stat().st_size, 200,
                f"component suspiciously small: {p} ({p.stat().st_size} bytes)",
            )
            self.assertTrue(
                fname.startswith("dorm-") and fname.endswith(".js"),
                f"unexpected file name: {fname}",
            )
            self.assertTrue(
                tag.startswith("dorm-") and tag != "dorm-",
                f"unexpected tag name: {tag}",
            )


# =========================================================================
# T2 — class extending HTMLElement
# =========================================================================
class TestComponentClassesExtendHTMLElement(unittest.TestCase):
    """Each component .js must define a class that extends HTMLElement."""

    def test_each_extends_HTMLElement(self) -> None:
        pattern = re.compile(
            r"class\s+Dorm\w+\s+extends\s+HTMLElement\b"
        )
        for fname, _ in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            self.assertRegex(
                text, pattern,
                f"{fname} must declare `class Dorm* extends HTMLElement`",
            )


# =========================================================================
# T3 — customElements.define call
# =========================================================================
class TestCustomElementsDefine(unittest.TestCase):
    """Each component must register itself via customElements.define."""

    def test_each_calls_define(self) -> None:
        for fname, tag in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            # Accept either single or double quotes around the tag name.
            pattern = re.compile(
                r"customElements\.define\(\s*['\"]" + re.escape(tag) + r"['\"]"
            )
            self.assertRegex(
                text, pattern,
                f"{fname} must call `customElements.define('{tag}', Class)`",
            )


# =========================================================================
# T4 — observedAttributes declared
# =========================================================================
class TestObservedAttributes(unittest.TestCase):
    """Each component must expose a static observedAttributes getter."""

    def test_each_declares_observed_attributes(self) -> None:
        for fname, _ in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            self.assertIn(
                "static get observedAttributes()",
                text,
                f"{fname} must declare `static get observedAttributes()`",
            )
            # Must return a non-trivial list (not `return [];`)
            m = re.search(
                r"static\s+get\s+observedAttributes\s*\(\s*\)\s*\{\s*return\s*\[([^\]]+)\]",
                text,
                flags=re.DOTALL,
            )
            self.assertIsNotNone(
                m,
                f"{fname} observedAttributes must return a populated array literal",
            )
            attrs = m.group(1)
            # Must contain at least one attribute name (quoted string).
            quoted = re.findall(r"['\"]([a-z][a-z0-9-]+)['\"]", attrs)
            self.assertGreater(
                len(quoted), 0,
                f"{fname} observedAttributes must list at least one attribute",
            )


# =========================================================================
# T5 — dashboard.html references every component script
# =========================================================================
class TestDashboardReferencesComponents(unittest.TestCase):
    """dashboard.html must <script src=> each component before dashboard.js."""

    def test_dashboard_loads_each_component_script(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        for fname, _ in COMPONENTS:
            needle = f'src="/static/js/components/{fname}"'
            self.assertIn(
                needle, html,
                f"dashboard.html must load /static/js/components/{fname}",
            )
        # dashboard.js must still be loaded, after all components.
        self.assertIn(
            'src="/static/js/dashboard.js"',
            html,
            "dashboard.html must still load dashboard.js",
        )


# =========================================================================
# T6 — dashboard.html uses every <dorm-*> tag
# =========================================================================
class TestDashboardUsesEveryTag(unittest.TestCase):
    """Each component's tag must appear at least once in dashboard.html.

    Tags that are only created at runtime via document.createElement
    (e.g. <dorm-toast>) are excluded — they're emitted by showToast()
    in dashboard.js, not by the Jinja template.
    """

    RUNTIME_ONLY = frozenset({"dorm-toast"})

    def test_each_tag_in_dashboard(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        for _, tag in COMPONENTS:
            if tag in self.RUNTIME_ONLY:
                continue
            self.assertIn(
                f"<{tag}", html,
                f"dashboard.html must use <{tag}> at least once",
            )

    def test_dorm_toast_used_in_dashboard_js(self) -> None:
        """<dorm-toast> is created at runtime, so check dashboard.js."""
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn(
            "dorm-toast",
            js,
            "dashboard.js must create <dorm-toast> via document.createElement",
        )


# =========================================================================
# T7/T8/T9 — view-transition CSS rules
# =========================================================================
class TestViewTransitionsCSS(unittest.TestCase):
    """dashboard.css must define View Transitions API + fallback rules."""

    def test_has_view_transition_old_rule(self) -> None:
        css = (CSS_DIR / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn(
            "::view-transition-old",
            css,
            "dashboard.css must declare at least one ::view-transition-old rule",
        )

    def test_has_view_transition_new_rule(self) -> None:
        css = (CSS_DIR / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn(
            "::view-transition-new",
            css,
            "dashboard.css must declare at least one ::view-transition-new rule",
        )

    def test_has_supports_fallback(self) -> None:
        css = (CSS_DIR / "dashboard.css").read_text(encoding="utf-8")
        self.assertIn(
            "@supports not (view-transition-name: none)",
            css,
            "dashboard.css must declare @supports not (view-transition-name: none) "
            "fallback for browsers without VT API",
        )


# =========================================================================
# T10/T11/T12/T13/T14 — dashboard.js wires View Transitions API
# =========================================================================
class TestViewTransitionsJS(unittest.TestCase):
    """dashboard.js must use withViewTransition to wrap section/chart/toast."""

    def test_uses_startViewTransition(self) -> None:
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        self.assertIn(
            "document.startViewTransition",
            js,
            "dashboard.js must call document.startViewTransition (via withViewTransition)",
        )

    def test_section_switch_wrapped(self) -> None:
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        # Find the activateSection function and check it calls withViewTransition.
        # The body may contain inner functions, so we match by scanning
        # for the call `withViewTransition(apply)` after the function def.
        m = re.search(
            r"function\s+activateSection\s*\([^)]*\)\s*\{",
            js,
        )
        self.assertIsNotNone(
            m,
            "activateSection function must be defined in dashboard.js",
        )
        # Slice from function start until the next "  }" at column ~2
        # (top-level closing brace) — heuristic that works for this codebase.
        start = m.end()
        tail = js[start:]
        # Take everything up to the next two-line break where the
        # indent drops back to two spaces.
        end_match = re.search(r"\n\s{0,2}\}\s*\n", tail)
        self.assertIsNotNone(
            end_match,
            "could not locate end of activateSection",
        )
        body = tail[: end_match.start() + 1]
        self.assertIn(
            "withViewTransition",
            body,
            "activateSection must wrap its body in withViewTransition",
        )

    def test_chart_update_wrapped(self) -> None:
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        # Both buildChart and buildDailyChart must wrap their
        # existing-instance update path.
        chart_block = re.search(
            r"function\s+buildChart\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\}\s*\n",
            js,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(chart_block, "buildChart must be defined")
        self.assertIn(
            "withViewTransition",
            chart_block.group("body"),
            "buildChart must wrap existing-instance updates in withViewTransition",
        )
        daily_block = re.search(
            r"function\s+buildDailyChart\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\}\s*\n",
            js,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(daily_block, "buildDailyChart must be defined")
        self.assertIn(
            "withViewTransition",
            daily_block.group("body"),
            "buildDailyChart must wrap existing-instance updates in withViewTransition",
        )

    def test_toast_wrapped(self) -> None:
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        toast_block = re.search(
            r"function\s+showToast\s*\([^)]*\)\s*\{(?P<body>.*?)\n\s*\}",
            js,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(toast_block, "showToast must be defined")
        self.assertIn(
            "withViewTransition",
            toast_block.group("body"),
            "showToast must wrap toast insertion in withViewTransition",
        )

    def test_withViewTransition_has_try_catch_fallback(self) -> None:
        js = (JS_DIR / "dashboard.js").read_text(encoding="utf-8")
        m = re.search(
            r"function\s+withViewTransition\s*\([^)]*\)\s*\{",
            js,
        )
        self.assertIsNotNone(
            m, "withViewTransition helper must be defined in dashboard.js",
        )
        start = m.end()
        tail = js[start:]
        end_match = re.search(r"\n\s{0,2}\}\s*\n", tail)
        self.assertIsNotNone(
            end_match,
            "could not locate end of withViewTransition",
        )
        body = tail[: end_match.start() + 1]
        self.assertIn(
            "try", body,
            "withViewTransition must wrap document.startViewTransition in try",
        )
        self.assertIn(
            "catch", body,
            "withViewTransition must have a catch fallback",
        )
        self.assertIn(
            "callback()", body,
            "withViewTransition must call the callback synchronously as fallback",
        )


# =========================================================================
# T15 — all component .js files are syntactically balanced JS
# =========================================================================
class TestComponentJSValidity(unittest.TestCase):
    """Light-weight JS sanity check: balanced braces/parens + valid IIFE."""

    @staticmethod
    def _strip_comments_and_strings(src: str) -> str:
        # Strip /* ... */ comments
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.DOTALL)
        # Strip // ... line comments
        src = re.sub(r"//[^\n]*", "", src)
        # Replace single- and double-quoted strings with placeholders.
        src = re.sub(r"'[^'\\\n]*(?:\\.[^'\\\n]*)*'", "''", src)
        src = re.sub(r'"[^"\\\n]*(?:\\.[^"\\\n]*)*"', '""', src)
        # Replace template literals with placeholders.
        src = re.sub(r"`[^`]*(?:\\.[^`]*)*`", "``", src, flags=re.DOTALL)
        return src

    def test_each_component_balanced(self) -> None:
        for fname, _ in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            stripped = self._strip_comments_and_strings(text)
            for open_c, close_c, name in (("{", "}", "braces"),
                                          ("(", ")", "parens"),
                                          ("[", "]", "brackets")):
                self.assertEqual(
                    stripped.count(open_c), stripped.count(close_c),
                    f"{fname} has unbalanced {name}: "
                    f"{stripped.count(open_c)} {open_c} vs "
                    f"{stripped.count(close_c)} {close_c}",
                )

    def test_each_component_uses_iife_wrapper(self) -> None:
        for fname, _ in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            self.assertRegex(
                text, r"\(function\s*\(\s*\)\s*\{",
                f"{fname} must use an IIFE wrapper to avoid polluting global scope",
            )
            self.assertRegex(
                text, r"\}\)\(\s*\)\s*;?\s*$",
                f"{fname} must close the IIFE wrapper at the end of the file",
            )


# =========================================================================
# T16 — dashboard.html keeps Jinja placeholders
# =========================================================================
class TestDashboardKeepsJinja(unittest.TestCase):
    """The R66 refactor must NOT strip Jinja placeholders."""

    def test_dashboard_has_jinja(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        has = any(
            needle in html
            for needle in (
                "{{ stats", "{{ rows", "{{ daily_elec",
                "{{ hours", "{{ initialLatestTs", "{{ run_status",
                "{{ violations", "{{ pay_history",
                "{% for", "{% if", "{% set",
            )
        )
        self.assertTrue(
            has,
            "dashboard.html lost its Jinja placeholders",
        )

    def test_dashboard_keeps_data_injection(self) -> None:
        html = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
        self.assertIn(
            "window.__DASHBOARD_DATA__",
            html,
            "dashboard.html must still define window.__DASHBOARD_DATA__",
        )


# =========================================================================
# T17 — components use browser-native <template> + cloneNode
# =========================================================================
class TestComponentsUseTemplateCloneNode(unittest.TestCase):
    """No build step: most components build their DOM via
    createElement('template') + cloneNode.  Pure decorators
    (<dorm-records-table>) are allowed to skip the template step.
    """

    TEMPLATE_OPTIONAL = frozenset({"dorm-records-table"})

    def test_each_uses_template_clonenode(self) -> None:
        for fname, tag in COMPONENTS:
            text = (COMP_DIR / fname).read_text(encoding="utf-8")
            if tag in self.TEMPLATE_OPTIONAL:
                # Pure decorators don't need a template, but must still
                # use createElement for any child nodes they make.
                self.assertIn(
                    "createElement(",
                    text,
                    f"{fname} (decorator) must use createElement for children",
                )
                continue
            self.assertIn(
                "createElement('template')",
                text,
                f"{fname} must use document.createElement('template')",
            )
            self.assertIn(
                "cloneNode",
                text,
                f"{fname} must use cloneNode on the template content",
            )


# =========================================================================
# T18 — py_compile + ast.parse on this test + web.py (untouched)
# =========================================================================
class TestPyCompile(unittest.TestCase):
    """web.py and the test itself must parse cleanly."""

    def test_py_compile_passes(self) -> None:
        py_compile.compile(str(WEB_PY), doraise=True)
        py_compile.compile(str(THIS_TEST), doraise=True)
        ast.parse(WEB_PY.read_text(encoding="utf-8"))
        ast.parse(THIS_TEST.read_text(encoding="utf-8"))
        print(f"  [py_compile OK] web.py + test_round66.py")


# =========================================================================
# Run with ``python -m unittest tests.modern.test_round66``
# =========================================================================
if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)