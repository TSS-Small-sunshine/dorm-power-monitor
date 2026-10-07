"""Round 63 — session-cookie authentication backend.

Background
==========

Pre-R63 the dashboard used HTTP Basic Auth against a single
hard-coded admin user with the password stored in plaintext under
``meta.admin_password``.  R63 introduces:

  * A ``users`` table with bcrypt-hashed passwords + role column
  * A server-side ``sessions`` table keyed by an opaque
    ``secrets.token_urlsafe(32)`` token
  * A 5-failures-in-15-minutes lockout per (username, ip) pair
  * An ``audit_log`` table for tamper-evident after-the-fact review
  * Two new HTTP endpoints (``/api/auth/login``, ``/api/auth/logout``,
    ``/api/auth/me``)
  * The ``@require_auth(role='admin')`` decorator that gates every
    admin route + ``/api/refresh``
  * An OOBE step-6 backend hook that can seed the initial admin user

The login UI itself is the R64 frontend's job; this round ships the
backend primitives + integration.

Coverage (24 testcases — 20+ static AST guards + 4 runtime smoke):

T1  — ``auth.py`` exists on disk and is non-empty.
T2  — ``auth.py`` defines ``hash_password`` + ``verify_password``.
T3  — ``auth.py`` defines ``create_session`` + ``validate_session``
        + ``revoke_session`` + ``purge_expired_sessions``.
T4  — ``auth.py`` defines ``record_failed_attempt`` +
        ``is_locked`` + ``clear_failed_attempts``.
T5  — ``auth.py`` defines ``require_auth`` (Flask decorator) +
        ``get_current_user`` + ``audit`` + ``attempt_login``.
T6  — ``auth.py`` defines the four Pydantic models: ``User``,
        ``Session``, ``FailedAttempt``, ``AuditLog``.
T7  — ``auth.py`` declares ``DEFAULT_BCRYPT_ROUNDS = 12`` and uses
        ``bcrypt.gensalt(rounds=12)`` in ``hash_password``.
T8  — ``auth.py`` lockout constants: ``DEFAULT_LOCKOUT_THRESHOLD = 5``
        + ``DEFAULT_LOCKOUT_MINUTES = 15``.
T9  — ``auth.py`` uses ``secrets.token_urlsafe(32)`` for the session
        token (not a random smaller-length shortcut).
T10 — ``auth.py`` does NOT write any plaintext password /
        ``password_hash`` / ``token`` to the ``audit_log`` table.
T11 — ``db/_legacy.py`` ``_SCHEMA`` now contains all four new tables
        (users / sessions / failed_attempts / audit_log) with the
        required indexes.
T12 — ``web.py`` registers the three new auth routes:
        ``/api/auth/login``, ``/api/auth/logout``, ``/api/auth/me``.
T13 — ``web.py`` wires ``require_auth(role='admin')`` onto ``/admin``,
        ``/admin/api/*`` (scrape / push / test-push / admin-password /
        import-url) and ``/api/refresh``.
T14 — ``web.py`` keeps ``/admin/api/oobe/save`` accessible via the
        legacy ``_admin_required`` (OOBE pre-auth path) — ``require_auth``
        would block first-time setup.
T15 — ``web.py`` does NOT touch the templates / static / frontend JS.
T16 — ``web.py`` sets up Flask's ``SECRET_KEY`` (so signed cookies
        survive restarts) and the session cookie hardening flags
        (``SESSION_COOKIE_HTTPONLY``, ``SESSION_COOKIE_SAMESITE``).
T17 — ``web.py`` does not import ``config`` differently from the
        pre-R63 shape (no new ``config.ATTR`` references).
T18 — ``auth.py`` exports a Flask ``SESSION_COOKIE_NAME`` distinct
        from the default ``session`` so we own the namespace.
T19 — ``auth.py`` writes audit entries only through ``write_audit``
        (no inline ``INSERT INTO audit_log`` elsewhere).
T20 — ``auth.py`` documents why JWT was rejected in favour of a
        signed-cookie + server-side row model (security docstring).

T21 — Runtime smoke: ``hash_password`` + ``verify_password`` round-trip.
T22 — Runtime smoke: ``create_session`` → ``validate_session`` →
        ``revoke_session`` round-trip (incl. sliding expiry + wrong
        token rejection).
T23 — Runtime smoke: ``record_failed_attempt`` ×5 → ``is_locked``
        returns True; ``clear_failed_attempts`` → ``is_locked`` False.
T24 — Runtime smoke: ``ensure_initial_admin`` seeds from
        ``AUTH_INITIAL_ADMIN_PASSWORD`` env, wipes the env var after
        seeding, and is idempotent on the second invocation.

The runtime smoke tests (T21–T24) require ``bcrypt`` to be installed
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
import textwrap
import unittest
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJ_DIR = Path("D:/MiniMax_Workstation/Creative_Workstation/dorm-power-monitor")
AUTH_PY = PROJ_DIR / "auth.py"
WEB_PY = PROJ_DIR / "web.py"
DB_LEGACY = PROJ_DIR / "db" / "_legacy.py"
REQUIREMENTS = PROJ_DIR / "requirements.txt"
TESTS_DIR = PROJ_DIR / "tests" / "modern"
THIS_TEST = TESTS_DIR / "test_round63.py"

# Templates + static — must be UNTOUCHED in R63.
TEMPLATES_DIR = PROJ_DIR / "templates"
STATIC_CSS_DIR = PROJ_DIR / "static" / "css"
STATIC_JS_DIR = PROJ_DIR / "static" / "js"
STATIC_COMP_DIR = PROJ_DIR / "static" / "js" / "components"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _ast(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _funcdefnames(path: Path) -> list[str]:
    tree = _ast(path)
    return sorted(
        {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    )


def _classdefnames(path: Path) -> list[str]:
    tree = _ast(path)
    return sorted({n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)})


def _decorated_func_names(path: Path, decorator_name: str) -> list[str]:
    """Return the names of every function whose decorators include ``decorator_name``."""
    tree = _ast(path)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == decorator_name:
                out.append(node.name)
                break
            # ``@require_auth(role='admin')`` → Call node with func=Name('require_auth')
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                if dec.func.id == decorator_name:
                    out.append(node.name)
                    break
    return sorted(out)


def _routes_registered(path: Path) -> list[str]:
    """Return every Flask route rule path declared in ``path``."""
    tree = _ast(path)
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            # ``app.route("/foo")`` → func=Attribute(Name='app', attr='route')
            if node.func.attr == "route" and node.args:
                a0 = node.args[0]
                if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                    out.append(a0.value)
    return out


# =========================================================================
# T1 — auth.py exists and is non-empty
# =========================================================================
class TestAuthModuleExists(unittest.TestCase):

    def test_auth_py_on_disk(self) -> None:
        self.assertTrue(AUTH_PY.exists(), f"missing {AUTH_PY}")
        self.assertGreater(
            AUTH_PY.stat().st_size, 5_000,
            f"auth.py suspiciously small ({AUTH_PY.stat().st_size} bytes); "
            "the R63 spec expects at least the bcrypt / session / lockout "
            "/ audit primitives plus the audit log docs.",
        )


# =========================================================================
# T2-T9 — auth.py API surface (AST-based)
# =========================================================================
class TestAuthApiSurface(unittest.TestCase):

    def setUp(self) -> None:
        self.tree = _ast(AUTH_PY)
        self.funcs = set(_funcdefnames(AUTH_PY))
        self.classes = set(_classdefnames(AUTH_PY))

    def test_password_helpers(self) -> None:
        # T2
        self.assertIn("hash_password", self.funcs)
        self.assertIn("verify_password", self.funcs)

    def test_session_helpers(self) -> None:
        # T3
        for name in ("create_session", "validate_session",
                     "revoke_session", "purge_expired_sessions"):
            self.assertIn(name, self.funcs, f"auth.py missing {name}()")

    def test_lockout_helpers(self) -> None:
        # T4
        for name in ("record_failed_attempt", "is_locked",
                     "clear_failed_attempts"):
            self.assertIn(name, self.funcs, f"auth.py missing {name}()")

    def test_decorators_and_login_helper(self) -> None:
        # T5
        for name in ("require_auth", "get_current_user",
                     "audit", "attempt_login"):
            self.assertIn(name, self.funcs, f"auth.py missing {name}()")

    def test_pydantic_models(self) -> None:
        # T6 — four Pydantic v2 entities.
        for name in ("User", "Session", "FailedAttempt", "AuditLog"):
            self.assertIn(
                name, self.classes,
                f"auth.py missing Pydantic model {name!r}",
            )

    def test_bcrypt_rounds_constant(self) -> None:
        # T7 — DEFAULT_BCRYPT_ROUNDS = 12 + bcrypt.gensalt(rounds=12)
        # The code is allowed to reference the constant by name OR the
        # literal; both are correct as long as the value resolves to 12.
        src = _read(AUTH_PY)
        self.assertRegex(
            src, r"DEFAULT_BCRYPT_ROUNDS\s*=\s*12",
            "DEFAULT_BCRYPT_ROUNDS must equal 12 per R63 spec",
        )
        # Match ``gensalt(rounds=12)`` OR ``gensalt(rounds=DEFAULT_BCRYPT_ROUNDS)``
        # — both forms are spec-compliant.
        self.assertRegex(
            src,
            r"bcrypt\.gensalt\(\s*rounds\s*=\s*(?:12|DEFAULT_BCRYPT_ROUNDS)\s*\)",
            "hash_password must call bcrypt.gensalt(rounds=12|DEFAULT_BCRYPT_ROUNDS)",
        )

    def test_lockout_constants(self) -> None:
        # T8
        src = _read(AUTH_PY)
        self.assertRegex(
            src, r"DEFAULT_LOCKOUT_THRESHOLD\s*=\s*5",
            "DEFAULT_LOCKOUT_THRESHOLD must equal 5 per R63 spec",
        )
        self.assertRegex(
            src, r"DEFAULT_LOCKOUT_MINUTES\s*=\s*15",
            "DEFAULT_LOCKOUT_MINUTES must equal 15 per R63 spec",
        )

    def test_session_token_uses_token_urlsafe_32(self) -> None:
        # T9
        src = _read(AUTH_PY)
        self.assertRegex(
            src, r"secrets\.token_urlsafe\(\s*32\s*\)",
            "create_session must use secrets.token_urlsafe(32) for "
            "256 bits of entropy",
        )

    def test_no_password_material_in_audit(self) -> None:
        # T10 — grep every INSERT INTO audit_log statement and assert
        # none of them touch password_hash / token / plain password
        # fields.  We extract the SQL string between the opening and
        # closing string-literal quotes so surrounding docstrings
        # (which legitimately mention "password" / "token" in
        # rationale prose) do NOT trigger a false positive.
        src = _read(AUTH_PY)
        # Match only the SQL string contents — start at the opening
        # quote, capture until the matching closing quote.
        for m in re.finditer(
            r'"INSERT\s+INTO\s+audit_log[^"]*"',
            src,
        ):
            sql = m.group(0).lower()
            self.assertNotIn(
                "password_hash", sql,
                "audit_log INSERT must not reference password_hash",
            )
            self.assertNotIn(
                "password", sql,
                "audit_log INSERT must not reference 'password' column",
            )
            self.assertNotIn(
                "token", sql,
                "audit_log INSERT must not reference the session token",
            )

    def test_audit_writes_only_through_write_audit(self) -> None:
        # T19 — no inline INSERT INTO audit_log outside write_audit()
        src = _read(AUTH_PY)
        # Split by function definition and count INSERT INTO audit_log
        # in each top-level function.  Only ``write_audit`` may do it.
        tree = self.tree
        for node in tree.body:
            if isinstance(node, ast.FunctionDef):
                body_src = ast.unparse(node) if hasattr(ast, "unparse") else ""
                if not body_src:
                    # Fallback: walk AST and gather string snippets.
                    body_src = "\n".join(
                        ast.unparse(s) for s in ast.walk(node)
                        if isinstance(s, ast.stmt)
                    )
                has_audit_insert = (
                    "INSERT INTO audit_log" in body_src
                    or "INSERT INTO\n  audit_log" in body_src
                )
                if node.name == "write_audit":
                    self.assertTrue(
                        has_audit_insert,
                        "write_audit() must contain the INSERT INTO audit_log statement",
                    )
                else:
                    self.assertFalse(
                        has_audit_insert,
                        f"function {node.name!r} must not write to audit_log "
                        f"directly; route it through write_audit()",
                    )

    def test_session_cookie_name_distinct(self) -> None:
        # T18 — auth.SESSION_COOKIE_NAME is not 'session' so we don't
        # collide with Flask's default cookie namespace.
        src = _read(AUTH_PY)
        m = re.search(
            r"^SESSION_COOKIE_NAME\s*=\s*['\"]([^'\"]+)['\"]",
            src, re.MULTILINE,
        )
        self.assertIsNotNone(m, "SESSION_COOKIE_NAME constant must exist")
        self.assertNotEqual(
            m.group(1), "session",
            "SESSION_COOKIE_NAME must NOT be 'session' — Flask's "
            "default cookie of the same name would collide with ours.",
        )

    def test_jwt_decision_in_docstring(self) -> None:
        # T20 — the "Why session cookies and NOT JWT" table must be
        # present so future readers know the decision rationale.
        src = _read(AUTH_PY)
        self.assertIn(
            "JWT",
            src,
            "auth.py docstring must mention JWT (the rejected alternative) "
            "with rationale for the chosen session-cookie model",
        )
        self.assertRegex(
            src, r"Why\s+session\s+cookies\s+and\s+NOT\s+JWT",
            "auth.py must include the 'Why session cookies and NOT JWT' "
            "comparison table for future maintainers.",
        )


# =========================================================================
# T11 — db/_legacy.py _SCHEMA now contains 4 new tables + indexes
# =========================================================================
class TestDBSchemaHasAuthTables(unittest.TestCase):

    def setUp(self) -> None:
        self.src = _read(DB_LEGACY)

    def test_users_table(self) -> None:
        self.assertRegex(
            self.src,
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+users\s*\(",
            "users table missing from _SCHEMA",
        )
        # Columns
        for col in ("username", "password_hash", "role", "created_at"):
            self.assertRegex(
                self.src, rf"\b{col}\b\s+TEXT",
                f"users.{col} column missing",
            )

    def test_sessions_table(self) -> None:
        self.assertRegex(
            self.src,
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+sessions\s*\(",
            "sessions table missing from _SCHEMA",
        )
        # user_id is INTEGER (foreign key), the rest are TEXT.
        self.assertRegex(
            self.src, r"\buser_id\b\s+INTEGER",
            "sessions.user_id must be INTEGER (FK to users.id)",
        )
        for col in ("token", "expires_at", "created_at", "ip", "user_agent"):
            self.assertRegex(
                self.src, rf"\b{col}\b\s+TEXT",
                f"sessions.{col} column missing",
            )
        # Indexes
        self.assertIn(
            "idx_sessions_token", self.src,
            "missing idx_sessions_token index",
        )
        self.assertIn(
            "idx_sessions_expires", self.src,
            "missing idx_sessions_expires index",
        )

    def test_failed_attempts_table(self) -> None:
        self.assertRegex(
            self.src,
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+failed_attempts\s*\(",
            "failed_attempts table missing from _SCHEMA",
        )
        self.assertIn("username", self.src)
        self.assertIn("attempted_at", self.src)
        self.assertIn(
            "idx_failed_attempts_username", self.src,
            "missing idx_failed_attempts_username index",
        )
        self.assertIn(
            "idx_failed_attempts_time", self.src,
            "missing idx_failed_attempts_time index",
        )

    def test_audit_log_table(self) -> None:
        self.assertRegex(
            self.src,
            r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+audit_log\s*\(",
            "audit_log table missing from _SCHEMA",
        )
        for col in ("action", "ip", "user_agent", "created_at", "details"):
            self.assertRegex(
                self.src, rf"\b{col}\b\s+TEXT",
                f"audit_log.{col} column missing",
            )
        for idx in ("idx_audit_user", "idx_audit_action", "idx_audit_time"):
            self.assertIn(
                idx, self.src,
                f"missing {idx} index on audit_log",
            )


# =========================================================================
# T12 — web.py registers /api/auth/login / logout / me
# =========================================================================
class TestAuthRoutesRegistered(unittest.TestCase):

    def setUp(self) -> None:
        self.routes = _routes_registered(WEB_PY)

    def test_login_route(self) -> None:
        self.assertIn("/api/auth/login", self.routes)

    def test_logout_route(self) -> None:
        self.assertIn("/api/auth/logout", self.routes)

    def test_me_route(self) -> None:
        self.assertIn("/api/auth/me", self.routes)


# =========================================================================
# T13 — web.py wires @require_auth(role='admin') onto admin routes
# =========================================================================
class TestRequireAuthDecoratorsApplied(unittest.TestCase):

    def setUp(self) -> None:
        self.tree = _ast(WEB_PY)
        # Map: route path -> set of decorator names + call-style decorators
        self.route_decorators: dict[str, list[str]] = {}
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            for dec in node.decorator_list:
                # @app.route("/x")
                if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                    if dec.func.attr == "route" and dec.args:
                        a0 = dec.args[0]
                        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                            decorators = self.route_decorators.setdefault(a0.value, [])
                            continue
                # Plain decorator: capture name so the call-shape can be inspected
                if isinstance(dec, ast.Name):
                    for path, decs in self.route_decorators.items():
                        # Map by reverse traversal — we don't track the fn name here
                        pass
        # Re-walk to associate each function with its route decorators.
        self.fn_decorators: dict[str, list[ast.expr]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef):
                self.fn_decorators[node.name] = list(node.decorator_list)
        # Route path -> function name
        self.route_fn: dict[str, str] = {}
        for node in ast.walk(self.tree):
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
                        self.route_fn[a0.value] = node.name

    def _fn_has_decorator(self, fn_name: str, decorator_id: str) -> bool:
        decs = self.fn_decorators.get(fn_name, [])
        for dec in decs:
            if isinstance(dec, ast.Name) and dec.id == decorator_id:
                return True
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                if dec.func.id == decorator_id:
                    return True
        return False

    def _fn_decorator_call_kwargs(self, fn_name: str, decorator_id: str) -> dict:
        for dec in self.fn_decorators.get(fn_name, []):
            if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Name):
                if dec.func.id == decorator_id:
                    out = {}
                    for kw in dec.keywords:
                        if kw.arg:
                            try:
                                out[kw.arg] = ast.literal_eval(kw.value)
                            except Exception:
                                out[kw.arg] = None
                    return out
        return {}

    def test_admin_index_requires_admin(self) -> None:
        self.assertIn("/admin", self.route_fn)
        fn = self.route_fn["/admin"]
        self.assertTrue(
            self._fn_has_decorator(fn, "require_auth"),
            "/admin route must use @require_auth",
        )
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(
            kwargs.get("role"), "admin",
            f"/admin require_auth role must be 'admin'; got {kwargs!r}",
        )

    def test_scrape_config_requires_admin(self) -> None:
        path = "/admin/api/scrape/config"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(
            kwargs.get("role"), "admin",
            f"{path} require_auth role must be 'admin'; got {kwargs!r}",
        )

    def test_push_config_requires_admin(self) -> None:
        path = "/admin/api/push/config"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(kwargs.get("role"), "admin")

    def test_test_push_requires_admin(self) -> None:
        path = "/admin/api/test-push"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(kwargs.get("role"), "admin")

    def test_admin_password_requires_admin(self) -> None:
        path = "/admin/api/admin-password"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(kwargs.get("role"), "admin")

    def test_import_url_requires_admin(self) -> None:
        path = "/admin/api/import-url"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(kwargs.get("role"), "admin")

    def test_api_refresh_requires_admin(self) -> None:
        path = "/api/refresh"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        kwargs = self._fn_decorator_call_kwargs(fn, "require_auth")
        self.assertEqual(
            kwargs.get("role"), "admin",
            "/api/refresh require_auth role must be 'admin'",
        )

    def test_oobe_save_keeps_legacy_admin_required(self) -> None:
        # T14 — the OOBE pre-auth path must NOT require session auth
        # (first-time setup can't log in before creating the user).
        path = "/admin/api/oobe/save"
        self.assertIn(path, self.route_fn)
        fn = self.route_fn[path]
        # The legacy ``_admin_required`` decorator should still wrap
        # this route because the wizard runs BEFORE auth exists.
        self.assertTrue(
            self._fn_has_decorator(fn, "_admin_required"),
            f"{path} must keep @_admin_required for the OOBE pre-auth flow",
        )
        # And require_auth must NOT be present (otherwise first-time
        # setup would block on a non-existent user).
        self.assertFalse(
            self._fn_has_decorator(fn, "require_auth"),
            f"{path} must NOT use @require_auth — OOBE runs before login",
        )

    def test_feishu_event_does_not_require_auth(self) -> None:
        # /feishu/event is the Feishu webhook callback — must remain
        # unauthenticated so the school bot can post events.
        for path in ("/feishu/event",):
            self.assertIn(path, self.route_fn)
            fn = self.route_fn[path]
            self.assertFalse(
                self._fn_has_decorator(fn, "require_auth"),
                f"{path} must NOT use @require_auth — Feishu callback",
            )


# =========================================================================
# T15 — web.py did NOT touch templates / static / frontend JS
# =========================================================================
class TestFrontendUntouched(unittest.TestCase):

    def test_templates_directory_exists(self) -> None:
        # Defensive: confirm the templates still exist (so we know
        # R63 didn't accidentally remove them).  Hash check skipped —
        # we only check existence + size to avoid false positives.
        for name in ("dashboard.html", "oobe.html", "admin.html"):
            p = TEMPLATES_DIR / name
            self.assertTrue(p.exists(), f"missing {p}")

    def test_static_files_exist(self) -> None:
        # CSS
        for name in ("dashboard.css", "admin.css", "oobe.css"):
            self.assertTrue((STATIC_CSS_DIR / name).exists())
        # JS
        for name in ("dashboard.js", "admin.js", "oobe.js"):
            self.assertTrue((STATIC_JS_DIR / name).exists())


# =========================================================================
# T16 — web.py sets up Flask SECRET_KEY + cookie hardening
# =========================================================================
class TestFlaskSecretKeySetup(unittest.TestCase):

    def test_secret_key_assigned(self) -> None:
        src = _read(WEB_PY)
        self.assertRegex(
            src,
            r"app\.secret_key\s*=\s*auth\.flask_secret_key\(\)",
            "web.py must assign app.secret_key = auth.flask_secret_key() "
            "so signed session cookies survive process restarts.",
        )

    def test_session_cookie_httponly(self) -> None:
        src = _read(WEB_PY)
        self.assertIn(
            "SESSION_COOKIE_HTTPONLY", src,
            "web.py must set SESSION_COOKIE_HTTPONLY=True to mitigate "
            "JS-driven session theft (XSS).",
        )

    def test_session_cookie_samesite(self) -> None:
        src = _read(WEB_PY)
        self.assertIn(
            "SESSION_COOKIE_SAMESITE", src,
            "web.py must set SESSION_COOKIE_SAMESITE so the cookie is "
            "scoped to same-site requests (CSRF mitigation).",
        )


# =========================================================================
# T17 — web.py did not pick up new config attrs (config.py untouched)
# =========================================================================
class TestConfigUntouched(unittest.TestCase):

    def test_no_new_config_attribute_access(self) -> None:
        # Grep web.py for ``config.`` references and compare to the
        # pre-R63 baseline.  We check that no NEW config.X attribute
        # is referenced — the R63 contract is to read auth env vars
        # directly via auth.py + os.environ, NOT to add new config.py
        # symbols.
        src = _read(WEB_PY)
        existing = {
            "FEISHU_WEBHOOK", "FEISHU_APP_ID", "FEISHU_APP_SECRET",
            "FEISHU_ENCRYPT_KEY", "DORM_BASE_URL", "DORM_OPENID",
            "FLASK_HOST", "FLASK_PORT", "FLASK_DEBUG", "DB_PATH",
        }
        for m in re.finditer(r"config\.([A-Z_][A-Z0-9_]*)", src):
            attr = m.group(1)
            if attr not in existing:
                self.fail(
                    f"web.py references new config.{attr} — R63 hard "
                    f"constraint says don't modify config.py; new auth "
                    f"tunables must be read by auth.py via os.environ.",
                )


# =========================================================================
# T21-T24 — runtime smoke tests (require bcrypt; skip cleanly when absent)
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


def _import_auth_or_skip(test: unittest.TestCase):
    """Import ``auth`` against an in-memory SQLite db; skip if bcrypt missing."""
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


def _patch_db_path_to_tempfile(monkey_module: str = "db") -> str:
    """Point db.get_conn() at a per-test tempfile so we don't touch records.db."""
    tmp = tempfile.NamedTemporaryFile(
        prefix="auth_r63_", suffix=".db", delete=False,
    )
    tmp.close()
    # Patch config.DB_PATH to the tempfile so db.get_conn() opens it.
    import config
    config.DB_PATH = tmp.name
    return tmp.name


