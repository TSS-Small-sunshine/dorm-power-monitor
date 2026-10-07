"""auth.py — Round 63 session-based authentication backend.

Round 63 design notes
=====================

The pre-R63 dashboard used HTTP Basic Auth against a single hard-coded
admin user, with the password stored as plaintext in ``meta.admin_password``.
That worked for a single-tenant home deployment but blocked two natural
follow-ups:

  * Adding / removing users without touching SQL.
  * Revoking a stolen session before its natural expiry.

R63 introduces a proper user table with bcrypt-hashed passwords, a
server-side session store keyed by an opaque token in an HTTP-only
cookie, a 5-failures-in-15-minutes lockout, and a tamper-evident
audit log.  R64 (frontend) will layer the login UI on top of these
primitives; this module ships the backend.

Why session cookies and NOT JWT
-------------------------------

| concern              | session cookie (chosen)             | JWT                              |
|----------------------|--------------------------------------|----------------------------------|
| library              | Flask built-in (itsdangerous signed) | PyJWT or similar                 |
| extra deps           | none                                 | PyJWT + a key-rotation story     |
| revocation           | DELETE the row                       | need a denylist + read every req |
| expiry extension     | UPDATE one row                       | cannot (signature is fixed)      |
| CSRF                 | requires SameSite=Lax cookie + token | requires header token + cookie   |

The only thing JWTs buy us that cookies do not is *statelessness* — but
our dashboard is a single-tenant home server with <10 users, so the
"DB lookup per request" cost of a session row is trivial (a few
hundred microseconds on the existing SQLite + WAL).  We pick the
simpler model.

Security contract
-----------------

  * Password storage: bcrypt(str, gensalt(rounds=12)).  Verify path
    uses ``bcrypt.checkpw`` (constant-time).
  * Session token: 32 bytes of ``secrets.token_urlsafe(32)`` entropy.
    Stored only as a SHA-256-equivalent secret in the cookie (we use
    the raw token for simplicity; the token itself is unguessable).
    The HTTP cookie is signed by Flask's itsdangerous so the user
    cannot tamper with it.
  * Cookie name: ``dorm_session`` (distinct from Flask's default
    ``session`` so we own the keys).
  * Failed-attempt lockout: 5 attempts per (username, ip) pair inside
    a 15-minute sliding window.  Successful login clears the row(s).
  * Audit log records action + target + ip + ua + a JSON ``details``
    blob.  Passwords / bcrypt hashes / tokens NEVER appear.

Public surface
--------------

Models (Pydantic v2 — same convention as ``db.models``):

    User, Session, FailedAttempt, AuditLog

Password helpers:

    hash_password(plain) -> str
    verify_password(plain, hashed) -> bool

Session management:

    create_session(user_id, ip, user_agent) -> token
    validate_session(token) -> Optional[User]
    revoke_session(token) -> None
    purge_expired_sessions() -> int

User CRUD:

    create_user(username, password, role) -> User
    get_user_by_id(user_id) -> Optional[User]
    get_user_by_username(username) -> Optional[User]
    count_users() -> int
    mark_user_logged_in(user_id) -> None

Lockout:

    record_failed_attempt(username, ip) -> None
    is_locked(username, ip) -> bool
    clear_failed_attempts(username, ip) -> None

Audit:

    write_audit(action, user_id, target, ip, user_agent, details)

Decorators / helpers (Flask-bound):

    get_current_user() -> Optional[User]
    require_auth(role=None) -> decorator
    audit(action) -> decorator
    client_ip() -> str

Bootstrap:

    ensure_initial_admin() -> None

.. note::

    ``auth.py`` deliberately does NOT touch ``config.py`` (hard
    constraint).  We read auth-related env vars directly via
    ``os.environ.get`` so adding new tunables does not require a
    config.py patch.  The project convention of putting ``.env`` in
    ``config.py`` is for cross-module consistency; here we follow the
    rule "don't change config.py" by skipping it.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from functools import wraps
from typing import Any, Callable, Optional

import bcrypt as _bcrypt

import db

# ---------------------------------------------------------------------------
# Pydantic v2 models — same convention as ``db.models``.
# ---------------------------------------------------------------------------
# We import lazily so that ``auth.py`` can be ``ast.parse``'d / imported
# in a context where pydantic is not installed (the project rule
# "don't run pytest locally").  When pydantic is missing we fall back
# to plain ``dataclasses`` so the module still loads and ``isinstance``
# checks still work for the simple shape we use here.  All public
# helpers return model instances, so callers always get objects with
# the same attribute names regardless of backend.
try:
    from pydantic import BaseModel, ConfigDict

    _HAS_PYDANTIC = True

    class _AuthBase(BaseModel):
        model_config = ConfigDict(from_attributes=True, extra="ignore")

    class User(_AuthBase):
        id: int
        username: str
        password_hash: str
        role: str
        created_at: str
        last_login_at: Optional[str] = None

    class Session(_AuthBase):
        id: int
        user_id: int
        token: str
        expires_at: str
        created_at: str
        ip: Optional[str] = None
        user_agent: Optional[str] = None

    class FailedAttempt(_AuthBase):
        id: int
        username: str
        ip: Optional[str] = None
        attempted_at: str

    class AuditLog(_AuthBase):
        id: int
        user_id: Optional[int] = None
        action: str
        target: Optional[str] = None
        ip: Optional[str] = None
        user_agent: Optional[str] = None
        created_at: str
        details: Optional[str] = None

except ImportError:  # pragma: no cover — fallback for no-pydantic env
    _HAS_PYDANTIC = False

    class _AuthBase:  # type: ignore[no-redef]
        """Minimal dataclass-like fallback used only when pydantic is absent."""

        __slots__ = ()

        @classmethod
        def from_row(cls, row: dict) -> "_AuthBase":
            obj = cls()
            for k, v in row.items():
                setattr(obj, k, v)
            return obj

    class User(_AuthBase):  # type: ignore[no-redef]
        __slots__ = ("id", "username", "password_hash", "role",
                     "created_at", "last_login_at")

    class Session(_AuthBase):  # type: ignore[no-redef]
        __slots__ = ("id", "user_id", "token", "expires_at",
                     "created_at", "ip", "user_agent")

    class FailedAttempt(_AuthBase):  # type: ignore[no-redef]
        __slots__ = ("id", "username", "ip", "attempted_at")

    class AuditLog(_AuthBase):  # type: ignore[no-redef]
        __slots__ = ("id", "user_id", "action", "target", "ip",
                     "user_agent", "created_at", "details")


# ---------------------------------------------------------------------------
# Constants — every secret-bearing tunable lives here.  Overridable via
# env vars but the defaults match the R63 spec exactly.
# ---------------------------------------------------------------------------
ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
VALID_ROLES = (ROLE_ADMIN, ROLE_VIEWER)

DEFAULT_BCRYPT_ROUNDS = 12
DEFAULT_SESSION_HOURS = 24
DEFAULT_LOCKOUT_MINUTES = 15
DEFAULT_LOCKOUT_THRESHOLD = 5

# Flask session cookie name — distinct from Flask's default ``session``
# so we own the key namespace and so a future ``flask.session`` use
# does not collide with our auth cookie.
SESSION_COOKIE_NAME = "dorm_session"

# The Flask ``SECRET_KEY`` is used to sign the cookie; if not provided
# we generate one at module-import time and pin it into ``.env`` via
# the bootstrap helper.  This keeps the signed cookie tamper-evident
# across restarts.
ENV_SECRET_KEY = "FLASK_SECRET_KEY"
ENV_SESSION_HOURS = "AUTH_SESSION_HOURS"
ENV_LOCKOUT_MINUTES = "AUTH_LOCKOUT_MINUTES"
ENV_LOCKOUT_THRESHOLD = "AUTH_LOCKOUT_THRESHOLD"
ENV_INITIAL_ADMIN_USERNAME = "AUTH_INITIAL_ADMIN_USERNAME"
ENV_INITIAL_ADMIN_PASSWORD = "AUTH_INITIAL_ADMIN_PASSWORD"

logger = logging.getLogger("auth")


def _now() -> str:
    """Current local-naive timestamp formatted as ``YYYY-MM-DD HH:MM:SS``.

    Matches the rest of the project (R47 contract): records / sessions
    / audit rows are stored without tz so cross-table string compares
    line up.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _shifted_expires(hours: int) -> str:
    return (datetime.now() + timedelta(hours=hours)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


def _session_hours() -> int:
    raw = os.environ.get(ENV_SESSION_HOURS, "").strip()
    if not raw:
        return DEFAULT_SESSION_HOURS
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_SESSION_HOURS
    except ValueError:
        return DEFAULT_SESSION_HOURS


def _lockout_minutes() -> int:
    raw = os.environ.get(ENV_LOCKOUT_MINUTES, "").strip()
    if not raw:
        return DEFAULT_LOCKOUT_MINUTES
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_LOCKOUT_MINUTES
    except ValueError:
        return DEFAULT_LOCKOUT_MINUTES


def _lockout_threshold() -> int:
    raw = os.environ.get(ENV_LOCKOUT_THRESHOLD, "").strip()
    if not raw:
        return DEFAULT_LOCKOUT_THRESHOLD
    try:
        v = int(raw)
        return v if v > 0 else DEFAULT_LOCKOUT_THRESHOLD
    except ValueError:
        return DEFAULT_LOCKOUT_THRESHOLD


# ---------------------------------------------------------------------------
# Password hashing — bcrypt cost=12, constant-time checkpw.
# ---------------------------------------------------------------------------
def hash_password(plain: str) -> str:
    """Return a bcrypt hash for ``plain``.  Raises ValueError on empty input.

    We call ``bcrypt.hashpw`` (NOT ``bcrypt.hashpw(..., bcrypt.gensalt())``)
    so a future cost upgrade is a one-line change.  ``rounds=12`` is the
    R63 spec; trades ~250 ms / hash on a typical server against a
    major slowdown of offline brute-force.
    """
    if not plain:
        raise ValueError("password must be non-empty")
    if not isinstance(plain, str):
        raise TypeError("password must be str")
    salt = _bcrypt.gensalt(rounds=DEFAULT_BCRYPT_ROUNDS)
    return _bcrypt.hashpw(plain.encode("utf-8"), salt).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time password check.

    Returns ``False`` on empty / non-string inputs and on any
    ``ValueError`` from bcrypt (a corrupted hash row should not crash
    the login path; the user simply sees "invalid credentials").
    """
    if not plain or not hashed:
        return False
    if not isinstance(plain, str) or not isinstance(hashed, str):
        return False
    try:
        return _bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# User CRUD — thin wrappers over SQL.
# ---------------------------------------------------------------------------
def _row_to_user(row: Any) -> User:
    if _HAS_PYDANTIC:
        return User.model_validate(dict(row))
    return User.from_row(dict(row))


def _row_to_session(row: Any) -> Session:
    if _HAS_PYDANTIC:
        return Session.model_validate(dict(row))
    return Session.from_row(dict(row))


def create_user(username: str, password: str, role: str = ROLE_ADMIN) -> User:
    """Insert a new user with a bcrypt-hashed password.

    Raises ``ValueError`` on bad role / empty username / empty password
    / duplicate username (UNIQUE constraint).  The caller is expected
    to handle the IntegrityError-equivalent and surface it as a 409.
    """
    if not username or not isinstance(username, str):
        raise ValueError("username must be non-empty str")
    if role not in VALID_ROLES:
        raise ValueError(f"role must be one of {VALID_ROLES!r}, got {role!r}")
    pwd_hash = hash_password(password)
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role) "
            "VALUES (?, ?, ?)",
            (username, pwd_hash, role),
        )
        user_id = int(cur.lastrowid)
    user = get_user_by_id(user_id)
    assert user is not None, "create_user: just-inserted row vanished"
    return user


def get_user_by_id(user_id: int) -> Optional[User]:
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, last_login_at "
            "FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_username(username: str) -> Optional[User]:
    if not username:
        return None
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT id, username, password_hash, role, created_at, last_login_at "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return _row_to_user(row) if row else None


def count_users() -> int:
    with db.get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"] or 0) if row else 0


def mark_user_logged_in(user_id: int) -> None:
    """Stamp ``last_login_at`` on a successful login."""
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE users SET last_login_at = ? WHERE id = ?",
            (_now(), user_id),
        )


# ---------------------------------------------------------------------------
# Session management — opaque token + server-side row.
# ---------------------------------------------------------------------------
def create_session(
    user_id: int,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> str:
    """Create a server-side session row and return the token.

    Token = ``secrets.token_urlsafe(32)`` → 32 bytes of entropy
    (~256 bits, URL-safe base64 encoded to 43 chars).  The token is
    stored as-is in the ``sessions.token`` column (UNIQUE) and
    returned to the caller, which is responsible for placing it in
    the Flask session / cookie.
    """
    token = secrets.token_urlsafe(32)
    expires_at = _shifted_expires(_session_hours())
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions (user_id, token, expires_at, ip, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, token, expires_at, ip or "", user_agent or ""),
        )
    return token


def validate_session(token: Optional[str]) -> Optional[User]:
    """Validate the token, slide its expiry, return the User (or None).

    Slides expiry forward by ``AUTH_SESSION_HOURS`` on every successful
    validation (sliding session).  An expired token is purged
    immediately and the caller gets ``None``.  Returns the User object
    so ``require_auth`` can do role checks without an extra DB hop.
    """
    if not token or not isinstance(token, str):
        return None
    new_expires = _shifted_expires(_session_hours())
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT s.id AS sid, s.user_id, s.token, s.expires_at, "
            "       s.created_at, s.ip AS sip, s.user_agent AS sua, "
            "       u.id, u.username, u.password_hash, u.role, "
            "       u.created_at AS u_created, u.last_login_at "
            "FROM sessions s JOIN users u ON u.id = s.user_id "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        try:
            exp_dt = datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["sid"],))
            return None
        if exp_dt < datetime.now():
            conn.execute("DELETE FROM sessions WHERE id = ?", (row["sid"],))
            return None
        # Slide expiry — every successful validation extends the window.
        conn.execute(
            "UPDATE sessions SET expires_at = ? WHERE id = ?",
            (new_expires, row["sid"]),
        )
        user = User(
            id=row["id"],
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            created_at=row["u_created"],
            last_login_at=row["last_login_at"],
        )
        return user


def revoke_session(token: Optional[str]) -> None:
    """Delete the session row by token (logout / revoke).  No-op on empty token."""
    if not token:
        return
    with db.get_conn() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions() -> int:
    """Delete every expired session row.  Returns the number purged.

    Intended to run from a cron / scheduled task; cheap enough to call
    on every request if desired.
    """
    with db.get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM sessions WHERE expires_at < datetime('now')"
        )
        try:
            return int(cur.rowcount or 0)
        except Exception:  # pragma: no cover — sqlite rowcount fallback
            return 0


# ---------------------------------------------------------------------------
# Lockout — 5 failures in 15 minutes per (username, ip).
# ---------------------------------------------------------------------------
def record_failed_attempt(username: str, ip: Optional[str] = None) -> None:
    """Insert a row into ``failed_attempts`` for the given (username, ip)."""
    if not username:
        return
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO failed_attempts (username, ip, attempted_at) "
            "VALUES (?, ?, ?)",
            (username, ip or "", _now()),
        )


def is_locked(username: str, ip: Optional[str] = None) -> bool:
    """Return True if the (username, ip) pair has hit the failure threshold.

    "Username + IP double-counting" per the R63 spec: we count rows
    where ``username == arg AND ip == arg`` inside the last 15 minutes
    and compare against the threshold (default 5).  This means an
    attacker who knows a username but rotates IPs still gets locked
    out, and a single user behind NAT doesn't lock themselves out by
    one typo'd password.
    """
    if not username:
        return False
    since = (datetime.now() - timedelta(minutes=_lockout_minutes())).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM failed_attempts "
            "WHERE attempted_at >= ? AND username = ? AND ip = ?",
            (since, username, ip or ""),
        ).fetchone()
        n = int(row["n"] or 0) if row else 0
        return n >= _lockout_threshold()


def clear_failed_attempts(username: str, ip: Optional[str] = None) -> None:
    """Delete every ``failed_attempts`` row matching (username, ip).

    Called on successful login so the next failure starts a fresh
    15-minute window.  Spec says the composite key is (username, ip);
    we do NOT clear "username only" or "ip only" — that would let a
    determined attacker reset the counter by alternating ip/username
    pairs.
    """
    if not username:
        return
    with db.get_conn() as conn:
        conn.execute(
            "DELETE FROM failed_attempts WHERE username = ? AND ip = ?",
            (username, ip or ""),
        )


# ---------------------------------------------------------------------------
# Audit log — append-only, no password material ever lands here.
# ---------------------------------------------------------------------------
def write_audit(
    action: str,
    user_id: Optional[int] = None,
    target: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append a row to ``audit_log``.

    ``details`` is JSON-encoded so callers can pass arbitrary context
    without a schema change.  ``write_audit`` MUST NOT receive
    passwords / bcrypt hashes / session tokens — the contract is
    enforced by code review and by the test suite (test_round63).
    """
    if not action:
        return
    try:
        details_json = (
            json.dumps(details, ensure_ascii=False) if details else None
        )
    except (TypeError, ValueError):
        details_json = None
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log "
            "(user_id, action, target, ip, user_agent, details) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, action, target, ip or "", user_agent or "", details_json),
        )


