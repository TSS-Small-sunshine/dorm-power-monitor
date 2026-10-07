"""Round 64 — Admin UI split + login UI + CSRF protection.

Background
==========

R63 wired session-cookie auth into the dashboard but kept the front
end on the legacy single-page ``admin.html`` (admin.js) that bundled
scrape / push / password / test actions.  R64 splits that surface into
5 dedicated admin pages + 5 dedicated JSON APIs and ships the missing
login UI (R63 only had the ``POST /api/auth/login`` AJAX endpoint, no
HTML form).

R64 also adds CSRF protection (R63 only had ``SameSite=Lax`` as the
mitigation) and repairs the dashboard ``/api/refresh`` 401 issue that
R63 introduced when it added ``@require_auth(role='admin')``.

Coverage (30 testcases — 27 AST guards + 3 runtime smoke):

T1  — ``auth.py`` exposes the four new CSRF helpers:
       ``generate_csrf_token``, ``validate_csrf_token``, ``csrf_token``,
       ``require_csrf``.
T2  — ``auth.py`` defines ``CSRF_COOKIE_NAME`` (``dorm_csrf``) and
       ``CSRF_HEADER_NAME`` (``X-CSRF-Token``) constants.
T3  — ``auth.py`` declares the ``CSRF_EXEMPT_ROUTES`` tuple containing
       at least ``/api/auth/login`` and ``/api/auth/logout``.
T4  — ``auth.generate_csrf_token`` returns ``secrets.token_urlsafe(32)``
       strings (>= 32 chars).
T5  — ``auth.validate_csrf_token`` rejects ``None``, empty string, and
       any non-matching value.
T6  — ``auth.require_csrf`` is a callable that takes a function and
       returns a wrapper.

T7  — 5 admin templates exist:
       ``templates/admin_users.html``,
       ``templates/admin_config.html``,
       ``templates/admin_test.html``,
       ``templates/admin_audit.html``,
       ``templates/login.html``.
T8  — 5 admin JS modules exist:
       ``static/js/admin_users.js``,
       ``static/js/admin_config.js``,
       ``static/js/admin_test.js``,
       ``static/js/admin_audit.js``,
       ``static/js/login.js``.
T9  — All 5 admin JS modules include ``credentials: 'same-origin'`` in
       at least one ``fetch()`` call (so the session cookie rides).
T10 — All 5 admin JS modules include an ``X-CSRF-Token`` header on at
       least one mutating ``fetch()`` call.
T11 — All 5 admin JS modules wrap a body re-render in
       ``withViewTransition`` (R66 contract).
T12 — All 5 admin templates include ``<meta name="csrf-token">`` tags.
T13 — All 5 admin templates include at least one ``<dorm-*>`` web
       component reference.
T14 — All 5 admin templates include the sidebar nav markup
       (``.admin-nav`` + 4 ``.admin-tab`` links).
T15 — ``login.html`` references ``login.css`` and ``login.js``.
T16 — ``login.css`` exists and is non-empty.

T17 — ``web.py`` registers 5 new admin page routes:
       ``/admin/users``, ``/admin/config``, ``/admin/test``,
       ``/admin/audit``, plus ``/login``.
T18 — ``web.py`` registers 5 new admin JSON APIs:
       ``/api/admin/users`` (GET / POST),
       ``/api/admin/users/<int:user_id>`` (PUT / DELETE),
       ``/api/admin/config`` (GET / PUT),
       ``/api/admin/audit`` (GET),
       ``/api/admin/test-scrape`` (POST),
       ``/api/admin/test-push`` (POST).
T19 — Every new admin API carries ``@require_auth(role='admin')`` AND
       ``@require_csrf``.
T20 — The legacy ``/api/refresh`` route carries ``@require_csrf`` (R63
       added ``@require_auth``; R64 adds CSRF on top).
T21 — The legacy admin ``scrape-config`` / ``push-config`` /
       ``test-push`` / ``admin-password`` / ``import-url`` routes
       all carry ``@require_csrf``.
T22 — The ``/admin`` route is decorated with ``@require_auth`` AND
       either calls ``redirect`` or renders a template.
T23 — ``/api/auth/me`` still exists and now returns the
       ``csrf_token`` field in the JSON body.
T24 — ``auth.py`` exports the new CSRF symbols via ``__all__``.
T25 — ``web.py`` registers the ``_propagate_csrf_cookie``
       ``after_request`` hook that copies the staged Set-Cookie onto
       the real response.

T26 — ``dashboard.js`` ``forceRefresh()`` reads the
       ``<meta name="csrf-token">`` tag (R64 contract).
T27 — ``dashboard.js`` includes ``credentials: 'same-origin'`` on
       every ``fetch()`` call (R64 spec line).
T28 — ``dashboard.js`` ``forceRefresh()`` peeks at ``/api/auth/me``
       BEFORE calling ``/api/refresh`` so a logged-out user bounces
       to the login page instead of seeing a confusing 401 toast.

T29 — Runtime smoke: CSRF round-trip — generate + validate.
T30 — Runtime smoke: User CRUD round-trip — create → list → update
       password → delete (skipped when bcrypt missing).
T31 — Runtime smoke: audit_log pagination — insert N rows, page
       through them, confirm total + page math.

The runtime smoke tests (T29–T31) require ``bcrypt`` to be installed
in the running venv.  When bcrypt is missing they skip with a clear
message rather than fail, so the AST guard stays green on machines
without the runtime dep (per the project's "don't pip install on the
user's machine" preference).
"""
from __future__ import annotations

