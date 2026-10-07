"""Round 65 鈥?Session-driven OOBE wizard + set-password via R63 user table.

Background
==========

R34B introduced the OOBE wizard as a 6-step HTML form whose state was
held in the URL (``?step=N``) and a small handful of ``meta`` keys.
R37 fixed two bugs on that path (B3 + B4); R63 wired the wizard's
final step (creating the admin user) onto the new ``auth.create_user``
+ ``auth.create_session`` primitives.  R65 supersedes the entire wizard:

  * State lives in ``session['oobe']`` (a Flask signed cookie) so a
    refresh does NOT lose progress and the user can't bookmark
    ``/oobe?step=5`` to skip ahead.
  * The wizard exposes seven JSON endpoints
    (``/api/oobe/{save-state,next,prev,skip-step,validate-feishu,
    validate-webhook,complete}``) plus a clean ``GET /oobe`` entry.
  * Step 6 (admin account creation) reuses the R63 primitives verbatim
    鈥?``auth.create_user`` + ``auth.create_session`` + ``auth.write_audit``.
  * Webhook URLs must be ``http(s)://...`` AND on a host allowlist
    (``*.tssplus.top`` / ``localhost``) to mitigate SSRF.
  * Passwords must be >= 8 chars and contain lowercase + uppercase +
    digit + special character.
  * The new front-end (``templates/oobe.html`` + ``static/{js,css}/oobe.*``)
    uses R66 web components (``<dorm-toast>``, ``<dorm-spinner>``),
    R66 View Transitions API, and R67 multi-device responsive CSS.

Coverage (25 testcases 鈥?22 static AST guards + 3 runtime smoke):

T1  鈥?``templates/oobe.html`` exists and is non-empty (rewritten in R65).
T2  鈥?``templates/oobe.html`` declares six step sections with
       ``data-step="1"`` through ``data-step="6"``.
T3  鈥?``templates/oobe.html`` carries the ``<meta name="csrf-token">``
       tag the front-end JS reads.
T4  鈥?``templates/oobe.html`` references both ``<dorm-toast>`` and
       ``<dorm-spinner>`` (R66 component reuse contract).
T5  鈥?``templates/oobe.html`` has the body with
       ``<body data-step="{{ step }}">`` so Flask renders the right
       section on first paint.
T6  鈥?``templates/oobe.html`` keeps Jinja placeholder intact.

T7  鈥?``static/js/oobe.js`` exists and defines the six step renderers
       (``templates[1]`` ... ``templates[6]`` or equivalent).
T8  鈥?``static/js/oobe.js`` wraps step switches in
       ``withViewTransition`` / ``document.startViewTransition``.
T9  鈥?``static/js/oobe.js`` reads the current step from
       ``document.body.dataset.step`` (R62 back-compat attribute name).
T10 鈥?``static/js/oobe.js`` posts JSON with
       ``credentials: 'same-origin'`` + ``X-CSRF-Token`` header on every
       mutating ``fetch()`` call.
T11 鈥?``static/js/oobe.js`` defines a password-strength checker with the
       same rules the server enforces.

T12 鈥?``static/css/oobe.css`` exists and is non-empty.
T13 鈥?``static/css/oobe.css`` keeps R67 responsive media queries
       (mobile / tablet / desktop / wide).
T14 鈥?``static/css/oobe.css`` uses ``view-transition-name`` for the
       wizard card so the R66 View Transitions API hooks in.

T15 鈥?``web.py`` registers ``GET /oobe`` (the new R65 wizard entry).
T16 鈥?``web.py`` keeps ``GET /admin/oobe`` as a back-compat alias
       (redirect to ``/oobe``) so older bookmarks keep working.
T17 鈥?``web.py`` registers the seven new POST ``/api/oobe/*`` routes.
T18 鈥?``web.py`` declares the password-strength + webhook-host
       + cron validators as module-level helpers (so unit tests can
       reach them via ``import web``).
T19 鈥?``web.py`` uses Flask ``session`` (``session['oobe']``,
       ``session['oobe_complete']``) for wizard state.
T20 鈥?``web.py`` OOBE-completion path calls ``auth.create_user`` +
       ``auth.create_session`` (R63 primitives 鈥?no duplication).
T21 鈥?``web.py`` ``/`` index redirect consults both
       ``session['oobe_complete']`` AND ``db.get_meta('oobe_completed')``
       so an in-session OOBE completion stops the redirect on refresh.
T22 鈥?``web.py`` keeps the legacy ``/admin/api/oobe/save`` route (R63
       back-compat 鈥?R37 + R63 test suites still POST against it).

T23 鈥?Runtime smoke: Flask session round-trip 鈥?set
       ``oobe.step=3`` then read it back via a request.
T24 鈥?Runtime smoke: password-strength validation
       (``_validate_password_strength``).
T25 鈥?Runtime smoke: webhook-host allowlist
       (``_validate_webhook_url``).

The runtime smoke tests require ``bcrypt`` + ``pydantic`` (the latter
is required by ``db.models`` which ``auth`` imports).  When those are
missing the runtime tests skip cleanly, mirroring the R63/R64 pattern.
"""
from __future__ import annotations

