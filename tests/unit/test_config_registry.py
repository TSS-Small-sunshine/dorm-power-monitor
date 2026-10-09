"""``starwatt.config_registry`` + ``starwatt.flags`` 单元测试。

三条主线
========

1. **覆盖性（RK13）** —— 旧库 36 个 ``meta`` 键**一个不漏**地进注册表。
   漏一个就意味着「所有配置都能在 WebUI 改」（Q15）做不到 100%。
2. **权限边界** —— 只有 ``CONFIG`` 能被配置 API 写；``STATE``/``CACHE``
   必须显式走 ``set_state``。
3. **L21 修复** —— 每个告警层都映射到真实开关，且总开关能压过分项开关。
"""
from __future__ import annotations

import pytest

from starwatt import flags
from starwatt.config_registry import (
    REGISTRY,
    Kind,
    SecretDecryptError,
    Setting,
    T,
    ValidationError,
    get,
    get_bool,
    get_int,
    get_str,
    is_encrypted,
    is_valid_rule,
    mask,
    registry,
    secrets,
    store,
    validate_value,
)

#: 旧库（develop 分支）实际使用的全部 meta 键。
#: 来源：``git show develop:db/_legacy.py`` 的 META_* 常量 +
#: 全仓 grep ``get_meta/set_meta`` 得到。
#: **不含** ``admin_password`` —— 它属于 :data:`REMOVED_KEYS`（L20）。
LEGACY_META_KEYS: frozenset[str] = frozenset({
    # 采集
    "dorm_base_url", "dorm_openid", "dorm_room_id", "room_label", "eqprice",
    "last_room_id", "last_scrape_at", "last_scrape_status",
    "last_daily_elect_at", "last_pay_at", "last_violation_check_at",
    "backfill_done",
    # 推送
    "feishu_webhook_url",
    "push_l1_enable", "push_l2_enable",
    "push_daily_enable", "push_weekly_enable", "push_monthly_enable",
    "push_daily_time", "push_weekly_time", "push_monthly_time",
    "push_receivers_l1", "push_receivers_l2",
    "push_receivers_report", "push_receivers_alert",
    "quiet_hours_start", "quiet_hours_end",
    "last_daily_report_date", "last_weekly_report_iso", "last_monthly_report_mo",
    "last_low_battery_alert_at", "last_violation_alert_at", "last_stale_alert_at",
    # 其它
    "api_internal_token", "oobe_step", "oobe_completed",
})


@pytest.fixture(autouse=True)
def _clear_cache():
    """每个用例前后清空配置缓存（避免跨用例污染）。"""
    store.invalidate()
    yield
    store.invalidate()


def _at(hour: int, minute: int = 0):
    """构造一个固定时刻（静默时段断言用）。"""
    from datetime import datetime

    return datetime(2026, 10, 6, hour, minute)


# ===========================================================================
# 覆盖性（RK13 的验收线）
# ===========================================================================
class TestRegistryCoverage:
    def test_every_legacy_key_is_declared(self) -> None:
        """🔑 旧库 36 个键**一个都不能漏**（Q9/Q10 零迁移的前提）。"""
        missing = sorted(LEGACY_META_KEYS - set(REGISTRY))
        assert missing == [], f"注册表漏了旧键：{missing}"

    def test_no_duplicate_keys(self) -> None:
        assert len(REGISTRY) == len(registry._ALL)

    def test_removed_key_is_not_declared(self) -> None:
        """``admin_password``（L20）必须**不在**注册表里。"""
        assert "admin_password" not in REGISTRY
        assert "admin_password" in registry.REMOVED_KEYS

    def test_all_keys_have_label_and_group(self) -> None:
        for key, setting in REGISTRY.items():
            assert setting.label, key
            assert setting.group in registry.GROUP_ORDER, key

    def test_every_group_is_non_empty(self) -> None:
        grouped = registry.by_group()
        for group in registry.GROUP_ORDER:
            assert grouped[group], f"分组 {group} 是空的"

    def test_depends_on_targets_exist(self) -> None:
        """``depends_on`` 必须指向真实存在的键（否则前端条件显示会失效）。"""
        for key, setting in REGISTRY.items():
            if setting.depends_on is not None:
                assert setting.depends_on in REGISTRY, key

    def test_depends_on_is_boolean(self) -> None:
        """``depends_on`` 只能指向布尔项 —— 否则「显示与否」无法判断。"""
        for key, setting in REGISTRY.items():
            if setting.depends_on is not None:
                target = REGISTRY[setting.depends_on]
                assert target.type is T.BOOL, key

    def test_validate_rules_are_wellformed(self) -> None:
        """每条规则串都要能被解析 —— 打错字会让校验静默失效。"""
        for key, setting in REGISTRY.items():
            assert is_valid_rule(setting.validate), (
                f"{key}: 无法识别的校验规则 {setting.validate!r}"
            )

    def test_defaults_pass_their_own_validation(self) -> None:
        """🔑 默认值必须通过自己的校验 —— 否则首次启动就报错。"""
        for key, setting in REGISTRY.items():
            try:
                validate_value(setting, setting.default)
            except ValidationError as exc:
                pytest.fail(f"{key} 的默认值不合法：{exc.message}")

    def test_schema_shape(self) -> None:
        out = registry.schema()
        assert out["total"] == len(REGISTRY)
        assert len(out["groups"]) == len(registry.GROUP_ORDER)
        first = out["groups"][0]["settings"][0]
        for field in ("key", "label", "type", "kind", "validate", "secret"):
            assert field in first

    def test_secret_defaults_are_never_exposed_in_schema(self) -> None:
        """schema 里 secret 项的默认值必须为 ``None``（避免前端拿到占位值）。"""
        for group in registry.schema()["groups"]:
            for item in group["settings"]:
                if item["secret"]:
                    assert item["default"] is None, item["key"]