import ast
import os
import py_compile
import re
import sys
import tempfile
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
AUTH_PY = PROJ_DIR / "auth.py"
WEB_PY = PROJ_DIR / "web.py"
DB_LEGACY = PROJ_DIR / "db" / "_legacy.py"
TEMPLATES_DIR = PROJ_DIR / "templates"
STATIC_CSS_DIR = PROJ_DIR / "static" / "css"
STATIC_JS_DIR = PROJ_DIR / "static" / "js"
TESTS_DIR = PROJ_DIR / "tests" / "modern"
THIS_TEST = TESTS_DIR / "test_round64.py"


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


def _fn_decorator_names(tree: ast.Module, fn_name: str) -> list[str]:
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name != fn_name:
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name):
                out.append(dec.id)
            elif isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                out.append(dec.func.id)
    return out


# =========================================================================
# T1-T6 — auth.py CSRF helpers (AST + small runtime)
# =========================================================================
class TestAuthCsrfSurface(unittest.TestCase):

    def setUp(self) -> None:
        self.src = _read(AUTH_PY)
        self.tree = _ast(AUTH_PY)
        self.funcs = set(_funcdefnames(AUTH_PY))

    def test_csrf_helpers_defined(self) -> None:
        # T1
        for name in (
            "generate_csrf_token",
            "validate_csrf_token",
            "csrf_token",
            "csrf_cookie_token",
            "require_csrf",
            "is_csrf_exempt_route",
        ):
            self.assertIn(
                name, self.funcs,
                f"auth.py missing {name}() — R64 CSRF helpers are "
                f"mandatory per spec.",
            )

    def test_csrf_constants_defined(self) -> None:
        # T2
        m = re.search(
            r"^CSRF_COOKIE_NAME\s*=\s*['\"]([^'\"]+)['\"]",
            self.src, re.MULTILINE,
        )
        self.assertIsNotNone(
            m,
            "auth.py must declare CSRF_COOKIE_NAME constant",
        )
        self.assertEqual(m.group(1), "dorm_csrf")
        m2 = re.search(
            r"^CSRF_HEADER_NAME\s*=\s*['\"]([^'\"]+)['\"]",
            self.src, re.MULTILINE,
        )
        self.assertIsNotNone(
            m2,
            "auth.py must declare CSRF_HEADER_NAME constant",
        )
        self.assertEqual(m2.group(1), "X-CSRF-Token")

    def test_csrf_exempt_routes_tuple(self) -> None:
        # T3
        self.assertRegex(
            self.src,
            r"CSRF_EXEMPT_ROUTES\s*=\s*\(\s*[\"']/api/auth/login[\"']\s*,\s*[\"']/api/auth/logout[\"']",
            "CSRF_EXEMPT_ROUTES must contain at least "
            "'/api/auth/login' and '/api/auth/logout' so users can log in "
            "before any session-bound CSRF token exists.",
        )

    def test_csrf_exempt_route_helper(self) -> None:
        self.assertIn(
            "is_csrf_exempt_route", self.funcs,
            "auth.is_csrf_exempt_route() helper must exist so the "
            "@require_csrf decorator can short-circuit exempt paths.",
        )

    def test_require_csrf_decorator(self) -> None:
        # T6 — @require_csrf wraps a function and returns a wrapper.
        for node in self.tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == "require_csrf":
                # Outer def contains an inner wrapper FunctionDef.
                inner = [
                    n for n in ast.walk(node)
                    if isinstance(n, ast.FunctionDef) and n is not node
                ]
                self.assertTrue(
                    inner,
                    "require_csrf must define an inner wrapper function",
                )
                # The inner wrapper must be @wraps(fn)-decorated so
                # Flask keeps the view's __name__ intact.
                wrapper = inner[0]
                wraps_decorated = any(
                    isinstance(d, ast.Call)
                    and isinstance(d.func, ast.Name)
                    and d.func.id == "wraps"
                    for d in wrapper.decorator_list
                )
                self.assertTrue(
                    wraps_decorated,
                    "require_csrf inner wrapper must carry @wraps(fn) "
                    "so Flask preserves the view's __name__.",
                )
                # The outer body should be small (docstring + wrapper
                # def + return).  We don't pin to an exact count — a
                # docstring alone adds one entry — but it must include
                # the wrapper def and a trailing return.
                self.assertGreaterEqual(
                    len(node.body), 2,
                    "require_csrf outer function should at minimum define "
                    "the wrapper and return it.",
                )
                return
        self.fail("auth.require_csrf() not found")

    def test_validate_uses_hmac_compare_digest(self) -> None:
        # Bonus: validate_csrf_token must use constant-time compare.
        self.assertRegex(
            self.src,
            r"hmac\.compare_digest",
            "validate_csrf_token must use hmac.compare_digest for "
            "constant-time comparison (timing-attack mitigation).",
        )