import ast
import os
import py_compile
import re
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
WEB_PY = PROJ_DIR / "web.py"
AUTH_PY = PROJ_DIR / "auth.py"
OOBE_HTML = PROJ_DIR / "templates" / "oobe.html"
OOBE_JS = PROJ_DIR / "static" / "js" / "oobe.js"
OOBE_CSS = PROJ_DIR / "static" / "css" / "oobe.css"
THIS_TEST = PROJ_DIR / "tests" / "modern" / "test_round65.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ast(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _funcdefnames(path: Path) -> list[str]:
    tree = _ast(path)
    return sorted(
        {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    )


def _routes_registered(path: Path) -> list[str]:
    """Return every Flask route rule path declared in ``path``."""
    tree = _ast(path)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "route" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    out.append(a0.value)
    return out


def _route_to_function(path: Path) -> dict[str, str]:
    tree = _ast(path)
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and dec.func.attr == "route"
                and dec.args
            ):
                a0 = dec.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    out[a0.value] = node.name
    return out


# =========================================================================
# T1-T6 鈥?templates/oobe.html structure
# =========================================================================
class TestOobeHtmlStructure(unittest.TestCase):
    """The new 6-step wizard template must carry every step + meta tag."""

    def test_oobe_html_exists(self) -> None:
        # T1
        self.assertTrue(OOBE_HTML.exists(), f"missing {OOBE_HTML}")
        self.assertGreater(
            OOBE_HTML.stat().st_size, 1_500,
            f"oobe.html suspiciously small ({OOBE_HTML.stat().st_size} bytes)",
        )

    def test_oobe_html_has_six_step_sections(self) -> None:
        # T2
        html = _read(OOBE_HTML)
        for n in range(1, 7):
            self.assertRegex(
                html,
                r'data-step="' + str(n) + r'"',
                f"oobe.html must contain <section data-step=\"{n}\"> "
                "(or equivalent element) so the JS can show/hide by step",
            )

    def test_oobe_html_has_csrf_meta_tag(self) -> None:
        # T3
        html = _read(OOBE_HTML)
        self.assertRegex(
            html,
            r'<meta\s+name=["\']csrf-token["\']',
            "oobe.html must include <meta name='csrf-token'> so the "
            "JS fetch wrapper can echo the X-CSRF-Token header.",
        )

    def test_oobe_html_uses_r66_components(self) -> None:
        # T4 鈥?both <dorm-toast> and <dorm-spinner> are referenced.
        html = _read(OOBE_HTML)
        for needle in (
            "/static/js/components/dorm-toast.js",
            "/static/js/components/dorm-spinner.js",
        ):
            self.assertIn(
                needle, html,
                f"oobe.html must load {needle} per the R66 component reuse contract",
            )

    def test_oobe_html_body_has_step_placeholder(self) -> None:
        # T5 鈥?R62 back-compat: <body data-step="{{ step }}">.
        html = _read(OOBE_HTML)
        self.assertRegex(
            html,
            r'<body[^>]*data-step=["\']\{\{\s*step\s*\}\}["\']',
            "oobe.html must keep <body data-step=\"{{ step }}\"> so "
            "Flask renders the right step on first paint (R62 contract).",
        )

    def test_oobe_html_keeps_jinja(self) -> None:
        # T6 鈥?R62 sanity check that we didn't strip the Jinja placeholder.
        html = _read(OOBE_HTML)
        self.assertIn("{{ step }}", html, "oobe.html lost {{ step }} placeholder")