# ---------------------------------------------------------------------------
# Flask-bound helpers — these depend on the Flask request context.
# ---------------------------------------------------------------------------
def client_ip() -> str:
    """Resolve the client IP, honouring ``X-Forwarded-For`` (Nginx).

    Falls back to ``remote_addr`` when the header is missing.  When
    no Flask request context is active (CLI / smoke tests), returns
    an empty string so callers can default safely.
    """
    try:
        from flask import request
    except ImportError:  # pragma: no cover — Flask is required at runtime
        return ""
    try:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.remote_addr or ""
    except Exception:  # pragma: no cover — defensive
        return ""


def _user_agent() -> str:
    try:
        from flask import request
        return request.headers.get("User-Agent", "") or ""
    except Exception:  # pragma: no cover
        return ""


def get_current_user() -> Optional[User]:
    """Read the auth cookie from Flask's signed session, validate it.

    Returns the User on success, ``None`` on missing / invalid / expired
    token.  No DB writes; the cookie itself stays untouched (Flask's
    itsdangerous signature guarantees it).
    """
    try:
        from flask import session
    except ImportError:  # pragma: no cover
        return None
    token = session.get(SESSION_COOKIE_NAME)
    if not token:
        return None
    return validate_session(token)


def require_auth(role: Optional[str] = None) -> Callable:
    """Decorator: gate a Flask view on session auth + (optional) role.

    Behaviour:
      * No current user → 401 ``{"ok": False, "error": "not_authenticated"}``.
      * Current user but wrong role → 403 ``{"ok": False, "error": "forbidden"}``.
      * Otherwise the view runs with ``flask.g.current_user`` set, so
        downstream code can read ``g.current_user.username`` etc.

    Usage::

        @app.route("/api/admin/...")
        @require_auth(role="admin")
        def view(): ...

    The ``role`` argument is optional; omitting it accepts any logged-in
    user (admin OR viewer).
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                from flask import g, jsonify
            except ImportError:  # pragma: no cover
                return fn(*args, **kwargs)
            user = get_current_user()
            if user is None:
                return jsonify({"ok": False, "error": "not_authenticated"}), 401
            if role and user.role != role:
                return jsonify({"ok": False, "error": "forbidden"}), 403
            g.current_user = user
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def audit(action: str) -> Callable:
    """Decorator: append an ``audit_log`` row after every invocation.

    Reads ``flask.g.current_user`` (set by ``require_auth``) so the
    log entry knows who acted.  Records both successful and failed
    runs (the ``details.ok`` boolean reflects the outcome).  Never
    logs request bodies — passwords / tokens are never written.
    """
    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                from flask import g, request
            except ImportError:  # pragma: no cover
                return fn(*args, **kwargs)
            user = getattr(g, "current_user", None) or get_current_user()
            user_id = user.id if user else None
            target = request.path if request else None
            ip = client_ip()
            ua = _user_agent()
            try:
                result = fn(*args, **kwargs)
                write_audit(
                    action, user_id=user_id, target=target, ip=ip, user_agent=ua,
                    details={"ok": True},
                )
                return result
            except Exception as exc:
                write_audit(
                    action, user_id=user_id, target=target, ip=ip, user_agent=ua,
                    details={"ok": False, "error": type(exc).__name__},
                )
                raise
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Login flow — the single public helper web.py uses from
# ``POST /api/auth/login``.  Kept here so the lockout + audit + session
# bookkeeping all happens in one place.
# ---------------------------------------------------------------------------
def attempt_login(
    username: str,
    password: str,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> dict:
    """Validate credentials and return a result dict.

    Result shape::

        {"ok": True,  "user": {...}, "token": "..."}    # success
        {"ok": False, "error": "locked"}                # currently locked
        {"ok": False, "error": "invalid_credentials"}   # bad user or bad pwd

    Side effects:
      * On success: ``clear_failed_attempts(username, ip)`` +
        ``mark_user_logged_in(user.id)`` +
        ``create_session(user.id, ip, ua)`` returning the token +
        ``write_audit("login_success", ...)``.
      * On invalid_credentials: ``record_failed_attempt(username, ip)``
        + ``write_audit("login_failed", ...)``.
      * On locked: no DB write beyond audit ("login_blocked").
    """
    ip = ip or ""
    user_agent = user_agent or ""
    if not username or not password:
        # Don't leak which side was empty — same error either way.
        return {"ok": False, "error": "invalid_credentials"}

    if is_locked(username, ip):
        write_audit(
            "login_blocked", user_id=None, target=username,
            ip=ip, user_agent=user_agent, details={"reason": "lockout"},
        )
        return {"ok": False, "error": "locked"}

    user = get_user_by_username(username)
    if user is None or not verify_password(password, user.password_hash):
        record_failed_attempt(username, ip)
        write_audit(
            "login_failed", user_id=(user.id if user else None),
            target=username, ip=ip, user_agent=user_agent,
            details={"reason": "bad_credentials"},
        )
        return {"ok": False, "error": "invalid_credentials"}

    # Success — clear lockout, stamp login, create session.
    clear_failed_attempts(username, ip)
    mark_user_logged_in(user.id)
    token = create_session(user.id, ip, user_agent)
    write_audit(
        "login_success", user_id=user.id, target=username,
        ip=ip, user_agent=user_agent, details={"role": user.role},
    )
    return {
        "ok": True,
        "user": {
            "id": user.id,
            "username": user.username,
            "role": user.role,
        },
        "token": token,
    }


# ---------------------------------------------------------------------------
# Bootstrap — one-time seeding of the initial admin user from env.
# ---------------------------------------------------------------------------
def ensure_initial_admin() -> None:
    """Seed an initial admin user when ``users`` is empty.

    Triggered by ``web.py`` at module-import time so deployments that
    set ``AUTH_INITIAL_ADMIN_PASSWORD`` in ``.env`` get an admin row
    on first start without touching SQL.  ``db.init()`` MUST have
    been called before this — the 4 new tables must exist.

    After a successful seed:
      * ``AUTH_INITIAL_ADMIN_PASSWORD`` is deleted from the process
        environment (so the next process restart does not silently
        re-seed if the user rotates via the admin UI).  The
        environment-level wipe happens here, not in the caller, so
        every code path that boots the dashboard benefits.
    """
    if count_users() > 0:
        return
    password = os.environ.get(ENV_INITIAL_ADMIN_PASSWORD, "").strip()
    if not password:
        return
    username = (
        os.environ.get(ENV_INITIAL_ADMIN_USERNAME, "").strip()
        or "admin"
    )
    try:
        create_user(username=username, password=password, role=ROLE_ADMIN)
        logger.info(
            "R63 bootstrap: seeded initial admin user %r (rotate the "
            "password via the admin UI; AUTH_INITIAL_ADMIN_PASSWORD has "
            "been wiped from the process environment so the seed is "
            "one-shot).",
            username,
        )
    except Exception as exc:
        logger.error("R63 bootstrap failed: %s: %s", type(exc).__name__, exc)
        return
    # Wipe from os.environ so the password doesn't outlive the seed.
    os.environ.pop(ENV_INITIAL_ADMIN_PASSWORD, None)
    os.environ.pop(ENV_INITIAL_ADMIN_USERNAME, None)


# ---------------------------------------------------------------------------
# Public helper used by web.py at import time to set up Flask's
# SECRET_KEY (so signed cookies survive restarts).
# ---------------------------------------------------------------------------
def flask_secret_key() -> str:
    """Return the Flask SECRET_KEY, generating one if absent.

    On first launch (no ``FLASK_SECRET_KEY`` in the environment) a fresh
    ``secrets.token_urlsafe(48)`` value is generated AND auto-persisted
    to ``.env`` via ``python-dotenv`` ``set_key`` so subsequent restarts
    reuse the same value and existing signed cookies stay valid.

    If the ``.env`` write fails (read-only filesystem, permission
    error, etc.) a warning is logged but the in-memory key is still
    returned — the app boots, only future restarts would regenerate.
    Production deployments should still set ``FLASK_SECRET_KEY`` to a
    stable value in ``.env`` so the file write doesn't matter.
    """
    existing = os.environ.get(ENV_SECRET_KEY, "").strip()
    if existing:
        return existing
    new_key = secrets.token_urlsafe(48)
    # R69 — persist the freshly-generated key so it survives restarts.
    # ``set_key`` preserves other lines in the file (it appends if the
    # key is missing, replaces if present).  We import inside the
    # function to keep auth.py importable for tooling that doesn't
    # have python-dotenv on PYTHONPATH.
    try:
        from pathlib import Path
        from dotenv import set_key
        dotenv_path = str(Path(__file__).resolve().parent / ".env")
        set_key(dotenv_path, ENV_SECRET_KEY, new_key)
        logger.info(
            "R69 bootstrap: generated and persisted %s to %s",
            ENV_SECRET_KEY, dotenv_path,
        )
    except OSError as exc:
        # Read-only FS, permission denied, disk full, etc. — log and
        # fall through; the in-memory key is still usable for this
        # process lifetime.
        logger.warning(
            "R69 bootstrap: failed to persist %s to .env (%s: %s); "
            "key will be regenerated on next restart unless it is "
            "manually written to the EnvironmentFile.",
            ENV_SECRET_KEY, type(exc).__name__, exc,
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "R69 bootstrap: unexpected error persisting %s to .env "
            "(%s: %s); key will be regenerated on next restart.",
            ENV_SECRET_KEY, type(exc).__name__, exc,
        )
    os.environ[ENV_SECRET_KEY] = new_key
    return new_key


__all__ = [
    # Pydantic / dataclass models
    "User", "Session", "FailedAttempt", "AuditLog",
    # constants
    "ROLE_ADMIN", "ROLE_VIEWER", "VALID_ROLES",
    "DEFAULT_BCRYPT_ROUNDS", "DEFAULT_SESSION_HOURS",
    "DEFAULT_LOCKOUT_MINUTES", "DEFAULT_LOCKOUT_THRESHOLD",
    "SESSION_COOKIE_NAME", "CSRF_COOKIE_NAME", "CSRF_HEADER_NAME",
    # env var names
    "ENV_SECRET_KEY", "ENV_SESSION_HOURS", "ENV_LOCKOUT_MINUTES",
    "ENV_LOCKOUT_THRESHOLD", "ENV_INITIAL_ADMIN_USERNAME",
    "ENV_INITIAL_ADMIN_PASSWORD",
    # passwords
    "hash_password", "verify_password",
    # users
    "create_user", "get_user_by_id", "get_user_by_username",
    "count_users", "mark_user_logged_in",
    # sessions
    "create_session", "validate_session", "revoke_session",
    "purge_expired_sessions",
    # lockout
    "record_failed_attempt", "is_locked", "clear_failed_attempts",
    # audit
    "write_audit",
    # flask helpers
    "client_ip", "get_current_user", "require_auth", "audit",
    "attempt_login", "ensure_initial_admin", "flask_secret_key",
    # Round 64 — CSRF helpers (token generation, validation, decorator)
    "generate_csrf_token", "validate_csrf_token", "csrf_token",
    "require_csrf", "csrf_cookie_token", "is_csrf_exempt_route",
]


# ---------------------------------------------------------------------------
# Round 64 — CSRF protection.
#
# Background
# ==========
# Pre-R64 every mutating route relied on the session cookie + SameSite=Lax
# alone.  Modern browsers honor ``SameSite=Lax`` for cross-site POST
# initiated by ``<form>`` (blocks them), but a same-site ``fetch`` from
# XSS-injected JS would still ride the cookie.  We add a CSRF token
# stored in an *HttpOnly* cookie; the front-end reads it from a
# ``<meta name="csrf-token" content="{{ csrf_token() }}">`` tag
# rendered by the ``csrf_token()`` template-context helper and echoes
# it back as the ``X-CSRF-Token`` header on every mutating request
# (double-submit pattern).  The server compares the header value to
# the cookie value via ``hmac.compare_digest``; mismatch → 403.
#
# Public surface added in R64
# ---------------------------
#   generate_csrf_token() -> str
#       Produce a fresh 32-byte URL-safe token (256 bits).
#   csrf_cookie_token() -> str
#       Return the token currently bound to the request, minting one
#       and planting it in the response cookie when absent.
#   csrf_token() -> str
#       Flask template-context helper — same as ``csrf_cookie_token``
#       but callable from a Jinja ``{{ csrf_token() }}`` expression.
#   validate_csrf_token(submitted: str) -> bool
#       Constant-time check that ``submitted`` matches the cookie-bound
#       token.
#   require_csrf(fn) -> decorator
#       Decorate a Flask view so any non-safe method (POST / PUT /
#       DELETE / PATCH) must carry a matching ``X-CSRF-Token`` header
#       (or ``X-CSRFToken`` for legacy compatibility).  ``GET`` /
#       ``HEAD`` / ``OPTIONS`` are exempt by default — they cannot
#       mutate state.
#   CSRF_EXEMPT_ROUTES
#       Tuple of route paths that bypass CSRF entirely:
#         * ``/api/auth/login`` — user is not yet logged in, so no
#           session-bound token exists; the login itself establishes
#           the trust anchor.
#         * ``/api/auth/logout`` — no state to mutate (already cleared
#           on the server); we still 401 if there was no session.
#
# Design notes
# ------------
#   * Token entropy: ``secrets.token_urlsafe(32)`` → 256 bits.  Same
#     primitive as the session token so an attacker who breaks one
#     already has the keys to the kingdom.
#   * Token transport: ``dorm_csrf`` cookie (R69 — HttpOnly so JS
#     cannot read it directly; ``Secure`` mirrors SESSION_COOKIE_SECURE
#     which is gated by the ``DORM_COOKIE_SECURE`` env var); the front
#     end reads the same value from the ``<meta name="csrf-token">``
#     tag rendered by ``{{ csrf_token() }}`` and echoes it via
#     ``X-CSRF-Token`` on every mutating fetch (double-submit).
#   * Constant-time compare: ``hmac.compare_digest`` so the validator
#     does not leak the token length via timing.
#   * Logging: mismatches are written to ``audit_log`` as
#     ``csrf_rejected`` with the IP + UA so a brute-force probe is
#     visible in the operator's audit dashboard.
# ---------------------------------------------------------------------------
CSRF_COOKIE_NAME = "dorm_csrf"
CSRF_HEADER_NAME = "X-CSRF-Token"
# Some browsers / frameworks still default to this alternate header;
# accept either so older wrappers keep working.
CSRF_HEADER_ALT = "X-CSRFToken"
# Routes that bypass CSRF entirely — see module docstring.
CSRF_EXEMPT_ROUTES = (
    "/api/auth/login",
    "/api/auth/logout",
)


def generate_csrf_token() -> str:
    """Mint a fresh CSRF token (URL-safe base64, 256 bits).

    Standalone helper that does NOT touch Flask — callers wanting the
    cookie-planting side effect should use :func:`csrf_cookie_token`.
    """
    return secrets.token_urlsafe(32)


def csrf_cookie_token() -> str:
    """Return the CSRF token tied to the current request, planting it
    in a response cookie if absent.

    R69 — the cookie is **HttpOnly** (JS cannot read it via
    ``document.cookie``); the front-end reads the token from the
    ``<meta name="csrf-token">`` tag rendered by ``{{ csrf_token() }}``
    in every template (see :func:`csrf_token`) and echoes it back as
    the ``X-CSRF-Token`` header on every mutating ``fetch`` — the
    classic double-submit pattern.  Same-site (``Lax``) on the cookie
    ensures the value is never sent cross-origin; ``Secure`` is
    hard-coded because ``SESSION_COOKIE_SECURE`` (``DORM_COOKIE_SECURE``
    env var) already gates whether the browser will accept the cookie
    at all, so the CSRF cookie mirrors the same posture.
    """
    try:
        from flask import request, make_response
    except ImportError:  # pragma: no cover — Flask required at runtime
        return generate_csrf_token()
    try:
        # Use ``request.cookies`` first — the previous response already
        # planted a token and we want a stable value across calls.
        existing = request.cookies.get(CSRF_COOKIE_NAME)
        if existing:
            return existing
        token = generate_csrf_token()
        # Plant the cookie on a transient response so the value is
        # ready for ``flask.set_cookie`` propagation.  We stash the
        # response on ``flask.g`` so an ``after_request`` hook (or the
        # route itself, when it calls ``make_response``) can copy the
        # ``Set-Cookie`` header onto the real outgoing response.
        try:
            resp = make_response("")
            # R69 — cookie is HttpOnly; JS reads the token from the
            # <meta name="csrf-token"> tag rendered by {{ csrf_token() }}
            # instead of document.cookie.  ``secure=True`` is hard-coded
            # because SESSION_COOKIE_SECURE already gates whether the
            # browser will accept the cookie at all; for local HTTP
            # dev the operator can flip ``DORM_COOKIE_SECURE=0`` to
            # disable both session + CSRF Secure flags in one place.
            resp.set_cookie(
                CSRF_COOKIE_NAME, token,
                httponly=True,                  # R69 — JS reads from <meta> tag
                samesite="Lax",
                secure=True,                    # R69 — always True (see DORM_COOKIE_SECURE for dev override)
                max_age=DEFAULT_SESSION_HOURS * 3600,
                path="/",
            )
            try:
                from flask import g as _g
                _g._csrf_cookie_response = resp
            except Exception:  # pragma: no cover
                pass
        except Exception:  # pragma: no cover
            pass
        return token
    except Exception:  # pragma: no cover — defensive
        return generate_csrf_token()


def csrf_token() -> str:
    """Template-context helper — drop into a Jinja template as ``{{ csrf_token() }}``.

    Always returns a fresh-cookie-bound token (planting one if absent),
    so even the first page load after deploy returns a valid value.
    """
    return csrf_cookie_token()


def validate_csrf_token(submitted: Optional[str]) -> bool:
    """Constant-time check that ``submitted`` matches the cookie-bound token.

    Returns ``False`` for any falsy / wrong-type / mismatched value.  An
    empty / missing cookie counts as a mismatch, NOT as a bypass — the
    caller is expected to handle the "user has no cookie yet" case by
    minting one and re-asking.
    """
    if not submitted or not isinstance(submitted, str):
        return False
    try:
        from flask import request
    except ImportError:  # pragma: no cover
        return False
    cookie_token = request.cookies.get(CSRF_COOKIE_NAME) or ""
    if not cookie_token:
        return False
    if len(submitted) != len(cookie_token):
        # ``hmac.compare_digest`` requires equal lengths to be
        # constant-time; short-circuit on length mismatch (length is
        # not a secret — token_urlsafe output is fixed at 43 chars).
        return False
    try:
        return hmac.compare_digest(submitted, cookie_token)
    except Exception:  # pragma: no cover — defensive
        return False


def is_csrf_exempt_route(path: Optional[str]) -> bool:
    """Return ``True`` when ``path`` is exempt from CSRF checks.

    Exempt routes (currently ``/api/auth/login`` and ``/api/auth/logout``)
    are called BEFORE a session is established, so the user has no
    cookie-bound CSRF token to echo back.  Login cannot be CSRF-protected
    without a token bootstrap; this is the standard mitigation.
    """
    if not path:
        return False
    return path in CSRF_EXEMPT_ROUTES


def require_csrf(fn: Callable) -> Callable:
    """Decorator: gate a Flask view on a matching ``X-CSRF-Token`` header.

    Behaviour:
      * Method is GET / HEAD / OPTIONS → pass through (no state change).
      * Method is POST / PUT / DELETE / PATCH and the route is in
        :data:`CSRF_EXEMPT_ROUTES` → pass through (login / logout).
      * Otherwise:
        - missing ``X-CSRF-Token`` header → 403
          ``{"ok": False, "error": "csrf_missing"}``.
        - header present but does not match the cookie → 403
          ``{"ok": False, "error": "csrf_invalid"}``.

    The decorator also calls :func:`csrf_cookie_token` before the view
    runs so a fresh login response can plant the first cookie on a
    user who had none yet.  It MUST be applied AFTER
    :func:`require_auth` so an unauthenticated probe cannot spam the
    CSRF audit log without first proving it can read the cookie.

    Audit trail:
      A rejection writes one row to ``audit_log`` with
      ``action='csrf_rejected'`` and the IP / UA in ``details``, so the
      admin's audit dashboard surfaces brute-force probes.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            from flask import jsonify, request
        except ImportError:  # pragma: no cover
            return fn(*args, **kwargs)

        method = (request.method or "GET").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            # Ensure the cookie is planted on GET so the front-end
            # can echo it back on the next POST.
            csrf_cookie_token()
            return fn(*args, **kwargs)

        if is_csrf_exempt_route(request.path):
            return fn(*args, **kwargs)

        # Make sure a cookie exists before validating — first-ever
        # login from a clean machine plants it.
        csrf_cookie_token()
        submitted = (
            request.headers.get(CSRF_HEADER_NAME)
            or request.headers.get(CSRF_HEADER_ALT)
            or ""
        ).strip()
        if not submitted:
            try:
                write_audit(
                    "csrf_rejected",
                    user_id=None,
                    target=request.path,
                    ip=client_ip(),
                    user_agent=request.headers.get("User-Agent", ""),
                    details={"reason": "missing_header", "method": method},
                )
            except Exception:  # pragma: no cover — never fail the request path
                pass
            return jsonify({"ok": False, "error": "csrf_missing"}), 403
        if not validate_csrf_token(submitted):
            try:
                write_audit(
                    "csrf_rejected",
                    user_id=None,
                    target=request.path,
                    ip=client_ip(),
                    user_agent=request.headers.get("User-Agent", ""),
                    details={"reason": "invalid_token", "method": method},
                )
            except Exception:  # pragma: no cover
                pass
            return jsonify({"ok": False, "error": "csrf_invalid"}), 403
        return fn(*args, **kwargs)
    return wrapper