# ===========================================================================
# types —— 校验引擎
# ===========================================================================
def _mk(type_: T, validate: str = "none", **kw) -> Setting:
    return Setting(
        key="k", label="L", group=registry.SITE, type=type_, default=None,
        validate=validate, **kw
    )


class TestValidateValue:
    def test_bool_rejects_string(self) -> None:
        with pytest.raises(ValidationError, match="true / false"):
            validate_value(_mk(T.BOOL), "yes")

    def test_bool_accepts_python_bool(self) -> None:
        validate_value(_mk(T.BOOL), True)
        validate_value(_mk(T.BOOL), False)

    def test_int_rejects_float(self) -> None:
        with pytest.raises(ValidationError, match="整数"):
            validate_value(_mk(T.INT), 1.5)

    def test_int_rejects_bool(self) -> None:
        """``bool`` 是 ``int`` 的子类 —— 必须显式排除，否则 ``True`` 会通过。"""
        with pytest.raises(ValidationError, match="数字"):
            validate_value(_mk(T.INT), True)

    def test_int_accepts_numeric_string(self) -> None:
        validate_value(_mk(T.INT), "42")  # 表单传字符串是常态

    def test_float_accepts_int(self) -> None:
        validate_value(_mk(T.FLOAT), 3)

    def test_time_format(self) -> None:
        validate_value(_mk(T.TIME), "09:00")
        validate_value(_mk(T.TIME), "23:59")
        validate_value(_mk(T.TIME), "00:00")
        for bad in ("9:00", "24:00", "23:60", "0900", "", "09:00:00", "09:0"):
            with pytest.raises(ValidationError, match="HH:MM"):
                validate_value(_mk(T.TIME), bad)

    def test_url(self) -> None:
        validate_value(_mk(T.URL), "https://a.example.com/x")
        for bad in ("ftp://a.com", "a.com", "", "javascript:alert(1)"):
            with pytest.raises(ValidationError):
                validate_value(_mk(T.URL), bad)

    def test_json(self) -> None:
        validate_value(_mk(T.JSON), {"a": 1})
        validate_value(_mk(T.JSON), "[1,2]")
        validate_value(_mk(T.JSON), '{"a": 1}')
        with pytest.raises(ValidationError, match="JSON"):
            validate_value(_mk(T.JSON), "{not json")

    def test_range_rule(self) -> None:
        setting = _mk(T.INT, "1..720")
        validate_value(setting, 1)
        validate_value(setting, 720)
        for bad in (0, 721, -1):
            with pytest.raises(ValidationError, match="1..720"):
                validate_value(setting, bad)

    def test_float_range_rule(self) -> None:
        setting = _mk(T.FLOAT, "0..100")
        validate_value(setting, 0)
        validate_value(setting, 99.5)
        with pytest.raises(ValidationError):
            validate_value(setting, 100.1)

    def test_len_rule(self) -> None:
        setting = _mk(T.STR, "len:1..4")
        validate_value(setting, "abc")
        for bad in ("", "abcde"):
            with pytest.raises(ValidationError, match="长度"):
                validate_value(setting, bad)

    def test_enum_rule(self) -> None:
        setting = _mk(T.STR, "enum:text|json")
        validate_value(setting, "text")
        with pytest.raises(ValidationError, match="text / json"):
            validate_value(setting, "yaml")

    def test_enum_choices_take_precedence(self) -> None:
        setting = _mk(T.ENUM, "none", choices=("INFO", "DEBUG"))
        validate_value(setting, "INFO")
        with pytest.raises(ValidationError, match="INFO / DEBUG"):
            validate_value(setting, "TRACE")

    def test_regex_rule(self) -> None:
        setting = _mk(T.STR, r"regex:^#[0-9a-fA-F]{6}$")
        validate_value(setting, "#1677ff")
        with pytest.raises(ValidationError, match="格式"):
            validate_value(setting, "1677ff")

    def test_error_carries_key(self) -> None:
        with pytest.raises(ValidationError) as info:
            validate_value(_mk(T.INT, "1..2"), 9)
        assert info.value.key == "k"
        assert "k" in str(info.value)


