"""Round 37 — OOBE bug fixes (B1 / B2 / B3 / B4 / B5 / B8).

Background
==========
Round 35's reviewer flagged 11 OOBE bugs; this round fixes the 6
marked 🔴 / 🟡 as must-fix:

  * B1 — OOBE step 1 「下一步」 button used ``location.reload()`` which
    re-rendered step 1 forever.  Now calls ``goNext()`` to advance.
  * B2 — ``_post_feishu`` swallowed ``RequestException`` silently.
    Added ``raise_on_error: bool = False`` kwarg.
  * B3 — OOBE step 5 POSTed a test push unconditionally.  Now the
    wizard just advances; test push only fires when the user clicks
    the explicit "发送测试消息" button.
  * B4 — OOBE step 2/3 silently accepted missing required fields.
    Now returns 400 with an error string the frontend toasts.
  * B5 — ``_parse_school_url`` had no SSRF protection.  Added host
    whitelist, IP literal rejection (loopback / link-local / private
    / metadata), scheme allowlist, ``allow_redirects=False``.
  * B8 — SQL skip + no admin password = deadlock.  ``/admin`` 401'd
    every request, including ``/admin/api/admin-password``.  Added
    a dedicated ``/admin/set-password`` route that bypasses the
    ``_admin_required`` decorator and lets the operator bootstrap a
    password after a SQL skip.

Not addressed this round (user-deferred):

  * B6 / B7 / B9 / B10 / B11 — see Round 35 reviewer report.

Coverage (>= 8 testcases as required by the round-37 spec):

  T1  ``test_oobe_step1_button_calls_goNext``
        — OOBE_HTML JS: step 1 button onclick is ``() => goNext()``,
          NOT ``() => location.reload()``.
  T2  ``test_post_feishu_raise_on_error_raises_on_4xx``
        — ``_post_feishu(card, raise_on_error=True)`` raises
          ``requests.RequestException`` when the webhook returns 4xx.
  T3  ``test_post_feishu_silent_by_default``
        — default ``raise_on_error=False`` swallows the exception
          (cron-friendly behavior preserved).
  T4  ``test_oobe_save_step2_returns_400_on_missing_roomId``
        — ``POST /admin/api/oobe/save`` step=2 with a URL that has
          openid but no roomId returns 400, doesn't write meta.
  T5  ``test_parse_school_url_rejects_localhost``
        — ``_parse_school_url("http://127.0.0.1/...?openid=...")``
          refuses (no HTTP call, returns empty roomId).
  T6  ``test_parse_school_url_rejects_169_254``
        — ``_parse_school_url("http://169.254.169.254/.../?openid=...")``
          refuses the AWS metadata address.
  T7  ``test_parse_school_url_rejects_open_redirect``
        — when ``requests.get`` would have followed a redirect
          (``allow_redirects=False`` + 3xx response), the function
          does NOT follow and returns empty roomId.
  T8  ``test_admin_required_bypass_when_skip_no_password``
        — when ``oobe_completed='1'`` AND no admin_password is set,
          ``/admin`` returns 401 (existing behavior preserved), but
          ``/admin/set-password`` is reachable without auth.
  T9  ``test_set_password_route_no_admin_required``
        — POST ``/admin/set-password`` with ``{"admin_password": ...}``
          persists meta and returns a 302 to ``/admin`` (success path).
  T10 ``test_oobe_save_step5_no_longer_fires_push``
        — ``POST /admin/api/oobe/save`` step=5 does NOT call
          ``_post_feishu`` (B3 fix; the only push is via
          ``/admin/api/test-push``).
  T11 ``test_set_password_route_redirects_home_if_oobe_incomplete``
        — sanity check: ``/admin/set-password`` redirect to ``/``
          when OOBE is not yet complete (prevents accidental bypass).
  T12 ``test_round37_syntax_and_exports``
        — py_compile + AST export sanity (mirrors round 35/36).

Uses only:
  * ``sqlite3`` in-memory (no disk DB)
  * ``unittest.mock`` for env-var / dependency isolation
  * ``py_compile`` + ``ast.parse`` for syntax / symbol presence checks

Run:    python -m unittest test_round37 -v

NOTE: per the user's standing preference we DO NOT run this on the
local machine — the test file is shipped to the server and the operator
runs it there.
"""
from __future__ import annotations