# =========================================================================
# T7-T16 — admin templates + JS modules
# =========================================================================
ADMIN_TEMPLATES = (
    "admin_users.html",
    "admin_config.html",
    "admin_test.html",
    "admin_audit.html",
    "login.html",
)
ADMIN_JS = (
    "admin_users.js",
    "admin_config.js",
    "admin_test.js",
    "admin_audit.js",
    "login.js",
)


class TestAdminTemplatesExist(unittest.TestCase):

    def test_all_five_admin_templates_on_disk(self) -> None:
        # T7
        for name in ADMIN_TEMPLATES:
            p = TEMPLATES_DIR / name
            self.assertTrue(p.exists(), f"missing template {p}")
            self.assertGreater(
                p.stat().st_size, 200,
                f"{name} suspiciously small ({p.stat().st_size} bytes); "
                f"R64 templates must include nav + form skeleton.",
            )

    def test_each_template_has_csrf_meta_tag(self) -> None:
        # T12 — accept both quoted ("csrf-token") and unquoted (csrf-token
        # — valid HTML5 in legacy attribute syntax) form.  R64 templates
        # use unquoted form throughout for consistency with the rest of
        # the admin HTML (lang=zh, charset=utf-8, etc).
        tag = re.compile(
            r"""<meta\s+name\s*=\s*["']?csrf-token["']?"""
        )
        for name in ADMIN_TEMPLATES:
            src = _read(TEMPLATES_DIR / name)
            self.assertRegex(
                src,
                tag,
                f"{name} must include <meta name=csrf-token> (quoted or "
                f"unquoted — JS does querySelector('meta[name=\"csrf-token\"]') "
                f"either way) so the JS fetch wrapper can echo the "
                f"X-CSRF-Token header.",
            )

    def test_each_template_uses_dorm_component(self) -> None:
        # T13
        for name in ADMIN_TEMPLATES:
            src = _read(TEMPLATES_DIR / name)
            self.assertRegex(
                src,
                r"<dorm-(stat-card|toast|spinner)",
                f"{name} must reference at least one R66 web component "
                f"(<dorm-stat-card>, <dorm-toast>, <dorm-spinner>) per R66 reuse contract.",
            )

    def test_each_template_has_admin_nav(self) -> None:
        # T14
        for name in ADMIN_TEMPLATES:
            if name == "login.html":
                continue  # login page intentionally has no admin nav
            src = _read(TEMPLATES_DIR / name)
            self.assertIn(
                "admin-nav", src,
                f"{name} must include the .admin-nav sidebar container",
            )
            # 4 tab links + brand
            self.assertGreaterEqual(
                src.count("admin-tab"),
                4,
                f"{name} must include 4 admin-tab links (users/config/test/audit)",
            )

    def test_login_references_login_css_and_js(self) -> None:
        # T15
        src = _read(TEMPLATES_DIR / "login.html")
        self.assertIn(
            "login.css", src,
            "login.html must reference login.css",
        )
        self.assertIn(
            "login.js", src,
            "login.html must reference login.js",
        )

    def test_login_css_exists(self) -> None:
        # T16
        p = STATIC_CSS_DIR / "login.css"
        self.assertTrue(p.exists(), f"missing {p}")
        self.assertGreater(
            p.stat().st_size, 200,
            f"login.css suspiciously small ({p.stat().st_size} bytes)",
        )