class TestIsValidRule:
    def test_accepts_all_supported_forms(self) -> None:
        for rule in ("none", "", "1..720", "0..100", "len:1..4", "enum:a|b",
                     r"regex:^x$", "url", "email", "json"):
            assert is_valid_rule(rule), rule

    def test_rejects_typos(self) -> None:
        for rule in ("1-720", "len:1", "enum:", "regex:", "ur", "1..", "..5"):
            assert not is_valid_rule(rule), rule


# ===========================================================================
# secrets —— AES-256-GCM
# ===========================================================================
class TestSecrets:
    def test_roundtrip(self, settings_override) -> None:
        cipher = secrets.encrypt("my-openid-value")
        assert secrets.is_encrypted(cipher)
        assert secrets.decrypt(cipher) == "my-openid-value"

    def test_ciphertext_differs_each_time(self, settings_override) -> None:
        """GCM 每次用新 nonce —— 相同明文不得产生相同密文。"""
        a = secrets.encrypt("same")
        b = secrets.encrypt("same")
        assert a != b
        assert secrets.decrypt(a) == secrets.decrypt(b) == "same"

    def test_prefix_is_present(self, settings_override) -> None:
        assert secrets.encrypt("x").startswith(secrets.PREFIX)

    def test_empty_is_not_encrypted(self, settings_override) -> None:
        assert secrets.encrypt("") == ""
        assert secrets.decrypt("") == ""
        assert secrets.decrypt(None) == ""

    def test_plaintext_passthrough(self, settings_override) -> None:
        """迁移期兼容：旧库里的明文 secret 原样返回（下次写入自动升级）。"""
        assert secrets.is_encrypted("oNRuR0fl") is False
        assert secrets.decrypt("oNRuR0fl") == "oNRuR0fl"

    def test_tampered_ciphertext_is_rejected(self, settings_override) -> None:
        """GCM 认证加密：改一个字节就解密失败（而不是吐出垃圾）。"""
        import base64

        cipher = secrets.encrypt("secret-value")
        body = cipher[len(secrets.PREFIX) :]
        nonce, tag, ct = body.split(":")
        raw = bytearray(base64.b64decode(ct))
        raw[0] ^= 0x01  # 翻转密文第一个字节
        tampered = (
            f"{secrets.PREFIX}{nonce}:{tag}:"
            f"{base64.b64encode(bytes(raw)).decode()}"
        )
        assert tampered != cipher
        with pytest.raises(SecretDecryptError):
            secrets.decrypt(tampered)

    def test_tampered_tag_is_rejected(self, settings_override) -> None:
        """改 tag 同样必须失败（完整性保护覆盖认证标签）。"""
        import base64

        cipher = secrets.encrypt("secret-value")
        nonce, tag, ct = cipher[len(secrets.PREFIX) :].split(":")
        raw = bytearray(base64.b64decode(tag))
        raw[0] ^= 0xFF
        tampered = (
            f"{secrets.PREFIX}{nonce}:"
            f"{base64.b64encode(bytes(raw)).decode()}:{ct}"
        )
        with pytest.raises(SecretDecryptError):
            secrets.decrypt(tampered)

    def test_wrong_key_raises_clear_error(self, settings_override, monkeypatch) -> None:
        """RK14：密钥换了必须**明确报错**，不能静默返回空串。"""
        cipher = secrets.encrypt("secret-value")
        monkeypatch.setenv("FLASK_SECRET_KEY", "a-completely-different-key-material")
        with pytest.raises(SecretDecryptError, match="FLASK_SECRET_KEY"):
            secrets.decrypt(cipher)

    def test_malformed_ciphertext(self, settings_override) -> None:
        for bad in ("enc:v1:onlyonepart", "enc:v1:a:b:c:d",
                    "enc:v1:!!!:!!!:!!!"):
            with pytest.raises(SecretDecryptError):
                secrets.decrypt(bad)

    def test_encrypt_rejects_non_string(self, settings_override) -> None:
        with pytest.raises(ValueError):
            secrets.encrypt(123)  # type: ignore[arg-type]