import ast
import importlib
import os
import pathlib
import py_compile
import re
import sqlite3
import sys
import unittest
from unittest import mock

PROJ_DIR = pathlib.Path(__file__).resolve().parent
os.chdir(PROJ_DIR)
sys.path.insert(0, str(PROJ_DIR))

# Force a fresh import of config / web so we get the new symbols even if
# a previous test cached an older module version.
if "config" in sys.modules:
    importlib.reload(sys.modules["config"])
if "web" in sys.modules:
    del sys.modules["web"]
import config  # noqa: E402
import db as _db  # noqa: E402
import web as web_module  # noqa: E402


# ---------------------------------------------------------------------------
# Test DB scaffolding (shared in-memory SQLite so each test is isolated)
# ---------------------------------------------------------------------------

def _make_test_db() -> None:
    """Create an isolated in-memory SQLite with the schema initialized."""
    _make_test_db._COUNTER = getattr(_make_test_db, "_COUNTER", 0) + 1
    shared_uri = (
        f"file:test_round37_db_{_make_test_db._COUNTER}"
        f"?mode=memory&cache=shared"
    )

    pin = sqlite3.connect(shared_uri, uri=True, timeout=30,
                          isolation_level=None)
    pin.row_factory = sqlite3.Row
    pin.execute("PRAGMA foreign_keys=ON;")
    _db._TEST_CONN = pin
    _db._TEST_CONN_URI = shared_uri

    def _shared_conn():
        c = sqlite3.connect(shared_uri, uri=True, timeout=30,
                            isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON;")
        return c

    _db._ORIGINAL_GET_CONN = getattr(_db, "get_conn", None)
    _db.get_conn = _shared_conn
    pin.executescript(_db._SCHEMA)


def _restore_db() -> None:
    pin = getattr(_db, "_TEST_CONN", None)
    if pin is not None:
        try:
            pin.close()
        except sqlite3.Error:
            pass
        _db._TEST_CONN = None
        _db._TEST_CONN_URI = None
    if getattr(_db, "_ORIGINAL_GET_CONN", None):
        _db.get_conn = _db._ORIGINAL_GET_CONN
        delattr(_db, "_ORIGINAL_GET_CONN")


def _reset_meta() -> None:
    """Wipe the meta table — every test starts from a clean slate."""
    with _db.get_conn() as conn:
        conn.execute("DELETE FROM meta")


# ---------------------------------------------------------------------------
# T1 — OOBE step 1 button calls goNext() (B1 fix)
# ---------------------------------------------------------------------------

class TestR37OobeStep1ButtonCallsGoNext(unittest.TestCase):
    """T1: Round 37 B1 — step 1's 「下一步 →」 button must call ``goNext()``
    (NOT ``location.reload()``).  We inspect the JS source directly so
    the test fails loudly if somebody reverts to the old reload hack.
    """

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()

    def tearDown(self) -> None:
        _restore_db()

    def test_oobe_step1_button_calls_goNext(self) -> None:
        """T1: Round 37 B1 — step 1's 「下一步 →」 button must call ``goNext()``
        (NOT ``location.reload()``).  We inspect the JS source directly so
        the test fails loudly if somebody reverts to the old reload hack.
        """
        oobe_html = web_module.OOBE_HTML
        # The step-1 template block — between the ``1: () => ({`` and
        # the closing ``}),`` of the templates dict.
        match = re.search(
            r"1:\s*\(\)\s*=>\s*\(\{(.+?)\}\),",
            oobe_html,
            re.DOTALL,
        )
        self.assertIsNotNone(
            match,
            "OOBE step 1 template block not found in OOBE_HTML",
        )
        block = match.group(1)

        # Bug-fix spec:
        #   * actions[].onclick must contain `() => goNext()` (positive)
        #   * actions[].onclick must NOT contain `location.reload()` (negative)
        # Comments above the actions line may legitimately mention the old
        # `location.reload()` as historical context — so we test against
        # the actions line, not the whole block.
        actions_match = re.search(
            r"actions:\s*\[(.+?)\]", block, re.DOTALL
        )
        self.assertIsNotNone(actions_match, "step 1 actions array not found")
        actions = actions_match.group(1)

        self.assertIn(
            "() => goNext()", actions,
            "Step 1 button should call goNext() (B1 fix)",
        )
        self.assertNotIn(
            "location.reload()", actions,
            "Step 1 button onclick must NOT call location.reload() — that "
            "was the bug that left users stuck on step 1 forever",
        )
        self.assertIn("下一步", actions, "Step 1 button label missing")


# ---------------------------------------------------------------------------
# T2 / T3 — _post_feishu raise_on_error flag (B2 fix)
# ---------------------------------------------------------------------------

class TestR37PostFeishuRaiseOnError(unittest.TestCase):
    """T2 / T3: Round 37 B2 — ``_post_feishu`` must honor ``raise_on_error``.
    Default behavior (cron) stays log-and-swallow; explicit callers can
    request re-raise.
    """

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()
        # Round 37d — make ``dorm_power`` visible at module-level so test
        # methods can use ``dorm_power._post_feishu`` etc.  Python's
        # ``import dorm_power`` here is a *local* import — the name
        # doesn't propagate to module globals.  We have to inject it.
        # web_module may have imported dorm_power via test_request_context
        # already; reload so a stale copy with the old _post_feishu signature
        # doesn't sneak in.
        if "dorm_power" in sys.modules:
            del sys.modules["dorm_power"]
        import dorm_power as _dp  # noqa: F401  pylint: disable=import-outside-toplevel
        globals()["dorm_power"] = _dp

    def tearDown(self) -> None:
        _restore_db()

    def test_post_feishu_signature_has_raise_on_error_kwarg(self) -> None:
        """The function exposes a ``raise_on_error`` kwarg with default False."""
        import inspect
        sig = inspect.signature(dorm_power._post_feishu)
        kwonly = [
            p for p in sig.parameters.values()
            if p.kind == inspect.Parameter.KEYWORD_ONLY
        ]
        names = [p.name for p in kwonly]
        self.assertIn(
            "raise_on_error", names,
            f"_post_feishu must accept raise_on_error kwarg; got {names}",
        )
        # Default must be False so the cron path keeps its swallow-on-error
        # behavior — backward compatibility.
        self.assertEqual(
            kwonly[names.index("raise_on_error")].default, False,
            "raise_on_error default must be False (cron-safe)",
        )

    def test_post_feishu_raise_on_error_raises_on_4xx(self) -> None:
        """T2: With ``raise_on_error=True``, a 4xx from Feishu raises
        ``requests.HTTPError`` (subclass of ``RequestException``)."""
        fake_resp = mock.Mock()
        fake_resp.status_code = 400
        fake_resp.text = '{"msg":"invalid webhook"}'
        fake_resp.raise_for_status.side_effect = (
            __import__("requests").HTTPError("400 Client Error")
        )
        with mock.patch.object(dorm_power, "_SESSION") as fake_session, \
             mock.patch.object(config, "FEISHU_WEBHOOK", "https://hook.example/x"):
            fake_session.post.return_value = fake_resp
            with self.assertRaises(Exception) as ctx:
                dorm_power._post_feishu(
                    {"msg_type": "text", "content": {"text": "x"}},
                    raise_on_error=True,
                )
            # The exception chain is HTTPError → RequestException, so
            # the caller (admin_test_push) can ``except Exception`` and
            # still catch it.  We just assert an exception was raised.
            self.assertIsNotNone(ctx.exception)

    def test_post_feishu_silent_by_default(self) -> None:
        """T3: Default ``raise_on_error=False`` swallows the exception."""
        fake_resp = mock.Mock()
        fake_resp.status_code = 500
        fake_resp.text = '{"msg":"server error"}'
        fake_resp.raise_for_status.side_effect = (
            __import__("requests").HTTPError("500 Server Error")
        )
        with mock.patch.object(dorm_power, "_SESSION") as fake_session, \
             mock.patch.object(config, "FEISHU_WEBHOOK", "https://hook.example/x"):
            fake_session.post.return_value = fake_resp
            # Must NOT raise.
            try:
                dorm_power._post_feishu(
                    {"msg_type": "text", "content": {"text": "x"}},
                )
            except Exception as exc:
                self.fail(
                    f"_post_feishu() default behavior must swallow errors; "
                    f"got exception {exc!r}"
                )


# ---------------------------------------------------------------------------
# T4 — OOBE step 2 rejects missing roomId (B4 fix)
# ---------------------------------------------------------------------------

class TestR37OobeSaveStep2RejectsMissingRoomId(unittest.TestCase):
    """T4: Round 37 B4 — ``POST /admin/api/oobe/save`` step=2 with a URL
    that yields openid but no roomId must return 400, NOT silently
    advance with empty meta.
    """

    SAMPLE_HTML_NO_ROOMID = """<html><body>
<form>
  <input type="hidden" id="roomNo" value="9999">
  <input type="hidden" id="EqPrice" value="0.5">
</form>
</body></html>"""

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()
        # Stay in OOBE mode so _admin_required passes through.
        _db.set_meta("oobe_completed", "0")
        _db.set_meta("admin_password", "")  # explicit blank
        # Round 37c — clear .env-loaded ADMIN_PASSWORD so the test client
        # doesn't need Basic Auth.  tearDown restores the original value.
        self._orig_admin_pwd = os.environ.pop("ADMIN_PASSWORD", None)

    def tearDown(self) -> None:
        _restore_db()
        # Round 37c — restore original ADMIN_PASSWORD
        if self._orig_admin_pwd is not None:
            os.environ["ADMIN_PASSWORD"] = self._orig_admin_pwd

    def _client(self):
        web_module.app.config["TESTING"] = True
        return web_module.app.test_client()

    def test_oobe_save_step2_returns_400_on_missing_roomId(self) -> None:
        # Pre-condition: meta has no last_room_id yet.
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_room_id'"
            ).fetchone()
            self.assertIsNone(row, "pre-condition: no last_room_id yet")

        # Mock requests.get to return HTML WITHOUT a roomId field.
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.text = self.SAMPLE_HTML_NO_ROOMID
        fake_resp.raise_for_status = mock.Mock()
        fake_resp.is_redirect = False

        with mock.patch.object(web_module.requests, "get",
                               return_value=fake_resp):
            client = self._client()
            resp = client.post(
                "/admin/api/oobe/save",
                json={
                    "step": "2",
                    "data": {
                        "school_url": (
                            "http://ybhqcz.fjny.edu.cn/dormEm0/finduser"
                            "?openid=test-openid-no-room"
                        ),
                    },
                },
            )

        # Must be 400, NOT 200.
        self.assertEqual(
            resp.status_code, 400,
            f"missing roomId must 400; got status={resp.status_code} "
            f"body={resp.data!r}",
        )
        body = resp.get_json()
        self.assertIsNotNone(body, "response should be JSON")
        self.assertFalse(body.get("ok"), f"response should report failure; got {body!r}")
        # The error message should hint at roomId (so the frontend toast
        # tells the user what's wrong).
        self.assertIn(
            "roomId", (body.get("error") or ""),
            f"error should mention roomId; got {body.get('error')!r}",
        )

        # Post-condition: last_room_id was NOT written.
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='last_room_id'"
            ).fetchone()
            self.assertIsNone(
                row,
                "missing-roomId POST must NOT write last_room_id",
            )