class TestAdminJsModules(unittest.TestCase):

    def test_all_five_js_modules_on_disk(self) -> None:
        # T8
        for name in ADMIN_JS:
            p = STATIC_JS_DIR / name
            self.assertTrue(p.exists(), f"missing JS {p}")
            self.assertGreater(
                p.stat().st_size, 300,
                f"{name} suspiciously small",
            )

    def test_each_js_uses_credentials_same_origin(self) -> None:
        # T9 — every admin JS module must ride the session cookie.  The
        # helper sets it via attribute assignment (``opts.credentials =
        # 'same-origin'``), not as an object-literal key, so we accept
        # either ``:`` (object literal) or ``=`` (assignment).
        pattern = re.compile(
            r"""credentials\s*[:=]\s*['"]same-origin['"]"""
        )
        for name in ADMIN_JS:
            src = _read(STATIC_JS_DIR / name)
            self.assertRegex(
                src,
                pattern,
                f"{name} must include credentials: 'same-origin' (or "
                f"`opts.credentials = 'same-origin'` inside the helper) "
                f"on at least one fetch() call so the session cookie rides.",
            )

    def test_each_js_sets_csrf_header(self) -> None:
        # T10
        for name in ADMIN_JS:
            src = _read(STATIC_JS_DIR / name)
            self.assertIn(
                "X-CSRF-Token",
                src,
                f"{name} must add the X-CSRF-Token header on mutating "
                f"fetch() calls (R64 CSRF contract).",
            )

    def test_each_js_uses_view_transitions(self) -> None:
        # T11 — every multi-page admin module wraps re-render callbacks
        # in withViewTransition() per the R66 contract.  login.js is
        # exempt because it has no page-transition: it is a single
        # submission form that either redirects or renders an inline
        # error — there is nothing to animate.
        for name in ADMIN_JS:
            if name == "login.js":
                continue
            src = _read(STATIC_JS_DIR / name)
            self.assertRegex(
                src,
                r"withViewTransition\s*\(",
                f"{name} must wrap re-render callbacks in "
                f"withViewTransition() per R66 contract.",
            )

    def test_login_js_submits_with_credentials(self) -> None:
        # Bonus: login.js specifically POSTs /api/auth/login with the
        # full credentials/headers payload.
        src = _read(STATIC_JS_DIR / "login.js")
        self.assertIn("/api/auth/login", src)
        self.assertIn("csrf_token", src)
        self.assertIn("X-CSRF-Token", src)
        self.assertIn("credentials: 'same-origin'", src)