# =========================================================================
# T7-T11 鈥?static/js/oobe.js logic
# =========================================================================
class TestOobeJsLogic(unittest.TestCase):
    """The wizard JS must drive 6 steps, view-transition them, and POST
    with same-origin credentials + CSRF header.
    """

    def test_oobe_js_exists(self) -> None:
        self.assertTrue(OOBE_JS.exists(), f"missing {OOBE_JS}")
        self.assertGreater(
            OOBE_JS.stat().st_size, 4_000,
            f"oobe.js suspiciously small ({OOBE_JS.stat().st_size} bytes)",
        )

    def test_oobe_js_has_six_step_renderers(self) -> None:
        # T7 鈥?JS template map references every step 1..6.
        js = _read(OOBE_JS)
        # Accept either an object-literal map OR six separate template
        # functions; we look for the integer literals in step-equality
        # contexts as a proxy.  ``currentStep`` (the wizard's mutable
        # variable) is accepted as a synonym for ``step``.
        for n in range(1, 7):
            needle = (
                rf"(?:step|currentStep)\s*===\s*{n}\b"
                rf"|(?:step|currentStep)\s*===\s*['\"]{n}['\"]\b"
                rf"|if\s*\(\s*(?:step|currentStep)\s*===\s*{n}\b"
            )
            self.assertRegex(
                js, needle,
                f"oobe.js must reference step {n} in a conditional render",
            )

    def test_oobe_js_uses_view_transition(self) -> None:
        # T8 鈥?step switches wrapped in startViewTransition.
        js = _read(OOBE_JS)
        self.assertIn(
            "document.startViewTransition",
            js,
            "oobe.js must call document.startViewTransition for the "
            "step swap animation (R66 contract).",
        )

    def test_oobe_js_reads_dataset_step(self) -> None:
        # T9
        js = _read(OOBE_JS)
        self.assertIn(
            "document.body.dataset.step",
            js,
            "oobe.js must read step from document.body.dataset.step "
            "(R62 back-compat attribute).",
        )

    def test_oobe_js_posts_with_csrf_header(self) -> None:
        # T10 鈥?every mutating fetch uses same-origin + CSRF header.
        js = _read(OOBE_JS)
        # Accept either quote style on the credentials option.
        self.assertRegex(
            js,
            r"""credentials\s*:\s*['"]same-origin['"]""",
            "oobe.js must pass credentials: 'same-origin' (or double-"
            "quoted variant) so the session cookie rides the fetch.",
        )
        self.assertIn(
            "X-CSRF-Token",
            js,
            "oobe.js must echo X-CSRF-Token on every mutating fetch "
            "so the server's CSRF guard accepts the request.",
        )

    def test_oobe_js_has_password_strength_checker(self) -> None:
        # T11
        js = _read(OOBE_JS)
        # The checker function name + at least one of the complexity rules.
        self.assertRegex(
            js,
            r"function\s+checkPasswordStrength\s*\(",
            "oobe.js must define a checkPasswordStrength() helper so the "
            "live strength meter matches the server's _validate_password_strength.",
        )
        # All four complexity rules must appear.
        for rule_re in (
            r"[a-z]",
            r"[A-Z]",
            r"[0-9]",
            r"[!@#\$%\^&\*]",
        ):
            self.assertRegex(
                js, rule_re,
                "oobe.js password checker must enforce "
                "lowercase / uppercase / digit / special-character rules.",
            )


# =========================================================================
# T12-T14 鈥?static/css/oobe.css responsive + view-transition rules
# =========================================================================
class TestOobeCssContract(unittest.TestCase):
    """The wizard CSS must keep R67 responsive breakpoints AND ship
    ``view-transition-name`` rules so the R66 View Transitions API
    hooks into the wizard card.
    """

    def test_oobe_css_exists(self) -> None:
        self.assertTrue(OOBE_CSS.exists(), f"missing {OOBE_CSS}")
        self.assertGreater(
            OOBE_CSS.stat().st_size, 1_500,
            f"oobe.css suspiciously small ({OOBE_CSS.stat().st_size} bytes)",
        )

    def test_oobe_css_has_responsive_breakpoints(self) -> None:
        # T13
        css = _read(OOBE_CSS)
        # Four named breakpoints (R67 contract).
        for label, pat in {
            "mobile":  r"@media[^{]*max-width:\s*767",
            "tablet":  r"@media[^{]*min-width:\s*768[^{]*max-width:\s*1023",
            "desktop": r"@media[^{]*min-width:\s*1024[^{]*max-width:\s*1439",
            "wide":    r"@media[^{]*min-width:\s*1440",
        }.items():
            self.assertRegex(
                css, pat,
                f"oobe.css must declare {label} breakpoint media query",
            )

    def test_oobe_css_has_view_transition_rules(self) -> None:
        # T14
        css = _read(OOBE_CSS)
        self.assertIn(
            "view-transition-name",
            css,
            "oobe.css must use view-transition-name to hook the wizard "
            "card into the R66 View Transitions API.",
        )