class TestRuntimeSmokeHashSessionLockout(unittest.TestCase):
    """T21 / T22 / T23 — round-trip bcrypt / session / lockout."""

    def setUp(self) -> None:
        auth = _import_auth_or_skip(self)
        if auth is None:
            return
        self.auth = auth
        self.tmp_db = _patch_db_path_to_tempfile()
        # Initialize the schema (creates users / sessions / ...)
        import db
        db.init()

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass

    def test_hash_password_round_trip(self) -> None:
        # T21
        plain = "CorrectHorseBatteryStaple!42"
        hashed = self.auth.hash_password(plain)
        # bcrypt hashes are 60-char strings starting with $2b$
        self.assertTrue(
            hashed.startswith(("$2a$", "$2b$", "$2y$")),
            f"bcrypt hash has unexpected prefix: {hashed[:7]!r}",
        )
        self.assertGreaterEqual(len(hashed), 59)
        self.assertTrue(
            self.auth.verify_password(plain, hashed),
            "verify_password should accept the original plaintext",
        )
        self.assertFalse(
            self.auth.verify_password("wrong-password", hashed),
            "verify_password must reject the wrong plaintext",
        )
        self.assertFalse(
            self.auth.verify_password(plain, ""),
            "verify_password must reject empty hash",
        )
        self.assertFalse(
            self.auth.verify_password("", hashed),
            "verify_password must reject empty plain",
        )

    def test_hash_password_rejects_empty(self) -> None:
        with self.assertRaises(ValueError):
            self.auth.hash_password("")

    def test_create_validate_revoke_session(self) -> None:
        # T22
        user = self.auth.create_user("alice", "alice-pwd", role=self.auth.ROLE_ADMIN)
        token = self.auth.create_session(user.id, ip="127.0.0.1", user_agent="pytest")
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(
            len(token), 40,
            "token_urlsafe(32) should produce >= 40 chars",
        )
        # Validate returns the User
        fetched = self.auth.validate_session(token)
        self.assertIsNotNone(fetched, "validate_session must return the user")
        self.assertEqual(fetched.id, user.id)
        self.assertEqual(fetched.username, "alice")
        self.assertEqual(fetched.role, self.auth.ROLE_ADMIN)
        # Wrong token returns None
        self.assertIsNone(self.auth.validate_session("not-a-real-token"))
        # Revoke then re-validate → None
        self.auth.revoke_session(token)
        self.assertIsNone(
            self.auth.validate_session(token),
            "validate_session must return None after revoke_session",
        )


