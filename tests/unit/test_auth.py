"""``starwatt.auth`` 单元测试 —— 密码 / 会话 / 用例层 / CSRF / 策略。

重点验证 4 个**已修复缺陷**：

* **D1** —— 密码散列改用 stdlib ``scrypt``，旧 bcrypt 哈希被安全拒绝（不崩溃）
* **L17** —— DB 里只有 ``sha256(token)``，明文查不到
* **L19** —— 会话滑动过期有 **30 天绝对上限**，不会无限续命
* **Q19/B4** —— bootstrap 建号后**从 .env 抹掉**密码（用后即焚）
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from starwatt import db, timeutil
from starwatt.auth import csrf as csrf_mod
from starwatt.auth import password as pwd
from starwatt.auth import secret as secret_mod
from starwatt.auth import service
from starwatt.auth import session as session_mod
from starwatt.auth.constants import ROLE_ADMIN, ROLE_VIEWER

GOOD_PASSWORD = "Str0ng-Pass!"
OTHER_PASSWORD = "0ther-Pass#"


# ===========================================================================
# password.py —— scrypt 散列（D1）
# ===========================================================================
class TestPasswordHashing:
    def test_hash_is_self_describing(self) -> None:
        stored = pwd.hash_password(GOOD_PASSWORD)
        parts = stored.split("$")
        assert parts[0] == "scrypt"
        assert parts[1:4] == [str(pwd.SCRYPT_N), str(pwd.SCRYPT_R), str(pwd.SCRYPT_P)]

    def test_no_third_party_dependency(self) -> None:
        """D1 铁律：散列实现**不得**依赖 bcrypt / passlib。"""
        import sys

        assert "bcrypt" not in sys.modules
        assert "passlib" not in sys.modules

    def test_roundtrip(self) -> None:
        stored = pwd.hash_password(GOOD_PASSWORD)
        assert pwd.verify_password(GOOD_PASSWORD, stored) is True

    def test_wrong_password_rejected(self) -> None:
        stored = pwd.hash_password(GOOD_PASSWORD)
        assert pwd.verify_password(OTHER_PASSWORD, stored) is False
        assert pwd.verify_password(GOOD_PASSWORD + "x", stored) is False

    def test_salt_is_random(self) -> None:
        assert pwd.hash_password(GOOD_PASSWORD) != pwd.hash_password(GOOD_PASSWORD)

    def test_empty_password_rejected(self) -> None:
        with pytest.raises(ValueError):
            pwd.hash_password("")

    def test_verify_never_raises(self) -> None:
        """任何垃圾输入都只返回 False，不抛异常（认证路径不能崩）。"""
        stored = pwd.hash_password(GOOD_PASSWORD)
        for bad in ("", "x", "$", "$$$$$", "md5$abc", "scrypt$1$2$3$4$5"):
            assert pwd.verify_password(GOOD_PASSWORD, bad) is False, bad
            assert pwd.verify_password(bad, stored) is False, bad

    def test_legacy_bcrypt_hash_is_rejected_not_crashed(self) -> None:
        """D1 的代价：旧 ``$2b$`` 哈希无法验证 → 返回 False（走重设流程）。"""
        legacy = "$2b$12$abcdefghijklmnopqrstuvABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
        assert pwd.verify_password(GOOD_PASSWORD, legacy) is False

    def test_needs_rehash(self) -> None:
        assert pwd.needs_rehash(pwd.hash_password(GOOD_PASSWORD)) is False
        assert pwd.needs_rehash("scrypt$1024$8$1$AAAA$BBBB") is True  # N 过时
        assert pwd.needs_rehash("$2b$12$whatever") is True  # 旧算法
        assert pwd.needs_rehash("") is True

    def test_upgrade_path_verifies_old_params(self) -> None:
        """参数升级后，旧哈希仍能验证（然后由 needs_rehash 惰性迁移）。"""
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(
            GOOD_PASSWORD.encode(), salt=salt, n=1024, r=8, p=1, dklen=32
        )
        weak = f"scrypt$1024$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
        assert pwd.verify_password(GOOD_PASSWORD, weak) is True
        assert pwd.needs_rehash(weak) is True


class TestPasswordStrength:
    """提示文案与旧代码逐字一致（behaviors.json 契约）。"""

    def test_empty(self) -> None:
        assert pwd.validate_strength("") == "password required"

    def test_too_short(self) -> None:
        assert pwd.validate_strength("Ab1!") == (
            "password must be at least 8 characters"
        )

    def test_needs_uppercase(self) -> None:
        assert pwd.validate_strength("abcdefg1!") == (
            "password must contain an uppercase letter"
        )

    def test_needs_digit(self) -> None:
        assert pwd.validate_strength("Abcdefg!") == "password must contain a digit"

    def test_needs_special(self) -> None:
        assert pwd.validate_strength("Abcdefg1") == (
            "password must contain a special character"
        )

    def test_too_long(self) -> None:
        assert pwd.validate_strength("A1!" + "a" * 200) == (
            f"password must be {pwd.MAX_LENGTH} characters or less"
        )

    def test_accepts_good(self) -> None:
        for good in ("Abcdefg1!", "Str0ng-Pass!", "P@ssw0rd", "Aa1!" + "x" * 10):
            assert pwd.validate_strength(good) is None, good


# ===========================================================================
# secret.py —— 策略读取 + 密钥自举
# ===========================================================================
class TestSecret:
    def test_defaults(self, settings_override) -> None:
        assert secret_mod.session_hours() == secret_mod.DEFAULT_SESSION_HOURS
        assert secret_mod.lockout_minutes() == secret_mod.DEFAULT_LOCKOUT_MINUTES
        assert secret_mod.lockout_threshold() == secret_mod.DEFAULT_LOCKOUT_THRESHOLD

    def test_env_override(self, settings_override, monkeypatch) -> None:
        monkeypatch.setenv("AUTH_SESSION_HOURS", "2")
        monkeypatch.setenv("AUTH_LOCKOUT_MINUTES", "3")
        monkeypatch.setenv("AUTH_LOCKOUT_THRESHOLD", "7")
        assert secret_mod.session_hours() == 2
        assert secret_mod.lockout_minutes() == 3
        assert secret_mod.lockout_threshold() == 7

    def test_bad_values_fall_back(self, settings_override, monkeypatch) -> None:
        """认证路径绝不因配置写错而崩。"""
        for bad in ("abc", "0", "-5", "  ", "1.5"):
            monkeypatch.setenv("AUTH_SESSION_HOURS", bad)
            assert secret_mod.session_hours() == secret_mod.DEFAULT_SESSION_HOURS, bad

    def test_flask_secret_key_uses_env(self, settings_override) -> None:
        assert secret_mod.flask_secret_key() == "test-secret-key-not-for-production"

    def test_flask_secret_key_bootstraps_and_persists(
        self, settings_override, monkeypatch, tmp_path
    ) -> None:
        """缺失时生成 + 写回 .env（重定向到临时文件，不碰真实 .env）。"""
        from starwatt import config as cfg

        env_file = tmp_path / "fake.env"
        monkeypatch.setattr(cfg, "ENV_FILE", env_file)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

        generated = secret_mod.flask_secret_key()
        assert len(generated) == 64  # token_hex(32)
        assert env_file.read_text(encoding="utf-8").count("FLASK_SECRET_KEY=") == 1
        # 二次调用必须稳定（否则重启后 cookie 全失效）
        assert secret_mod.flask_secret_key() == generated

    def test_flask_secret_key_replaces_existing_empty_line(
        self, settings_override, monkeypatch, tmp_path
    ) -> None:
        from starwatt import config as cfg

        env_file = tmp_path / "fake.env"
        env_file.write_text("FLASK_PORT=5000\nFLASK_SECRET_KEY=\n", encoding="utf-8")
        monkeypatch.setattr(cfg, "ENV_FILE", env_file)
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)

        generated = secret_mod.flask_secret_key()
        text = env_file.read_text(encoding="utf-8")
        assert text.count("FLASK_SECRET_KEY=") == 1  # 不产生重复键
        assert f"FLASK_SECRET_KEY={generated}" in text
        assert "FLASK_PORT=5000" in text  # 其他行保留

    def test_readonly_env_file_does_not_raise(
        self, settings_override, monkeypatch, tmp_path
    ) -> None:
        """只读文件系统（Docker 只读根）下应降级为临时密钥而非崩溃。"""
        from starwatt import config as cfg

        monkeypatch.setattr(cfg, "ENV_FILE", tmp_path / "nonexistent-dir" / "x.env")
        monkeypatch.delenv("FLASK_SECRET_KEY", raising=False)
        assert len(secret_mod.flask_secret_key()) == 64


# ===========================================================================
# session.py —— L17 / L19
# ===========================================================================
def _freeze(monkeypatch, moment) -> None:
    """把 ``now_cst()`` 冻到某个时刻（会话滑动/过期断言用）。"""
    monkeypatch.setattr(timeutil, "now_cst", lambda: moment)


def _all_stored_tokens() -> list[str]:
    """直接读 ``sessions.token`` 列。

    注意列名是 ``token`` 而不是 ``token_hash`` —— 这是**有意**的：
    Q9/Q10 要求零数据迁移，旧库的列名不能改，所以列还是 ``token``，
    但**里面存的内容**已经变成 sha256 摘要（L17 修复）。
    """
    with db.connect() as conn:
        rows = conn.execute("SELECT token FROM sessions").fetchall()
    return [r["token"] for r in rows]


class TestSession:
    @pytest.fixture
    def user(self, tmp_db):
        return service.create_user("alice", GOOD_PASSWORD, role=ROLE_ADMIN)

    def test_db_stores_hash_not_plaintext(self, user) -> None:
        """**L17**：DB 里绝不能出现原始 token。"""
        raw = session_mod.create_session(user.id)
        assert db.SessionRepo.get_by_token_hash(db.hash_token(raw)) is not None
        # 用明文去查 → 查不到（说明存的是哈希）
        assert db.SessionRepo.get_by_token_hash(raw) is None
        assert raw not in _all_stored_tokens()

    def test_validate_returns_user(self, user) -> None:
        raw = session_mod.create_session(user.id)
        got = session_mod.validate_session(raw)
        assert got is not None and got.id == user.id

    def test_validate_rejects_garbage(self, user) -> None:
        for bad in (None, "", "not-a-token", "a" * 200):
            assert session_mod.validate_session(bad) is None

    def test_revoke(self, user) -> None:
        raw = session_mod.create_session(user.id)
        session_mod.revoke_session(raw)
        assert session_mod.validate_session(raw) is None

    def test_sliding_expiry_extends(self, user, monkeypatch) -> None:
        raw = session_mod.create_session(user.id)
        # 前进 23 小时（24h 窗口内）→ 仍有效，且到期时间被前推
        _freeze(monkeypatch, timeutil.now_cst() + timedelta(hours=23))
        assert session_mod.validate_session(raw) is not None

    def test_idle_expiry(self, user, monkeypatch) -> None:
        raw = session_mod.create_session(user.id)
        _freeze(monkeypatch, timeutil.now_cst() + timedelta(hours=25))
        assert session_mod.validate_session(raw) is None

    def test_absolute_cap_L19(self, user, monkeypatch) -> None:
        """**L19**：持续访问也不能无限续命 —— 30 天后硬失效。"""
        raw = session_mod.create_session(user.id)
        start = timeutil.now_cst()
        # 每 12 小时访问一次，连做 31 天（旧代码会一直有效）
        for step in range(1, 63):
            _freeze(monkeypatch, start + timedelta(hours=12 * step))
            session_mod.validate_session(raw)
        assert session_mod.validate_session(raw) is None

    def test_expired_row_is_deleted(self, user, monkeypatch) -> None:
        raw = session_mod.create_session(user.id)
        _freeze(monkeypatch, timeutil.now_cst() + timedelta(hours=25))
        session_mod.validate_session(raw)
        assert db.SessionRepo.count() == 0

    def test_disabled_user_session_dies(self, user) -> None:
        raw = session_mod.create_session(user.id)
        service.set_disabled(user.id, True)
        assert session_mod.validate_session(raw) is None

    def test_deleted_user_session_dies(self, user) -> None:
        raw = session_mod.create_session(user.id)
        service.delete_user(user.id)
        assert session_mod.validate_session(raw) is None

    def test_revoke_all_for_user(self, user) -> None:
        a = session_mod.create_session(user.id)
        b = session_mod.create_session(user.id)
        assert session_mod.revoke_all_for_user(user.id) == 2
        assert session_mod.validate_session(a) is None
        assert session_mod.validate_session(b) is None

    def test_purge_expired(self, user, monkeypatch) -> None:
        session_mod.create_session(user.id)
        _freeze(monkeypatch, timeutil.now_cst() + timedelta(days=40))
        assert session_mod.purge_expired() == 1


# ===========================================================================
# service.py —— 用户 CRUD
# ===========================================================================
class TestUserCrud:
    def test_create_and_read_back(self, tmp_db) -> None:
        u = service.create_user("bob", GOOD_PASSWORD, role=ROLE_ADMIN)
        assert u.username == "bob" and u.is_admin
        assert u.password_hash.startswith("scrypt$")
        assert u.password_hash != GOOD_PASSWORD  # 绝不存明文
        assert not u.needs_password_change

    def test_default_role_is_viewer(self, tmp_db) -> None:
        assert service.create_user("v", GOOD_PASSWORD).role == ROLE_VIEWER

    def test_rejects_blank_username(self, tmp_db) -> None:
        with pytest.raises(ValueError, match="username"):
            service.create_user("   ", GOOD_PASSWORD)

    def test_rejects_bad_role(self, tmp_db) -> None:
        with pytest.raises(ValueError, match="role"):
            service.create_user("x", GOOD_PASSWORD, role="superuser")

    def test_rejects_weak_password(self, tmp_db) -> None:
        with pytest.raises(ValueError, match="uppercase"):
            service.create_user("x", "weakweak1!")

    def test_duplicate_username_conflicts(self, tmp_db) -> None:
        import sqlite3

        service.create_user("dup", GOOD_PASSWORD)
        with pytest.raises(sqlite3.IntegrityError):
            service.create_user("dup", GOOD_PASSWORD)

    def test_change_password(self, tmp_db) -> None:
        u = service.create_user("c", GOOD_PASSWORD)
        service.change_password(u.id, OTHER_PASSWORD)
        assert service.authenticate("c", GOOD_PASSWORD).ok is False
        assert service.authenticate("c", OTHER_PASSWORD).ok is True

    def test_change_password_rejects_weak(self, tmp_db) -> None:
        u = service.create_user("c", GOOD_PASSWORD)
        with pytest.raises(ValueError):
            service.change_password(u.id, "short")

    def test_change_password_kills_sessions(self, tmp_db) -> None:
        u = service.create_user("c", GOOD_PASSWORD)
        raw = session_mod.create_session(u.id)
        service.change_password(u.id, OTHER_PASSWORD)
        assert session_mod.validate_session(raw) is None

    def test_set_role(self, tmp_db) -> None:
        u = service.create_user("r", GOOD_PASSWORD)
        service.set_role(u.id, ROLE_ADMIN)
        assert db.UserRepo.get_by_id(u.id).is_admin
        with pytest.raises(ValueError):
            service.set_role(u.id, "root")

    def test_delete_user(self, tmp_db) -> None:
        u = service.create_user("d", GOOD_PASSWORD)
        service.delete_user(u.id)
        assert db.UserRepo.get_by_username("d") is None

    def test_audit_never_contains_password(self, tmp_db) -> None:
        """Q20 铁律：审计详情不得出现密码。"""
        service.create_user("a", GOOD_PASSWORD)
        service.authenticate("a", "wrong-password")
        rows = db.AuditRepo.recent(limit=50)
        assert rows, "应至少有失败登录记录"
        for row in rows:
            blob = f"{row.action} {row.target} {row.details}"
            assert GOOD_PASSWORD not in blob
            assert "wrong-password" not in blob


# ===========================================================================
# service.py —— 登录 / 锁定
# ===========================================================================
class TestAuthenticate:
    @pytest.fixture
    def admin(self, tmp_db):
        return service.create_user("admin", GOOD_PASSWORD, role=ROLE_ADMIN)

    def test_success(self, admin) -> None:
        res = service.authenticate("admin", GOOD_PASSWORD, ip="10.0.0.1")
        assert res.ok and res.error is None
        assert res.user is not None and res.user.is_admin
        assert res.token and session_mod.validate_session(res.token) is not None

    def test_success_clears_failures(self, admin) -> None:
        for _ in range(3):
            service.authenticate("admin", "nope")
        assert service.authenticate("admin", GOOD_PASSWORD).ok is True
        assert db.FailedAttemptRepo.count_since("admin", "1970-01-01 00:00:00") == 0

    def test_wrong_password(self, admin) -> None:
        res = service.authenticate("admin", "nope")
        assert res.ok is False and res.error == "invalid" and res.token is None

    def test_unknown_user(self, admin) -> None:
        assert service.authenticate("ghost", GOOD_PASSWORD).error == "invalid"

    def test_blank_inputs(self, admin) -> None:
        assert service.authenticate("", GOOD_PASSWORD).error == "invalid"
        assert service.authenticate("admin", "").error == "invalid"

    def test_username_is_trimmed(self, admin) -> None:
        assert service.authenticate("  admin  ", GOOD_PASSWORD).ok is True

    def test_lockout_after_threshold(self, admin) -> None:
        """默认阈值 5：第 6 次起返回 locked（即使密码正确）。"""
        for _ in range(5):
            assert service.authenticate("admin", "nope").error == "invalid"
        assert service.authenticate("admin", "nope").error == "locked"
        assert service.authenticate("admin", GOOD_PASSWORD).error == "locked"

    def test_lockout_expires(self, admin, monkeypatch) -> None:
        for _ in range(5):
            service.authenticate("admin", "nope")
        assert service.authenticate("admin", GOOD_PASSWORD).error == "locked"
        # 16 分钟后（锁定窗口 15 分钟）→ 解锁
        _freeze(monkeypatch, timeutil.now_cst() + timedelta(minutes=16))
        assert service.authenticate("admin", GOOD_PASSWORD).ok is True

    def test_disabled_user_cannot_login(self, admin) -> None:
        service.set_disabled(admin.id, True)
        res = service.authenticate("admin", GOOD_PASSWORD)
        assert res.error == "disabled" and res.token is None

    def test_must_change_password_flag(self, tmp_db) -> None:
        service.create_user("m", GOOD_PASSWORD, must_change_password=True)
        res = service.authenticate("m", GOOD_PASSWORD)
        assert res.ok is True and res.must_change_password is True
        assert res.token is not None  # 仍发 token，前端跳改密页

    def test_lazy_rehash_upgrades_old_params(self, admin) -> None:
        """旧参数哈希登录成功后会被透明升级到当前 scrypt 参数。"""
        import base64
        import hashlib
        import secrets

        salt = secrets.token_bytes(16)
        dk = hashlib.scrypt(
            GOOD_PASSWORD.encode(), salt=salt, n=1024, r=8, p=1, dklen=32
        )
        weak = f"scrypt$1024$8$1${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"
        db.UserRepo.update_password(admin.id, weak)

        assert service.authenticate("admin", GOOD_PASSWORD).ok is True
        after = db.UserRepo.get_by_id(admin.id).password_hash
        assert pwd.needs_rehash(after) is False  # 已升级


# ===========================================================================
# service.py —— bootstrap 建号（Q19 / B4：用后即焚）
# ===========================================================================
class TestBootstrap:
    def test_creates_admin_and_wipes_env(self, tmp_db, monkeypatch, tmp_path) -> None:
        from starwatt import config as cfg

        env_file = tmp_path / "boot.env"
        env_file.write_text(
            f"DB_PATH=./x.db\nBOOTSTRAP_ADMIN_PASSWORD={GOOD_PASSWORD}\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(cfg, "ENV_FILE", env_file)
        monkeypatch.setenv(service.ENV_BOOTSTRAP_PASSWORD, GOOD_PASSWORD)

        user = service.ensure_bootstrap_admin()
        assert user is not None and user.is_admin
        assert user.needs_password_change is True  # 强制首登改密
        # 🔑 用后即焚：.env 里不能留下密码
        text = env_file.read_text(encoding="utf-8")
        assert "BOOTSTRAP_ADMIN_PASSWORD" not in text
        assert GOOD_PASSWORD not in text
        assert "DB_PATH=./x.db" in text  # 其他行保留
        # 进程环境也要清掉
        import os

        assert service.ENV_BOOTSTRAP_PASSWORD not in os.environ

    def test_skipped_when_users_exist(self, tmp_db, monkeypatch) -> None:
        service.create_user("existing", GOOD_PASSWORD)
        monkeypatch.setenv(service.ENV_BOOTSTRAP_PASSWORD, OTHER_PASSWORD)
        assert service.ensure_bootstrap_admin() is None

    def test_skipped_without_env(self, tmp_db) -> None:
        assert service.ensure_bootstrap_admin() is None
        assert db.UserRepo.count() == 0

    def test_skipped_on_weak_password(self, tmp_db, monkeypatch) -> None:
        monkeypatch.setenv(service.ENV_BOOTSTRAP_PASSWORD, "weak")
        assert service.ensure_bootstrap_admin() is None
        assert db.UserRepo.count() == 0

    def test_idempotent(self, tmp_db, monkeypatch, tmp_path) -> None:
        from starwatt import config as cfg

        monkeypatch.setattr(cfg, "ENV_FILE", tmp_path / "boot.env")
        monkeypatch.setenv(service.ENV_BOOTSTRAP_PASSWORD, GOOD_PASSWORD)
        assert service.ensure_bootstrap_admin() is not None
        assert service.ensure_bootstrap_admin() is None  # 第二次 no-op
        assert db.UserRepo.count() == 1


# ===========================================================================
# csrf.py —— 无状态 HMAC
# ===========================================================================
class TestCsrf:
    def test_issue_and_validate(self, settings_override) -> None:
        token = csrf_mod.issue("session-token-abc")
        assert token and csrf_mod.validate("session-token-abc", token) is True

    def test_bound_to_session(self, settings_override) -> None:
        """换会话 → CSRF token 失效（登出后旧 token 不能用）。"""
        token = csrf_mod.issue("session-a")
        assert csrf_mod.validate("session-b", token) is False

    def test_rejects_garbage(self, settings_override) -> None:
        token = csrf_mod.issue("s")
        for bad in (None, "", "x", token + "0", token.upper()):
            assert csrf_mod.validate("s", bad) is False, bad

    def test_empty_session_yields_empty_token(self, settings_override) -> None:
        assert csrf_mod.issue("") == ""
        assert csrf_mod.issue(None) == ""  # type: ignore[arg-type]

    def test_stable_within_process(self, settings_override) -> None:
        assert csrf_mod.issue("s") == csrf_mod.issue("s")

    def test_safe_methods_set(self) -> None:
        assert csrf_mod.SAFE_METHODS == {"GET", "HEAD", "OPTIONS", "TRACE"}