# ---------------------------------------------------------------------------
# T10 — OOBE step 5 no longer fires push (B3 fix)
# ---------------------------------------------------------------------------

class TestR37OobeSaveStep5DoesNotFirePush(unittest.TestCase):
    """T10: Round 37 B3 — step 5 ``POST /admin/api/oobe/save`` must NOT
    call ``_post_feishu``.  The only push path now is
    ``/admin/api/test-push`` (user-initiated).
    """

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()
        _db.set_meta("oobe_completed", "0")
        _db.set_meta("oobe_step", "5")
        _db.set_meta("feishu_webhook_url", "https://hook.example/test")
        # Round 37c — clear .env-loaded ADMIN_PASSWORD so the test client
        # doesn't need Basic Auth.  tearDown restores the original value.
        self._orig_admin_pwd = os.environ.pop("ADMIN_PASSWORD", None)

    def tearDown(self) -> None:
        _restore_db()
        # Round 37c — restore original ADMIN_PASSWORD
        if self._orig_admin_pwd is not None:
            os.environ["ADMIN_PASSWORD"] = self._orig_admin_pwd

    def _client(self):
        web_module.app.config["TESTING"] = True
        return web_module.app.test_client()

    def test_oobe_save_step5_does_not_call_post_feishu(self) -> None:
        client = self._client()
        with mock.patch(
            "dorm_power._post_feishu"
        ) as fake_post, mock.patch.object(config, "FEISHU_WEBHOOK", ""):
            resp = client.post(
                "/admin/api/oobe/save",
                json={"step": "5", "data": {}},
            )
        self.assertEqual(
            resp.status_code, 200, resp.data,
        )
        self.assertFalse(
            fake_post.called,
            "step 5 oobe_save must NOT call _post_feishu (B3 fix)",
        )

    def test_oobe_save_step5_advances_to_step_6(self) -> None:
        client = self._client()
        resp = client.post(
            "/admin/api/oobe/save",
            json={"step": "5", "data": {}},
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        body = resp.get_json()
        self.assertTrue(body.get("ok"))
        self.assertEqual(
            body.get("next_step"), "6",
            f"step 5 should advance to step 6; got {body!r}",
        )


# ---------------------------------------------------------------------------
# T5 / T6 / T7 — _parse_school_url SSRF protection (B5 fix)
# ---------------------------------------------------------------------------

class TestR37ParseSchoolUrlSsrf(unittest.TestCase):
    """T5-T7: Round 37 B5 — host whitelist + IP rejection + redirect refusal.

    The school host is read from ``config.get_dorm_base_url()`` (the
    lazy getter), which defaults to ``http://ybhqcz.fjny.edu.cn``.  We
    patch the getter to a known host so we don't depend on the user's
    .env.
    """

    SCHOOL = "ybhqcz.fjny.edu.cn"
    SAMPLE_HTML = """<html><body>
<form>
  <input type="hidden" id="roomId" value="abc-room-uuid">
  <input type="hidden" id="roomNo" value="8888">
  <input type="hidden" id="EqPrice" value="0.5">
</form>
</body></html>"""

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()
        # Pin the school host so we don't depend on .env values.
        self._getter_patcher = mock.patch.object(
            config, "get_dorm_base_url",
            return_value=f"http://{self.SCHOOL}",
        )
        self._getter_patcher.start()

    def tearDown(self) -> None:
        _restore_db()
        self._getter_patcher.stop()

    def test_t5_parse_school_url_rejects_localhost(self) -> None:
        """T5: ``127.0.0.1`` / ``localhost`` must be refused."""
        url = "http://127.0.0.1:6379/finduser?openid=evil"
        with mock.patch.object(web_module.requests, "get") as fake_get:
            result = web_module._parse_school_url(url)
        # Must NOT have made an HTTP call (defense wins early).
        self.assertFalse(
            fake_get.called,
            "127.0.0.1 must be refused before requests.get fires",
        )
        self.assertIsNone(
            result.get("roomId"),
            "127.0.0.1 must NOT yield a roomId (SSRF blocked)",
        )
        # openid may still be parsed from the query string — that's
        # fine, the user can see what they pasted.  The protection is
        # about NOT making the HTTP call.

    def test_t6_parse_school_url_rejects_169_254(self) -> None:
        """T6: AWS metadata address ``169.254.169.254`` must be refused."""
        url = "http://169.254.169.254/latest/meta-data/?openid=evil"
        with mock.patch.object(web_module.requests, "get") as fake_get:
            result = web_module._parse_school_url(url)
        self.assertFalse(
            fake_get.called,
            "169.254.169.254 must be refused before requests.get fires",
        )
        self.assertIsNone(
            result.get("roomId"),
            "169.254.169.254 must NOT yield a roomId (metadata blocked)",
        )

    def test_t7_parse_school_url_rejects_open_redirect(self) -> None:
        """T7: When the school response is a redirect (3xx), the function
        does NOT follow and returns empty roomId.

        This covers both the SSRF angle (open redirect to internal
        service) and Round 35 B11 (open redirect).
        """
        url = f"http://{self.SCHOOL}/dormEm0/finduser?openid=evil"
        fake_resp = mock.Mock()
        fake_resp.status_code = 302
        fake_resp.headers = {"Location": "http://internal-admin/steal"}
        fake_resp.is_redirect = True
        fake_resp.raise_for_status = mock.Mock()
        with mock.patch.object(web_module.requests, "get",
                               return_value=fake_resp) as fake_get:
            result = web_module._parse_school_url(url)
        # Must have made the call (host check passed)…
        self.assertTrue(
            fake_get.called,
            "school host check should pass before redirect check",
        )
        # …but the redirect itself must be refused.
        self.assertIsNone(
            result.get("roomId"),
            "redirected response must NOT yield a roomId",
        )

    def test_parse_school_url_allows_legitimate_school_host(self) -> None:
        """Sanity: a real school URL with the right host still works."""
        url = (
            f"http://{self.SCHOOL}/dormEm0/finduser?openid=oXyzAbC1234567890"
        )
        fake_resp = mock.Mock()
        fake_resp.status_code = 200
        fake_resp.text = self.SAMPLE_HTML
        fake_resp.is_redirect = False
        fake_resp.raise_for_status = mock.Mock()
        with mock.patch.object(web_module.requests, "get",
                               return_value=fake_resp):
            result = web_module._parse_school_url(url)
        self.assertEqual(
            result.get("roomId"), "abc-room-uuid",
            f"legitimate school URL should parse; got {result!r}",
        )


# ---------------------------------------------------------------------------
# T8 / T9 / T11 — /admin/set-password bypass for SQL-skip deadlock (B8 fix)
# ---------------------------------------------------------------------------

class TestR37AdminRequiredBypass(unittest.TestCase):
    """T8 / T9 / T11: Round 37 B8 — SQL-skip users (oobe_completed='1' +
    no admin_password) must be able to bootstrap a password via the
    dedicated ``/admin/set-password`` route, which intentionally bypasses
    ``_admin_required``.
    """

    def setUp(self) -> None:
        _make_test_db()
        _reset_meta()
        # Simulate the SQL-skip state: oobe_completed='1', no password.
        _db.set_meta("oobe_completed", "1")
        # Don't set admin_password.
        # Drop env ADMIN_PASSWORD too.
        self._orig_env = os.environ.pop("ADMIN_PASSWORD", None)

    def tearDown(self) -> None:
        _restore_db()
        if self._orig_env is not None:
            os.environ["ADMIN_PASSWORD"] = self._orig_env

    def _client(self):
        web_module.app.config["TESTING"] = True
        return web_module.app.test_client()

    def test_t8_admin_required_bypass_when_skip_no_password(self) -> None:
        """T8: ``/admin`` (data surface) still 401s in skip-no-password
        state — only ``/admin/set-password`` is the bypass."""
        client = self._client()
        resp = client.get("/admin")
        self.assertEqual(
            resp.status_code, 401,
            f"/admin must still 401 in skip-no-password state; "
            f"got {resp.status_code}",
        )
        # But /admin/set-password GET should succeed (recovery page).
        recovery = client.get("/admin/set-password")
        self.assertEqual(
            recovery.status_code, 200,
            f"/admin/set-password GET must succeed when no password set; "
            f"got {recovery.status_code} body={recovery.data[:200]!r}",
        )
        self.assertIn(
            b"Admin", recovery.data,
            "recovery page should render a form",
        )

    def test_t9_set_password_route_no_admin_required(self) -> None:
        """T9: POST ``/admin/set-password`` sets meta and redirects to
        ``/admin`` — no auth required because we're in the skip-no-
        password recovery state."""
        client = self._client()
        resp = client.post(
            "/admin/set-password",
            json={"admin_password": "freshly-set"},
        )
        # After successful POST, the route returns a 302 to /admin.
        self.assertEqual(
            resp.status_code, 302,
            f"successful POST should 302 redirect to /admin; "
            f"got {resp.status_code}",
        )
        # And meta.admin_password was persisted.
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='admin_password'"
            ).fetchone()
        self.assertIsNotNone(row, "admin_password must be persisted to meta")
        self.assertEqual(
            row["value"], "freshly-set",
            f"admin_password value wrong; row={dict(row)!r}",
        )

    def test_t11_set_password_route_redirects_home_if_oobe_incomplete(self) -> None:
        """T11: ``/admin/set-password`` while OOBE is incomplete must
        redirect to ``/`` — this route is ONLY for the SQL-skip recovery
        case, not a way to bypass the OOBE wizard."""
        # Reset oobe_completed to NOT '1'.
        with _db.get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) "
                "VALUES ('oobe_completed', '0')"
            )
        client = self._client()
        resp = client.get("/admin/set-password")
        self.assertEqual(
            resp.status_code, 302,
            f"OOBE incomplete must redirect; got {resp.status_code}",
        )
        # The Location header points home.
        loc = resp.headers.get("Location", "")
        self.assertIn("/", loc,
                      f"redirect Location should be '/' or '/...'; got {loc!r}")

    def test_set_password_route_rejects_empty_password(self) -> None:
        """Empty password must 400, same as the original admin_password endpoint."""
        client = self._client()
        resp = client.post(
            "/admin/set-password",
            json={"admin_password": ""},
        )
        self.assertEqual(
            resp.status_code, 400,
            f"empty password must 400; got {resp.status_code}",
        )

    def test_set_password_route_uses_form_encoded(self) -> None:
        """The recovery page POSTs as application/x-www-form-urlencoded.
        The route must accept that shape (not just JSON)."""
        client = self._client()
        resp = client.post(
            "/admin/set-password",
            data={"admin_password": "form-encoded-pw"},
        )
        self.assertEqual(
            resp.status_code, 302,
            f"form-encoded POST should 302; got {resp.status_code}",
        )
        with _db.get_conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='admin_password'"
            ).fetchone()
        self.assertEqual(row["value"], "form-encoded-pw")