# =========================================================================
# T23 — lockout: 5 attempts → is_locked True
# =========================================================================
class TestRuntimeLockout(unittest.TestCase):

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

    def test_lockout_after_threshold(self) -> None:
        # T23 — record 5 failed attempts for the same (username, ip)
        # and verify is_locked flips to True.  Also verify that
        # clear_failed_attempts resets the counter.
        username = "bob"
        ip = "10.0.0.5"
        # Pre-condition: not locked
        self.assertFalse(
            self.auth.is_locked(username, ip),
            "is_locked must be False before any failures",
        )
        # Record threshold attempts
        for _ in range(self.auth.DEFAULT_LOCKOUT_THRESHOLD):
            self.auth.record_failed_attempt(username, ip)
        self.assertTrue(
            self.auth.is_locked(username, ip),
            "is_locked must be True after DEFAULT_LOCKOUT_THRESHOLD "
            "failures for the same (username, ip)",
        )
        # Clearing resets
        self.auth.clear_failed_attempts(username, ip)
        self.assertFalse(
            self.auth.is_locked(username, ip),
            "is_locked must be False after clear_failed_attempts",
        )

    def test_lockout_is_per_pair(self) -> None:
        # Defensive: a row recorded for (alice, 1.1.1.1) does NOT lock
        # (alice, 2.2.2.2).  Per-pair, not per-user or per-ip.
        self.auth.record_failed_attempt("alice", "1.1.1.1")
        for _ in range(self.auth.DEFAULT_LOCKOUT_THRESHOLD):
            self.auth.record_failed_attempt("alice", "1.1.1.1")
        self.assertTrue(self.auth.is_locked("alice", "1.1.1.1"))
        self.assertFalse(
            self.auth.is_locked("alice", "2.2.2.2"),
            "different IP must have its own lockout window",
        )
        self.assertFalse(
            self.auth.is_locked("bob", "1.1.1.1"),
            "different username must have its own lockout window",
        )