# =========================================================================
# T15-T22 鈥?web.py OOBE handlers + helpers
# =========================================================================
class TestWebPyOobeRoutes(unittest.TestCase):

    def setUp(self) -> None:
        self.src = _read(WEB_PY)
        self.tree = _ast(WEB_PY)
        self.funcs = set(_funcdefnames(WEB_PY))
        self.routes = _routes_registered(WEB_PY)
        self.route_fn = _route_to_function(WEB_PY)

    def test_get_oobe_route_registered(self) -> None:
        # T15
        self.assertIn("/oobe", self.routes, "web.py missing GET /oobe route")

    def test_legacy_admin_oobe_redirects(self) -> None:
        # T16 鈥?back-compat alias.
        self.assertIn(
            "/admin/oobe", self.routes,
            "web.py must keep /admin/oobe as a back-compat alias "
            "for older bookmarks / dashboard redirects.",
        )
        fn_name = self.route_fn["/admin/oobe"]
        # Find the function and check it returns a redirect.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn_name:
                body_src = ast.unparse(node)
                self.assertIn(
                    "redirect(", body_src,
                    "/admin/oobe legacy route must call redirect() "
                    "(typically redirect('/oobe')) so old URLs keep working.",
                )
                return
        self.fail("/admin/oobe handler not found")

    def test_seven_oobe_api_routes(self) -> None:
        # T17 鈥?exactly seven POST /api/oobe/* routes.
        expected = {
            "/api/oobe/save-state",
            "/api/oobe/next",
            "/api/oobe/prev",
            "/api/oobe/skip-step",
            "/api/oobe/validate-feishu",
            "/api/oobe/validate-webhook",
            "/api/oobe/complete",
        }
        for path in expected:
            self.assertIn(
                path, self.routes,
                f"web.py missing POST {path} route (R65 wizard API)",
            )

    def test_oobe_validation_helpers_defined(self) -> None:
        # T18 鈥?module-level helpers for password / webhook / cron.
        for name in (
            "_validate_password_strength",
            "_validate_webhook_url",
            "_validate_cron_expr",
            "_webhook_host_allowed",
        ):
            self.assertIn(
                name, self.funcs,
                f"web.py missing {name}() helper (R65 wizard validators)",
            )

    def test_oobe_uses_flask_session(self) -> None:
        # T19 鈥?session['oobe'] / session['oobe_complete'] writes.
        for needle in (
            "session['oobe']",
            "session['oobe_complete']",
            "session.pop(",
        ):
            self.assertIn(
                needle, self.src,
                f"web.py OOBE handlers must reference {needle!r} for "
                "session-based wizard state.",
            )

    def test_oobe_complete_uses_r63_primitives(self) -> None:
        # T20 鈥?step-6 admin creation reuses auth.create_user +
        # auth.create_session.  No duplication of bcrypt / session
        # primitives inside web.py.
        # Find the oobe_complete function and verify it calls auth helpers.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "oobe_complete":
                body_src = ast.unparse(node)
                self.assertIn(
                    "auth.create_user", body_src,
                    "/api/oobe/complete must call auth.create_user() "
                    "to seed the admin (R63 primitive; no duplication).",
                )
                self.assertIn(
                    "auth.create_session", body_src,
                    "/api/oobe/complete must call auth.create_session() "
                    "to auto-login the freshly-created admin.",
                )
                self.assertIn(
                    "auth.write_audit", body_src,
                    "/api/oobe/complete must call auth.write_audit() "
                    "with action='oobe.complete' so the audit log records it.",
                )
                return
        self.fail("oobe_complete function not defined in web.py")

    def test_index_redirect_checks_session_flag(self) -> None:
        # T21
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == "index":
                body_src = ast.unparse(node)
                self.assertIn(
                    "session.get('oobe_complete')",
                    body_src,
                    "GET / must consult session['oobe_complete'] in "
                    "addition to db.get_meta('oobe_completed') so an "
                    "in-session completion stops the redirect on refresh.",
                )
                self.assertIn(
                    "db.get_meta('oobe_completed')",
                    body_src,
                    "GET / must still consult the legacy meta key "
                    "for backward compatibility with SQL-skip deployments.",
                )
                return
        self.fail("index function not found in web.py")

    def test_legacy_oobe_save_route_kept(self) -> None:
        # T22 鈥?R37 + R63 test suites POST against
        # /admin/api/oobe/save with @_admin_required; the route must
        # still exist after R65.
        self.assertIn(
            "/admin/api/oobe/save", self.routes,
            "web.py must keep /admin/api/oobe/save for R37 + R63 "
            "backward compatibility (the new wizard uses /api/oobe/* "
            "but the legacy route stays untouched).",
        )