class TestMask:
    def test_masks_all_but_last_four(self) -> None:
        assert mask("oNRuR0fl65GJ_0NlCQpFVkCD0sHE") == "••••0sHE"

    def test_short_values_fully_masked(self) -> None:
        """🔑 短值必须**全部**打码 —— 露出 4/5 个字符等于没脱敏。"""
        assert mask("abcd") == "••••"
        assert mask("ab") == "••"
        assert mask("abcde") == "•••••"
        assert mask("abcdefg") == "•••••••"

    def test_threshold_boundary(self) -> None:
        assert mask("abcdefgh") == "••••efgh"  # 刚好 8 位 → 按规则露末 4
        assert mask("abcdefg") == "•••••••"  # 7 位 → 全打码

    def test_realistic_secret_lengths(self) -> None:
        """真实的 openid / token 远长于阈值 —— 行为与 Q15 描述一致。"""
        assert mask("oNRuR0fl65GJ_0NlCQpFVkCD0sHE") == "••••0sHE"
        assert mask("a" * 32) == "••••aaaa"

    def test_empty(self) -> None:
        assert mask("") == ""
        assert mask(None) == ""


# ===========================================================================
# store —— 读写 / 类型 / 权限 / 缓存
# ===========================================================================
class TestStoreRead:
    def test_falls_back_to_registry_default(self, tmp_db) -> None:
        """``meta`` 表空着也能读到值（RK15：升级新增项零迁移）。"""
        assert get_int("scrape_interval_sec") == 600
        assert get_bool("push_group_enabled") is True
        assert get_str("push_daily_time") == "09:00"
        assert get("threshold_red") == 30

    def test_unregistered_key_raises(self, tmp_db) -> None:
        with pytest.raises(KeyError):
            get("no_such_key")
        assert get("no_such_key", "fallback") == "fallback"

    def test_typed_accessors(self, tmp_db) -> None:
        store.set("scrape_interval_sec", 300)
        assert get_int("scrape_interval_sec") == 300
        store.set("threshold_red", 25.5)
        assert get("threshold_red") == 25.5
        store.set("room_label", "3 号楼 412")
        assert get_str("room_label") == "3 号楼 412"

    def test_json_roundtrip(self, tmp_db) -> None:
        store.set("log_overrides", {"scrape": "DEBUG"})
        assert store.get_json("log_overrides") == {"scrape": "DEBUG"}
        # 库里存的是 JSON 文本
        assert store.raw("log_overrides") == '{"scrape": "DEBUG"}'

    def test_bool_encoding(self, tmp_db) -> None:
        store.set("push_group_enabled", False)
        assert store.raw("push_group_enabled") == "0"
        store.set("push_group_enabled", True)
        assert store.raw("push_group_enabled") == "1"

    def test_corrupt_value_falls_back(self, tmp_db) -> None:
        """库里存了垃圾 → 回落默认值，而不是抛异常。"""
        from starwatt.db.repositories import MetaRepo

        MetaRepo.set("scrape_interval_sec", "not-a-number")
        store.invalidate()
        assert get_int("scrape_interval_sec", 600) == 600