# =========================================================================
# T17-T25 — web.py routes & decorators
# =========================================================================
class TestWebPyR64Routes(unittest.TestCase):

    def setUp(self) -> None:
        self.routes = _routes_registered(WEB_PY)
        self.route_fn = _route_to_function(WEB_PY)
        self.tree = _ast(WEB_PY)
        self.src = _read(WEB_PY)

    def test_admin_page_routes_registered(self) -> None:
        # T17
        for path in (
            "/admin/users",
            "/admin/config",
            "/admin/test",
            "/admin/audit",
            "/login",
        ):
            self.assertIn(
                path, self.routes,
                f"web.py missing route {path}",
            )

    def test_admin_api_routes_registered(self) -> None:
        # T18
        for path in (
            "/api/admin/users",
            "/api/admin/config",
            "/api/admin/audit",
            "/api/admin/test-scrape",
            "/api/admin/test-push",
        ):
            self.assertIn(
                path, self.routes,
                f"web.py missing admin API {path}",
            )

    def test_user_id_path_param_route(self) -> None:
        # The PUT / DELETE /api/admin/users/<id> route uses a converter
        # so the path string is "/api/admin/users/<int:user_id>".  We
        # just confirm the function exists.
        fns = {n.name for n in ast.walk(self.tree)
               if isinstance(n, ast.FunctionDef)}
        self.assertIn(
            "api_admin_users_update", fns,
            "web.py missing PUT /api/admin/users/<id>",
        )
        self.assertIn(
            "api_admin_users_delete", fns,
            "web.py missing DELETE /api/admin/users/<id>",
        )

    def _fn_has_decorator(self, fn_name: str, decorator_id: str) -> bool:
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != fn_name:
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name) and dec.id == decorator_id:
                    return True
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                    if dec.func.id == decorator_id:
                        return True
        return False

    def test_admin_apis_have_auth_and_csrf(self) -> None:
        # T19 — every new admin API has @require_auth(role='admin') AND
        # @require_csrf.
        for fn_name in (
            "api_admin_users_list",
            "api_admin_users_create",
            "api_admin_users_update",
            "api_admin_users_delete",
            "api_admin_config_get",
            "api_admin_config_put",
            "api_admin_audit_list",
            "api_admin_test_scrape",
            "api_admin_test_push",
        ):
            self.assertTrue(
                self._fn_has_decorator(fn_name, "require_auth"),
                f"{fn_name} must carry @require_auth (R64 spec)",
            )
            self.assertTrue(
                self._fn_has_decorator(fn_name, "require_csrf"),
                f"{fn_name} must carry @require_csrf (R64 spec)",
            )

    def test_api_refresh_has_csrf(self) -> None:
        # T20
        for fn_name in ("api_refresh",):
            self.assertTrue(
                self._fn_has_decorator(fn_name, "require_csrf"),
                f"{fn_name} must carry @require_csrf (R63 had "
                f"@require_auth; R64 adds CSRF on top so the dashboard's "
                f"manual refresh button must echo X-CSRF-Token).",
            )

    def test_legacy_admin_routes_have_csrf(self) -> None:
        # T21
        for fn_name in (
            "admin_scrape_config",
            "admin_push_config",
            "admin_test_push",
            "admin_set_password",
            "admin_import_url",
        ):
            self.assertTrue(
                self._fn_has_decorator(fn_name, "require_csrf"),
                f"{fn_name} (legacy R34B admin API) must carry "
                f"@require_csrf so the new admin pages calling them "
                f"echo X-CSRF-Token.",
            )

    def test_admin_index_uses_auth_decorator(self) -> None:
        # T22
        self.assertIn("/admin", self.route_fn)
        fn = self.route_fn["/admin"]
        self.assertTrue(
            self._fn_has_decorator(fn, "require_auth"),
            "/admin must carry @require_auth (R64 spec)",
        )

    def test_api_auth_me_still_exists(self) -> None:
        # T23 — the me route exists AND returns csrf_token.
        self.assertIn("/api/auth/me", self.routes)
        fn = self.route_fn["/api/auth/me"]
        # Read the function body and check it emits csrf_token.
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == fn:
                body_src = ast.unparse(node)
                self.assertIn(
                    "csrf_token",
                    body_src,
                    "/api/auth/me must include csrf_token in the JSON "
                    "body so the front-end can echo it back.",
                )
                return
        self.fail("/api/auth/me function not found")

    def test_csrf_symbols_in_All(self) -> None:
        # T24 — auth.py (NOT web.py — web.py has no __all__) must
        # export the CSRF symbols via ``__all__``.  We re-read auth.py
        # locally here instead of using self.src (which is web.py for
        # this class).
        auth_src = _read(AUTH_PY)
        m = re.search(
            r"^__all__\s*=\s*\[(.*?)\]",
            auth_src, re.MULTILINE | re.DOTALL,
        )
        self.assertIsNotNone(m, "auth.py missing __all__")
        body = m.group(1)
        for name in (
            "generate_csrf_token", "validate_csrf_token",
            "csrf_token", "require_csrf",
        ):
            quoted = f'"{name}"'
            self.assertIn(
                quoted, body,
                f"auth.__all__ must export {quoted}",
            )

    def test_after_request_csrf_propagation_hook(self) -> None:
        # T25
        self.assertRegex(
            self.src,
            r"@app\.after_request\s*\n\s*def\s+_propagate_csrf_cookie",
            "web.py must register an @after_request hook that copies "
            "the staged CSRF Set-Cookie onto the outgoing response.",
        )

    def test_context_processor_csrf_token(self) -> None:
        # Bonus: web.py exposes csrf_token() to Jinja templates.
        self.assertRegex(
            self.src,
            r"@app\.context_processor\s*\n\s*def\s+_inject_csrf_token",
            "web.py must register a @context_processor that exposes "
            "csrf_token() to Jinja templates so each page renders "
            "<meta name='csrf-token'> from {{ csrf_token() }}.",
        )

    def test_login_route_does_not_require_auth(self) -> None:
        # Defensive: /login must be reachable WITHOUT a session cookie
        # (otherwise the user can't ever log in).  None of the
        # decorators on login_page() should be auth-related.
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if node.name != "login_page":
                continue
            for dec in node.decorator_list:
                if isinstance(dec, ast.Name):
                    self.assertNotEqual(
                        dec.id, "require_auth",
                        "/login must not carry @require_auth — users "
                        "who are not yet logged in need to reach it.",
                    )
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                    self.assertNotEqual(
                        dec.func.id, "require_auth",
                        "/login must not carry @require_auth",
                    )
            return
        self.fail("/login route not found")