# ---------------------------------------------------------------------------
# T12 — Syntax + AST export sanity
# ---------------------------------------------------------------------------

class TestR37SyntaxAndExports(unittest.TestCase):
    """T12: py_compile + AST walk — every changed file compiles and every
    new symbol / route is present."""

    def test_web_compiles(self) -> None:
        py_compile.compile(str(PROJ_DIR / "web.py"), doraise=True)

    def test_dorm_power_compiles(self) -> None:
        py_compile.compile(str(PROJ_DIR / "dorm_power.py"), doraise=True)

    def test_config_compiles(self) -> None:
        py_compile.compile(str(PROJ_DIR / "config.py"), doraise=True)

    def test_db_compiles(self) -> None:
        py_compile.compile(str(PROJ_DIR / "db.py"), doraise=True)

    def test_post_feishu_signature_has_raise_on_error(self) -> None:
        """AST-level guard so a future refactor doesn't silently drop
        the kwarg."""
        import inspect
        sig = inspect.signature(__import__(
            "dorm_power", fromlist=["_post_feishu"]
        )._post_feishu)
        self.assertIn(
            "raise_on_error", sig.parameters,
            "_post_feishu must accept raise_on_error kwarg",
        )

    def test_admin_set_password_recovery_route_exists(self) -> None:
        """The new /admin/set-password route must be wired."""
        text = pathlib.Path(web_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "@app.route(\"/admin/set-password\"",
            text,
            "/admin/set-password route missing",
        )
        # And it MUST NOT use @_admin_required (the whole point of B8).
        # Look for the route, then check the next 3 lines for the
        # decorator — should NOT include _admin_required.
        # Use AST for a cleaner check.
        tree = ast.parse(text)
        found_recovery = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and \
                    node.name == "admin_set_password_recovery":
                # Collect all decorators on this function.
                deco_names = {
                    ast.unparse(d) for d in node.decorator_list
                }
                self.assertNotIn(
                    "_admin_required", "\n".join(deco_names),
                    "admin_set_password_recovery must NOT have "
                    "@_admin_required (B8 fix point)",
                )
                found_recovery = True
        self.assertTrue(
            found_recovery,
            "admin_set_password_recovery() function definition missing",
        )

    def test_oobe_save_step2_validates_roomid(self) -> None:
        """The step 2 branch must contain the new validation guard."""
        text = pathlib.Path(web_module.__file__).read_text(encoding="utf-8")
        # The new branch returns 400 with a Chinese error message about
        # roomId.  Check for both the literal string and the return shape.
        self.assertIn(
            "HTML 没找到 roomId",
            text,
            "oobe_save step 2 must include the new 'roomId not found' "
            "validation error",
        )

    def test_oobe_save_step3_validates_webhook(self) -> None:
        """The step 3 branch must reject empty webhook."""
        text = pathlib.Path(web_module.__file__).read_text(encoding="utf-8")
        self.assertIn(
            "Webhook URL 不能为空",
            text,
            "oobe_save step 3 must reject empty webhook URL (B4 fix)",
        )

    def test_parse_school_url_has_ssrf_guards(self) -> None:
        """_parse_school_url body must include the new SSRF defenses."""
        text = pathlib.Path(web_module.__file__).read_text(encoding="utf-8")
        for needle in (
            "is_global",
            "allow_redirects=False",
            "Round37 SSRF",
        ):
            self.assertIn(
                needle, text,
                f"_parse_school_url must include {needle!r} guard",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)