class TestStoreWrite:
    def test_set_rejects_invalid(self, tmp_db) -> None:
        with pytest.raises(ValidationError, match="60..86400"):
            store.set("scrape_interval_sec", 5)

    def test_set_rejects_unknown_key(self, tmp_db) -> None:
        with pytest.raises(KeyError):
            store.set("nope", 1)

    def test_state_cannot_be_written_via_config_api(self, tmp_db) -> None:
        """🔑 权限边界：用户不能改 ``last_scrape_at``，否则 stale 检测失效。"""
        with pytest.raises(PermissionError, match="state"):
            store.set("last_scrape_at", "2026-01-01 00:00:00")

    def test_set_state_allows_state_writes(self, tmp_db) -> None:
        store.set_state("last_scrape_at", "2026-10-06 12:00:00")
        assert get_str("last_scrape_at") == "2026-10-06 12:00:00"

    def test_secret_is_encrypted_at_rest(self, tmp_db) -> None:
        """🔑 库里存的必须是密文（Q15/Q17）。"""
        store.set("dorm_openid", "oNRuR0fl65GJ_0NlCQpFVkCD0sHE")
        raw = store.raw("dorm_openid")
        assert raw is not None
        assert is_encrypted(raw)
        assert "oNRuR0fl" not in raw  # 明文不得出现在库里
        # 读出来仍是明文
        assert get_str("dorm_openid") == "oNRuR0fl65GJ_0NlCQpFVkCD0sHE"

    def test_secret_migration_from_plaintext(self, tmp_db) -> None:
        """旧库里的明文 secret 能被读到；重写一次即升级为密文。"""
        from starwatt.db.repositories import MetaRepo

        MetaRepo.set("dorm_openid", "legacy-plaintext-openid")
        store.invalidate()
        assert get_str("dorm_openid") == "legacy-plaintext-openid"

        store.set("dorm_openid", "legacy-plaintext-openid")
        assert is_encrypted(store.raw("dorm_openid"))


class TestSetMany:
    def test_all_valid(self, tmp_db) -> None:
        errors = store.set_many({"room_label": "412", "eqprice": 0.55})
        assert errors == {}
        assert get_str("room_label") == "412"
        assert get("eqprice") == 0.55

    def test_partial_failure_keeps_valid_changes(self, tmp_db) -> None:
        """🔑 一项填错不该丢掉其它正确改动（用户在配置页一次改 5 项）。"""
        errors = store.set_many({
            "room_label": "412",
            "scrape_interval_sec": 5,  # 非法
            "eqprice": 0.55,
        })
        assert set(errors) == {"scrape_interval_sec"}
        assert get_str("room_label") == "412"  # 有效项已落库
        assert get("eqprice") == 0.55

    def test_unknown_key_reported(self, tmp_db) -> None:
        assert store.set_many({"ghost": 1}) == {"ghost": "未注册的配置项"}

    def test_state_key_reported(self, tmp_db) -> None:
        errors = store.set_many({"last_scrape_at": "x"})
        assert "state" in errors["last_scrape_at"]

    def test_error_message_is_chinese(self, tmp_db) -> None:
        errors = store.set_many({"push_daily_time": "25:00"})
        assert "HH:MM" in errors["push_daily_time"]


class TestCache:
    def test_second_read_hits_cache(self, tmp_db, monkeypatch) -> None:
        from starwatt.db.repositories import MetaRepo

        store.set("room_label", "cached")
        calls = []
        original = MetaRepo.get

        def spy(key):
            calls.append(key)
            return original(key)

        monkeypatch.setattr(MetaRepo, "get", staticmethod(spy))
        store.invalidate()
        assert get_str("room_label") == "cached"
        assert get_str("room_label") == "cached"
        assert calls.count("room_label") == 1  # 第二次走缓存

    def test_write_invalidates(self, tmp_db) -> None:
        assert get_str("room_label") == ""
        store.set("room_label", "new")
        assert get_str("room_label") == "new"