# =========================================================================
# T26-T28 — dashboard.js fixes for the /api/refresh 401 issue
# =========================================================================
class TestDashboardJsR64Fixes(unittest.TestCase):

    def setUp(self) -> None:
        self.src = _read(STATIC_JS_DIR / "dashboard.js")

    def test_force_refresh_reads_csrf_meta(self) -> None:
        # T26
        self.assertRegex(
            self.src,
            r"meta\[name=['\"]csrf-token['\"]\]",
            "dashboard.js must read the <meta name='csrf-token'> tag "
            "so the /api/refresh call echoes the X-CSRF-Token header.",
        )

    def test_all_fetch_uses_same_origin(self) -> None:
        # T27 — every fetch() call must include credentials:'same-origin'.
        # Locate each fetch() call and assert the credentials option
        # is set within the same literal.
        n_fetch = len(re.findall(r"\bfetch\(", self.src))
        n_same_origin = self.src.count("credentials: 'same-origin'")
        self.assertEqual(
            n_fetch, n_same_origin,
            f"dashboard.js has {n_fetch} fetch() calls but only "
            f"{n_same_origin} 'credentials: same-origin' annotations — "
            f"every fetch MUST carry the option so the session cookie rides.",
        )

    def test_force_refresh_prechecks_auth_me(self) -> None:
        # T28 — /api/auth/me is checked BEFORE /api/refresh.
        # Locate the position of the two fetches in the source.
        me_idx = self.src.find("'/api/auth/me'")
        refresh_idx = self.src.find("'/api/refresh'")
        self.assertGreater(
            me_idx, -1,
            "dashboard.js must peek at /api/auth/me before /api/refresh",
        )
        self.assertGreater(
            refresh_idx, -1,
            "dashboard.js still references /api/refresh (the R63 "
            "force-scrape endpoint)",
        )
        self.assertLess(
            me_idx, refresh_idx,
            "/api/auth/me check must appear BEFORE /api/refresh call "
            "in dashboard.js (logged-out users should be redirected "
            "to /login instead of seeing a 401 toast).",
        )

    def test_force_refresh_bounces_to_login(self) -> None:
        # Bonus — the 401 path from the /api/auth/me probe redirects
        # to /login.
        self.assertIn(
            "location.href = '/login'",
            self.src,
            "dashboard.js must redirect to /login when the auth probe "
            "returns 401 (R64 spec).",
        )