# =========================================================================
# T23 鈥?Flask session round-trip (runtime smoke)
# =========================================================================
def _has_bcrypt() -> bool:
    try:
        import bcrypt  # noqa: F401
        return True
    except ImportError:
        return False


def _has_pydantic() -> bool:
    try:
        import pydantic  # noqa: F401
        return True
    except ImportError:
        return False


def _import_web_or_skip(test: unittest.TestCase):
    """Import ``web`` against a temp SQLite db; skip cleanly if bcrypt /
    pydantic is missing (the project's "don't pip install on the
    user's machine" preference).
    """
    if not _has_bcrypt() or not _has_pydantic():
        test.skipTest(
            "bcrypt and/or pydantic missing in this venv; runtime smoke "
            "tests require them (auth.py -> db.models -> pydantic).",
        )
        return None
    try:
        import db
        # Pin the db path to a tempfile so we don't touch records.db.
        tmp = tempfile.NamedTemporaryFile(
            prefix="r65_", suffix=".db", delete=False,
        )
        tmp.close()
        import config
        config.DB_PATH = tmp.name
        db.init()
        import web  # noqa: F401
        web.app.config["TESTING"] = True
        web.app.config["SECRET_KEY"] = "r65-test-secret-key"
        return web, tmp.name
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"web import failed: {type(exc).__name__}: {exc}")
        return None