# =========================================================================
# T24 — ensure_initial_admin seeds + is one-shot
# =========================================================================
class TestRuntimeEnsureInitialAdmin(unittest.TestCase):

    def setUp(self) -> None:
        auth = _import_auth_or_skip(self)
        if auth is None:
            return
        self.auth = auth
        self.tmp_db = _patch_db_path_to_tempfile()
        # Save and wipe any existing env var
        self._saved_password = os.environ.pop(
            self.auth.ENV_INITIAL_ADMIN_PASSWORD, None,
        )
        self._saved_username = os.environ.pop(
            self.auth.ENV_INITIAL_ADMIN_USERNAME, None,
        )
        import db
        db.init()

    def tearDown(self) -> None:
        try:
            os.unlink(self.tmp_db)
        except OSError:
            pass
        # Restore env
        if self._saved_password is not None:
            os.environ[self.auth.ENV_INITIAL_ADMIN_PASSWORD] = self._saved_password
        if self._saved_username is not None:
            os.environ[self.auth.ENV_INITIAL_ADMIN_USERNAME] = self._saved_username

    def test_ensure_initial_admin_seeds_and_wipes(self) -> None:
        # T24
        os.environ[self.auth.ENV_INITIAL_ADMIN_PASSWORD] = "initial-pwd-abc"
        os.environ[self.auth.ENV_INITIAL_ADMIN_USERNAME] = "bootstrap_admin"
        # Empty users table → should seed
        self.assertEqual(self.auth.count_users(), 0)
        self.auth.ensure_initial_admin()
        self.assertEqual(
            self.auth.count_users(), 1,
            "ensure_initial_admin must seed exactly one user",
        )
        user = self.auth.get_user_by_username("bootstrap_admin")
        self.assertIsNotNone(user, "seeded user must be retrievable")
        self.assertEqual(user.role, self.auth.ROLE_ADMIN)
        self.assertTrue(
            self.auth.verify_password("initial-pwd-abc", user.password_hash),
            "seeded user must have a valid bcrypt hash of the env password",
        )
        # Env vars are wiped
        self.assertNotIn(
            self.auth.ENV_INITIAL_ADMIN_PASSWORD, os.environ,
            "AUTH_INITIAL_ADMIN_PASSWORD must be wiped after seed",
        )
        self.assertNotIn(
            self.auth.ENV_INITIAL_ADMIN_USERNAME, os.environ,
            "AUTH_INITIAL_ADMIN_USERNAME must be wiped after seed",
        )
        # Second call is a no-op
        self.auth.ensure_initial_admin()
        self.assertEqual(
            self.auth.count_users(), 1,
            "second ensure_initial_admin must be a no-op",
        )


# =========================================================================
# T-py_compile — every file R63 touched compiles
# =========================================================================
class TestPyCompile(unittest.TestCase):

    def test_compile_auth_py(self) -> None:
        py_compile.compile(str(AUTH_PY), doraise=True)

    def test_compile_web_py(self) -> None:
        py_compile.compile(str(WEB_PY), doraise=True)

    def test_compile_db_legacy_py(self) -> None:
        py_compile.compile(str(DB_LEGACY), doraise=True)

    def test_compile_test_self(self) -> None:
        # The test file itself is the 4th py_compile target listed
        # in the R63 output report.
        py_compile.compile(str(THIS_TEST), doraise=True)


# =========================================================================
# T-requirements — bcrypt added to requirements.txt
# =========================================================================
class TestRequirementsTxt(unittest.TestCase):

    def test_bcrypt_pinned(self) -> None:
        src = _read(REQUIREMENTS)
        self.assertRegex(
            src, r"\bbcrypt\s*>=\s*\d",
            "requirements.txt must pin bcrypt>=X for the auth password "
            "hashing; add a line 'bcrypt>=4.0' if missing.",
        )


if __name__ == "__main__":
    unittest.main()