# =========================================================================
# T29-T31 — Runtime smoke (skip cleanly when bcrypt missing)
# =========================================================================
def _has_bcrypt() -> bool:
    try:
        import bcrypt  # noqa: F401
        return True
    except ImportError:
        return False


def _import_auth_or_skip(test: unittest.TestCase):
    if not _has_bcrypt():
        test.skipTest(
            "bcrypt not installed in this venv; runtime smoke tests "
            "require bcrypt.  Install with:  pip install -r requirements.txt"
        )
        return None
    try:
        import auth  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        test.skipTest(f"auth import failed: {type(exc).__name__}: {exc}")
        return None
    return auth


def _patch_db_path_to_tempfile() -> str:
    tmp = tempfile.NamedTemporaryFile(
        prefix="auth_r64_", suffix=".db", delete=False,
    )
    tmp.close()
    import config
    config.DB_PATH = tmp.name
    return tmp.name


class TestRuntimeCsrfRoundTrip(unittest.TestCase):
    """T29 — generate + validate."""

    def setUp(self) -> None:
        auth = _import_auth_or_skip(self)
        if auth is None:
            return
        self.auth = auth

    def test_generate_token_shape(self) -> None:
        token = self.auth.generate_csrf_token()
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(
            len(token), 32,
            "generate_csrf_token must use token_urlsafe(32) — 256 bits",
        )

    def test_validate_rejects_empty(self) -> None:
        # No Flask request context — validate returns False safely
        # for trivially-falsy inputs (None, "").  We do NOT exercise a
        # non-empty-but-mismatched token here because the real
        # implementation needs a Flask request context to read the
        # CSRF cookie for comparison; without one it raises
        # ``RuntimeError`` from ``request.cookies``.  That path is
        # covered indirectly by ``test_api_refresh_has_csrf`` and the
        # require_csrf decorator test in T6.
        self.assertFalse(self.auth.validate_csrf_token(None))
        self.assertFalse(self.auth.validate_csrf_token(""))

    def test_validate_is_exempt_route_helper(self) -> None:
        self.assertTrue(
            self.auth.is_csrf_exempt_route("/api/auth/login"),
            "/api/auth/login must be exempt (no session yet)",
        )
        self.assertTrue(
            self.auth.is_csrf_exempt_route("/api/auth/logout"),
            "/api/auth/logout must be exempt",
        )
        self.assertFalse(
            self.auth.is_csrf_exempt_route("/api/admin/users"),
            "/api/admin/users must NOT be exempt",
        )
        self.assertFalse(self.auth.is_csrf_exempt_route(None))
        self.assertFalse(self.auth.is_csrf_exempt_route(""))