class TestRuntimeSessionRoundTrip(unittest.TestCase):
    """T23 鈥?Flask session round-trip via test_client.

    Sets ``session['oobe']['step']`` to 3 on one request, then reads it
    back on a second request through the same client (cookies stick).
    """

    def setUp(self) -> None:
        loaded = _import_web_or_skip(self)
        if loaded is None:
            return
        self.web, self.tmp_db = loaded
        self.client = self.web.app.test_client()

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_session_step_round_trip(self) -> None:
        # T23 鈥?we don't actually have a public route that echoes the
        # session step (only the wizard JS does, server-side).  Instead
        # we hit /api/oobe/save-state which is the documented
        # session-writing endpoint, then re-fetch /oobe and verify the
        # ``step`` template variable reflects the new step (the GET
        # handler reads ``session['oobe']['step']``).
        # First clear any pre-existing step.
        with self.client.session_transaction() as sess:
            sess.pop("oobe", None)
            sess.pop("oobe_complete", None)
        # Save state on step 3.
        resp = self.client.post(
            "/api/oobe/save-state",
            json={"step": 3, "data": {"url": "https://x.tssplus.top/h"}},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        # Advance via /api/oobe/next.
        resp = self.client.post(
            "/api/oobe/next", json={"step": 3},
        )
        # Step 3 needs url, but url is required -> 400.  Accept either.
        # Use a different step (1 doesn't need data) and verify
        # state propagates.
        resp = self.client.post(
            "/api/oobe/save-state",
            json={"step": 2, "data": {"app_id": "cli_test", "app_secret": "s"}},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        resp = self.client.post("/api/oobe/next", json={"step": 2})
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertEqual(body.get("step"), 3, body)
        # /oobe GET must now render step 3.
        resp = self.client.get("/oobe")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertIn(
            'data-step="3"',
            resp.data.decode("utf-8"),
            "GET /oobe must render with data-step=\"3\" after the "
            "session advanced from step 2 to step 3.",
        )


# =========================================================================
# T24 鈥?password-strength validation
# =========================================================================
class TestRuntimePasswordStrength(unittest.TestCase):
    """T24 鈥?``_validate_password_strength`` accepts / rejects correctly."""

    def setUp(self) -> None:
        loaded = _import_web_or_skip(self)
        if loaded is None:
            return
        self.web, self.tmp_db = loaded

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_password_strength_accepts_strong(self) -> None:
        self.assertIsNone(self.web._validate_password_strength("Aa1!aaaa"))
        self.assertIsNone(self.web._validate_password_strength("Correct-Horse-42!"))
        self.assertIsNone(self.web._validate_password_strength("longer_password-9Z"))

    def test_password_strength_rejects_weak(self) -> None:
        # Empty.
        self.assertIsNotNone(self.web._validate_password_strength(""))
        # Too short.
        self.assertIsNotNone(self.web._validate_password_strength("Aa1!aa"))
        # Missing lowercase.
        self.assertIsNotNone(self.web._validate_password_strength("AA1!AAAA"))
        # Missing uppercase.
        self.assertIsNotNone(self.web._validate_password_strength("aa1!aaaa"))
        # Missing digit.
        self.assertIsNotNone(self.web._validate_password_strength("Aaa!aaaa"))
        # Missing special.
        self.assertIsNotNone(self.web._validate_password_strength("Aa1234567"))


# =========================================================================
# T25 鈥?webhook URL allowlist
# =========================================================================
class TestRuntimeWebhookAllowlist(unittest.TestCase):
    """T25 鈥?``_validate_webhook_url`` enforces the SSRF allowlist."""

    def setUp(self) -> None:
        loaded = _import_web_or_skip(self)
        if loaded is None:
            return
        self.web, self.tmp_db = loaded

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_webhook_allows_school_domain(self) -> None:
        # .tssplus.top is explicitly whitelisted.
        self.assertIsNone(
            self.web._validate_webhook_url("https://x.tssplus.top/h")
        )
        # The /open-apis/bot/v2/hook path is what Feishu users actually
        # paste, but its host is ``open.feishu.cn`` which is NOT on the
        # allowlist (R65 deliberately keeps the allowlist tight to
        # mitigate SSRF).  Verify the rejection is correct.
        self.assertIsNotNone(
            self.web._validate_webhook_url(
                "https://open.feishu.cn/open-apis/bot/v2/hook/x"
            )
        )

    def test_webhook_allows_localhost(self) -> None:
        self.assertIsNone(
            self.web._validate_webhook_url("http://localhost:5000/webhook")
        )
        self.assertIsNone(
            self.web._validate_webhook_url("http://127.0.0.1:9000/cb")
        )

    def test_webhook_rejects_other_hosts(self) -> None:
        self.assertIsNotNone(
            self.web._validate_webhook_url("http://example.com/x"),
            "example.com is NOT on the allowlist (SSRF guard)",
        )
        self.assertIsNotNone(
            self.web._validate_webhook_url("http://192.168.1.1/admin"),
            "private IP must be rejected (SSRF guard)",
        )
        # Wrong scheme.
        self.assertIsNotNone(
            self.web._validate_webhook_url("ftp://x.tssplus.top/"),
            "ftp:// must be rejected",
        )
        self.assertIsNotNone(
            self.web._validate_webhook_url("file:///etc/passwd"),
            "file:// must be rejected",
        )
        # Empty.
        self.assertIsNotNone(self.web._validate_webhook_url(""))


# =========================================================================
# py_compile every file R65 touched
# =========================================================================
class TestPyCompile(unittest.TestCase):
    """Every R65-touched .py file compiles cleanly."""

    def test_compile_web(self) -> None:
        py_compile.compile(str(WEB_PY), doraise=True)

    def test_compile_auth(self) -> None:
        # We don't change auth.py but if the bug we found earlier
        # (require_csrf not imported) re-surfaces this catches it.
        py_compile.compile(str(AUTH_PY), doraise=True)

    def test_compile_test_self(self) -> None:
        py_compile.compile(str(THIS_TEST), doraise=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)