class TestBulkViews:
    def test_all_values_masks_secrets_by_default(self, tmp_db) -> None:
        """🔑 默认脱敏 —— 调用方忘了脱敏也不会泄露。"""
        store.set("dorm_openid", "oNRuR0fl65GJ_0NlCQpFVkCD0sHE")
        values = store.all_values()
        assert values["dorm_openid"] == "••••0sHE"

    def test_all_values_can_include_secrets(self, tmp_db) -> None:
        store.set("dorm_openid", "oNRuR0fl65GJ_0NlCQpFVkCD0sHE")
        values = store.all_values(include_secrets=True)
        assert values["dorm_openid"] == "oNRuR0fl65GJ_0NlCQpFVkCD0sHE"

    def test_all_values_only_config_by_default(self, tmp_db) -> None:
        values = store.all_values()
        for key in values:
            assert REGISTRY[key].kind is Kind.CONFIG

    def test_all_values_empty_secret_is_empty_string(self, tmp_db) -> None:
        assert store.all_values()["dorm_openid"] == ""

    def test_ensure_defaults_is_idempotent_and_non_destructive(self, tmp_db) -> None:
        store.set("room_label", "我改过的")
        written = store.ensure_defaults()
        assert written > 0
        assert store.ensure_defaults() == 0  # 第二次 no-op
        assert get_str("room_label") == "我改过的"  # 不覆盖用户值

    def test_ensure_defaults_writes_only_config(self, tmp_db) -> None:
        """``ensure_defaults`` 只写 ``CONFIG`` 项。

        注意：库里本来就有 ``STATE`` 键（``schema_version`` 由迁移框架写入），
        所以要比对**新增**的键，而不是全部键。
        """
        from starwatt.db.repositories import MetaRepo

        before = set(MetaRepo.all())
        store.ensure_defaults()
        added = set(MetaRepo.all()) - before
        assert added, "应写入默认值"
        for key in added:
            assert REGISTRY[key].kind is Kind.CONFIG, key

    def test_every_meta_key_in_db_is_registered(self, tmp_db) -> None:
        """🔑 **RK13 的核心不变量**：库里出现的每个 meta 键都必须在注册表里。

        这条测试在 M1 抓到了 ``schema_version`` —— 它由迁移框架直接写 meta，
        却没登记。任何遍历 meta 的代码（``REGISTRY[key]``）都会 KeyError。
        「代码写的键没登记」是「所有配置可改」最容易漏的一类。
        """
        from starwatt.db.repositories import MetaRepo

        store.ensure_defaults()
        store.set_state("last_scrape_at", "2026-10-06 12:00:00")
        unknown = sorted(set(MetaRepo.all()) - set(REGISTRY))
        assert unknown == [], f"库里有未登记的 meta 键：{unknown}"

    def test_purge_removed_deletes_admin_password(self, tmp_db) -> None:
        """🔑 L20：旧库里的明文管理员密码必须被清掉（Q21）。"""
        from starwatt.db.repositories import MetaRepo

        MetaRepo.set("admin_password", "old-plaintext-password")
        store.invalidate()
        assert store.purge_removed() == 1
        assert MetaRepo.get("admin_password") is None

    def test_purge_removed_is_noop_when_absent(self, tmp_db) -> None:
        assert store.purge_removed() == 0


# ===========================================================================
# flags —— Q16 / L21
# ===========================================================================
class TestFlagRegistry:
    def test_exactly_18_flags(self) -> None:
        """设计文档 §2.9 定的是 17 个（2 总开关 + 8 告警层 + 7 命令）。

        +1 = ``qq_bot_enabled``（Q18 扩展：新增 QQ 官方机器人渠道时加的总开关）。
        """
        assert len(flags.FLAGS) == 18

    def test_every_layer_maps_to_a_real_flag(self) -> None:
        """🔑 **L21 的核心修复**：不允许存在「没有开关的告警层」。"""
        for layer, key in flags.LAYER_FLAGS.items():
            assert key in flags.FLAGS, f"层 {layer} 映射到不存在的开关 {key}"

    def test_all_eight_push_layers_are_covered(self) -> None:
        """旧代码里 5 条推送路径绕过开关；重写后 8 个层都有开关。"""
        assert set(flags.LAYER_FLAGS) == {
            "l1", "l2", "daily", "weekly", "monthly",
            "offline", "violation", "stale",
        }

    def test_every_command_maps_to_a_real_flag(self) -> None:
        for command, key in flags.COMMAND_FLAGS.items():
            assert key in flags.FLAGS, f"命令 {command} 映射到不存在的开关 {key}"

    def test_flag_labels_are_chinese(self) -> None:
        for key, flag in flags.FLAGS.items():
            assert flag.label, key
            assert any("\u4e00" <= ch <= "\u9fff" for ch in flag.label), key

    def test_parents_exist_and_are_masters(self) -> None:
        for key, flag in flags.FLAGS.items():
            if flag.parent is not None:
                assert flag.parent in flags.FLAGS, key
                assert flags.FLAGS[flag.parent].parent is None, key

    def test_unknown_flag_raises_loudly(self) -> None:
        """拼错开关名是编程错误 —— 大声失败，别静默不告警。"""
        with pytest.raises(KeyError, match="未注册的开关"):
            flags.is_enabled("push_l1_enabled")  # 多了个 d

    def test_unknown_layer_and_command_raise(self) -> None:
        with pytest.raises(KeyError, match="告警层"):
            flags.layer_enabled("l9")
        with pytest.raises(KeyError, match="命令"):
            flags.command_enabled("reboot")