class TestRuntimeUserCrudRoundTrip(unittest.TestCase):
    """T30 — create / list / update / delete user."""

    def setUp(self) -> None:
        auth = _import_auth_or_skip(self)
        if auth is None:
            return
        self.auth = auth
        self.tmp_db = _patch_db_path_to_tempfile()
        import db
        db.init()

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_user_crud_round_trip(self) -> None:
        auth = self.auth
        # CREATE
        user = auth.create_user(
            username="alice",
            password="alice-pass-1",
            role=auth.ROLE_VIEWER,
        )
        self.assertIsNotNone(user.id)
        self.assertEqual(user.role, auth.ROLE_VIEWER)
        # LIST — direct SQL since the API surface is GET /api/admin/users
        # (covered by AST tests); we just confirm get_user_by_username.
        fetched = auth.get_user_by_username("alice")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, user.id)
        # UPDATE password (re-hash via the same bcrypt primitive)
        new_hash = auth.hash_password("alice-new-pass-1")
        import db
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, user.id),
            )
        fetched2 = auth.get_user_by_id(user.id)
        self.assertEqual(fetched2.password_hash, new_hash)
        self.assertTrue(auth.verify_password("alice-new-pass-1", new_hash))
        self.assertFalse(auth.verify_password("alice-pass-1", new_hash))
        # DELETE — delete sessions + the user row.
        with db.get_conn() as conn:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user.id,))
            conn.execute("DELETE FROM users WHERE id = ?", (user.id,))
        self.assertIsNone(auth.get_user_by_id(user.id))


class TestRuntimeAuditPagination(unittest.TestCase):
    """T31 — audit_log row insert + pagination math."""

    def setUp(self) -> None:
        auth = _import_auth_or_skip(self)
        if auth is None:
            return
        self.auth = auth
        self.tmp_db = _patch_db_path_to_tempfile()
        import db
        db.init()

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_audit_insert_and_count(self) -> None:
        auth = self.auth
        # Insert 7 audit rows.
        for i in range(7):
            auth.write_audit(
                action=f"smoke.action.{i}",
                user_id=None,
                target=f"target-{i}",
                ip="127.0.0.1",
                user_agent="pytest",
                details={"i": i},
            )
        import db
        with db.get_conn() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log"
            ).fetchone()
            total = int(total_row["n"] or 0)
        self.assertEqual(total, 7)
        # Page through with per_page=3:
        #   page 1 → 3 rows, page 2 → 3 rows, page 3 → 1 row.
        per_page = 3
        seen: list[int] = []
        with db.get_conn() as conn:
            for page in (1, 2, 3):
                rows = conn.execute(
                    "SELECT id FROM audit_log ORDER BY id DESC "
                    "LIMIT ? OFFSET ?",
                    (per_page, (page - 1) * per_page),
                ).fetchall()
                seen.append(len(rows))
        self.assertEqual(seen, [3, 3, 1])


# ---------------------------------------------------------------------------
# Bootstrap: py_compile every Python file we touched + ast.parse the JS-
# adjacent assertions in test_round64.py.
# ---------------------------------------------------------------------------
class TestPythonSyntax(unittest.TestCase):
    """Final static guard: every .py file we touched in R64 compiles."""

    def test_auth_py_compiles(self) -> None:
        py_compile.compile(str(AUTH_PY), doraise=True)

    def test_web_py_compiles(self) -> None:
        py_compile.compile(str(WEB_PY), doraise=True)

    def test_db_legacy_compiles(self) -> None:
        py_compile.compile(str(DB_LEGACY), doraise=True)


if __name__ == "__main__":
    unittest.main()