class TestFlagSemantics:
    def test_defaults_are_enabled_except_bots(self, tmp_db) -> None:
        """所有开关默认开启，**唯独两个机器人总开关默认关闭**。

        为什么默认关：机器人需要 App ID / Secret / Token 才能工作，
        新装用户还没填。默认开会让回调端点一直报「未配置」。

        它们下面的 7 个命令开关也因此默认不生效 —— 这正是**总开关优先**的体现。
        """
        assert store.get_bool(flags.GROUP_MASTER) is True
        assert store.get_bool(flags.BOT_MASTER) is False
        assert store.get_bool(flags.QQ_MASTER) is False
        off_masters = {flags.BOT_MASTER, flags.QQ_MASTER}
        for key, flag in flags.FLAGS.items():
            # 关闭的只有：两个机器人总开关自身 + 机器人总开关下面的 7 个命令
            off = key in off_masters or flag.parent in off_masters
            assert flags.is_enabled(key) is not off, key

    def test_bot_commands_work_once_master_is_on(self, tmp_db) -> None:
        store.set(flags.BOT_MASTER, True)
        for command in ("remain", "meter", "today", "history", "pay",
                        "violations", "help"):
            assert flags.command_enabled(command) is True, command

    def test_master_switch_overrides_children(self, tmp_db) -> None:
        """🔑 总开关关闭 → 所有分项一律失效（用户不必逐个关 8 个层）。"""
        store.set(flags.GROUP_MASTER, False)
        for layer in ("l1", "l2", "daily", "weekly", "monthly",
                      "offline", "violation", "stale"):
            assert flags.layer_enabled(layer) is False, layer

    def test_master_off_does_not_affect_bot_commands(self, tmp_db) -> None:
        """群推送关掉不应影响私聊命令 —— 两者是独立的总开关。"""
        store.set(flags.BOT_MASTER, True)
        store.set(flags.GROUP_MASTER, False)
        assert flags.command_enabled("help") is True

    def test_child_off_only_affects_itself(self, tmp_db) -> None:
        store.set("push_l1_enable", False)
        assert flags.layer_enabled("l1") is False
        assert flags.layer_enabled("l2") is True

    def test_bot_master_overrides_commands(self, tmp_db) -> None:
        store.set(flags.BOT_MASTER, True)
        assert flags.command_enabled("help") is True
        store.set(flags.BOT_MASTER, False)
        for command in ("remain", "meter", "today", "history", "pay",
                        "violations", "help"):
            assert flags.command_enabled(command) is False, command

    def test_status_and_remain_share_a_switch(self, tmp_db) -> None:
        store.set("cmd_remain_enabled", False)
        assert flags.command_enabled("remain") is False
        assert flags.command_enabled("status") is False

    def test_enabled_flags_list(self, tmp_db) -> None:
        store.set("push_l1_enable", False)
        listed = flags.enabled_flags()
        assert "push_l1_enable" not in listed
        assert "push_l2_enable" in listed


class TestQuietHours:
    def test_cross_midnight_inside(self, tmp_db) -> None:
        """23:00–07:00 跨午夜：02:00 在区间内。"""
        assert flags.in_quiet_hours(_at(2, 0)) is True

    def test_cross_midnight_outside(self, tmp_db) -> None:
        assert flags.in_quiet_hours(_at(12, 0)) is False

    def test_cross_midnight_boundaries(self, tmp_db) -> None:
        assert flags.in_quiet_hours(_at(23, 0)) is True  # 含起点
        assert flags.in_quiet_hours(_at(7, 0)) is False  # 不含终点
        assert flags.in_quiet_hours(_at(6, 59)) is True

    def test_same_day_range(self, tmp_db) -> None:
        store.set("quiet_hours_start", "01:00")
        store.set("quiet_hours_end", "05:00")
        assert flags.in_quiet_hours(_at(3, 0)) is True
        assert flags.in_quiet_hours(_at(6, 0)) is False

    def test_equal_start_end_means_disabled(self, tmp_db) -> None:
        """用户把起止填成同一值 = 想关掉静默，不是想整天静默。"""
        store.set("quiet_hours_start", "00:00")
        store.set("quiet_hours_end", "00:00")
        assert flags.in_quiet_hours(_at(12, 0)) is False

    def test_malformed_config_falls_back(self, tmp_db) -> None:
        """配置写坏不能让推送系统崩 —— 回落 23:00–07:00。

        配置 API 自己会挡住非法值，所以这里**直接写库**来模拟
        「旧库里存着坏数据」这一迁移场景（Q9/Q10 的零迁移前提）。
        """
        from starwatt.db.repositories import MetaRepo

        MetaRepo.set("quiet_hours_start", "banana")
        MetaRepo.set("quiet_hours_end", "also-bad")
        store.invalidate()
        assert flags.in_quiet_hours(_at(2, 0)) is True
        assert flags.in_quiet_hours(_at(12, 0)) is False

    def test_uses_frozen_now_by_default(self, tmp_db, frozen_now) -> None:
        """不传 moment 时走 ``timeutil.now_cst()`` —— 可被 frozen_now 冻结。"""
        assert flags.in_quiet_hours() is False  # FIXED_NOW = 12:00


class TestShouldPush:
    def test_allows_by_default(self, tmp_db) -> None:
        # 关掉静默时段（start == end = 不静默）—— 否则本用例会随真实时钟
        # 在 23:00–07:00 之间变红（CI 跑在哪个时区都该是绿的）
        store.set_many({"quiet_hours_start": "00:00", "quiet_hours_end": "00:00"})
        assert flags.should_push("l1") is True
        assert flags.why_suppressed("l1") is None

    def test_master_off_reports_reason(self, tmp_db) -> None:
        store.set(flags.GROUP_MASTER, False)
        assert flags.should_push("l1") is False
        assert "总开关" in (flags.why_suppressed("l1") or "")

    def test_layer_off_reports_reason(self, tmp_db) -> None:
        store.set("push_violation_enable", False)
        assert flags.should_push("violation") is False
        assert "违规" in (flags.why_suppressed("violation") or "")

    def test_quiet_hours_suppresses_only_l1_l2(self, tmp_db, monkeypatch) -> None:
        """🔑 静默时段只压 L1/L2；日报/周报是用户主动订阅的，照发。"""
        # 统一 patch 时间源（flags 走 ``timeutil.now_cst()`` 模块属性，
        # 这样 conftest 的 frozen_now 与这里的 patch 都能生效）
        monkeypatch.setattr("starwatt.timeutil.now_cst", lambda: _at(2, 0))
        assert flags.should_push("l1") is False
        assert flags.should_push("l2") is False
        assert flags.should_push("daily") is True
        assert flags.should_push("weekly") is True
        assert flags.should_push("monthly") is True

    def test_quiet_hours_reason_mentions_range(self, tmp_db, monkeypatch) -> None:
        monkeypatch.setattr("starwatt.timeutil.now_cst", lambda: _at(2, 0))
        reason = flags.why_suppressed("l1") or ""
        assert "静默时段" in reason and "23:00" in reason


class TestFlagCallSites:
    """源码扫描 —— 拦住「拼错开关名」这类只有上线才发现的 bug。"""

    def test_all_literal_flag_names_in_source_are_registered(self) -> None:
        import ast
        from pathlib import Path

        pkg = Path(flags.__file__).resolve().parent
        offenders: list[str] = []
        for path in pkg.rglob("*.py"):
            if path.name == "flags.py":
                continue  # 定义处本身
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name)
                    else ""
                )
                if name not in ("is_enabled", "layer_enabled", "command_enabled"):
                    continue
                pool: dict[str, object] = (
                    flags.LAYER_FLAGS if name == "layer_enabled"
                    else flags.COMMAND_FLAGS if name == "command_enabled"
                    else flags.FLAGS
                )
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value not in pool:
                            offenders.append(
                                f"{path.name}:{node.lineno} {name}({arg.value!r})"
                            )
        assert offenders == [], f"源码里出现未注册的开关名：{offenders}"


