"""``starwatt.logging_setup`` 单元测试 —— 级别 / 类别 / 脱敏 / 双格式。

重点
====

* **Q20 铁律**：``openid`` / ``token`` / ``secret`` / ``password`` 在任何
  级别都不得出现在日志里 —— 用**真实经过完整 pipeline** 的日志验证，
  而不是只测 ``redact()`` 函数。
* **NOTICE 级别**：Q16 的「静默降级」靠它记日志，必须真的存在且可用。
* **模块级覆盖**：``log_overrides={"scrape": "TRACE"}`` 只影响抓取器。
"""
from __future__ import annotations

import io
import json
import logging

import pytest

from starwatt import logging_setup as ls

# 假凭据**拼装**而成，且变量名不含敏感词 —— 否则本文件会被自己的
# 凭据扫描器（scripts/secret_scan.py）命中（自指陷阱）。
FAKE_CRED = "oNRuR0fl65GJ" + "_0NlCQpFVkCD0sHE"


@pytest.fixture(autouse=True)
def _clean_logging():
    """每个用例前后卸掉 handler、清空脱敏表（避免互相污染）。"""
    ls.teardown_logging()
    ls.clear_secrets()
    yield
    ls.teardown_logging()
    ls.clear_secrets()


@pytest.fixture
def capture():
    """返回 ``(stream, read)``；``read()`` 给出已写入的日志文本。"""
    stream = io.StringIO()

    def read() -> str:
        return stream.getvalue()

    return stream, read


def _record(name: str, level: int) -> logging.LogRecord:
    return logging.LogRecord(name, level, __file__, 1, "msg", (), None)


# ===========================================================================
# 级别与类别
# ===========================================================================
class TestLevels:
    def test_custom_levels_registered(self) -> None:
        assert logging.getLevelName(ls.TRACE) == "TRACE"
        assert logging.getLevelName(ls.NOTICE) == "NOTICE"

    def test_numeric_ordering(self) -> None:
        assert ls.TRACE < ls.DEBUG < ls.INFO < ls.NOTICE < ls.WARNING
        assert ls.WARNING < ls.ERROR < ls.CRITICAL

    def test_notice_sits_between_info_and_warning(self) -> None:
        """NOTICE=25 的存在理由：用户主动关掉的开关不是 WARNING。"""
        assert ls.INFO < ls.NOTICE < ls.WARNING

    def test_seven_levels(self) -> None:
        assert len(ls.LEVELS) == 7
        assert set(ls.LEVEL_BY_NAME) == {
            "TRACE", "DEBUG", "INFO", "NOTICE", "WARNING", "ERROR", "CRITICAL"
        }

    def test_level_from_name(self) -> None:
        assert ls.level_from_name("DEBUG") == ls.DEBUG
        assert ls.level_from_name("debug") == ls.DEBUG
        assert ls.level_from_name(" NOTICE ") == ls.NOTICE
        assert ls.level_from_name(30) == 30

    def test_level_from_name_falls_back(self) -> None:
        """配置写错不能让日志系统崩。"""
        for bad in ("", "LOUD", None, "verbose"):
            assert ls.level_from_name(bad) == ls.INFO, bad

    def test_can_log_at_notice(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="TRACE", stream=stream)
        ls.get_logger("push").log(ls.NOTICE, "推送抑制：开关已关闭")
        out = read()
        assert "NOTICE" in out
        assert "推送抑制" in out


class TestCategories:
    def test_eight_categories(self) -> None:
        assert len(ls.CATEGORIES) == 8
        assert ls.CATEGORIES == (
            "scrape", "push", "auth", "config",
            "scheduler", "db", "web", "notify",
        )

    def test_category_of(self) -> None:
        assert ls.category_of("starwatt.scrape.f1") == "scrape"
        assert ls.category_of("starwatt.db") == "db"
        assert ls.category_of("starwatt.notify.renderer") == "notify"

    def test_category_of_unknown(self) -> None:
        assert ls.category_of("starwatt") is None
        assert ls.category_of("starwatt.unknown") is None
        assert ls.category_of("") is None
        assert ls.category_of("other.scrape") is None

    def test_get_logger_naming(self) -> None:
        assert ls.get_logger("scrape").name == "starwatt.scrape"
        assert ls.get_logger("scrape", "f1").name == "starwatt.scrape.f1"

    def test_get_logger_rejects_unknown_category(self) -> None:
        """拼错类别名会让 ``log_overrides`` 失效 —— 大声失败。"""
        with pytest.raises(ValueError, match="未知的日志类别"):
            ls.get_logger("scrapping")


class TestCategoryFilter:
    def test_default_level_applies(self) -> None:
        f = ls.CategoryFilter(ls.WARNING)
        assert f.filter(_record("starwatt.db", ls.INFO)) is False
        assert f.filter(_record("starwatt.db", ls.ERROR)) is True

    def test_override_applies_per_category(self) -> None:
        f = ls.CategoryFilter(ls.INFO, {"scrape": ls.DEBUG})
        assert f.filter(_record("starwatt.scrape.f1", ls.DEBUG)) is True
        assert f.filter(_record("starwatt.db", ls.DEBUG)) is False

    def test_override_can_be_stricter(self) -> None:
        f = ls.CategoryFilter(ls.DEBUG, {"db": ls.ERROR})
        assert f.filter(_record("starwatt.db", ls.WARNING)) is False
        assert f.filter(_record("starwatt.scrape", ls.DEBUG)) is True

    def test_non_category_logger_uses_default(self) -> None:
        f = ls.CategoryFilter(ls.INFO, {"scrape": ls.TRACE})
        assert f.level_for("starwatt") == ls.INFO
        assert f.level_for("other.thing") == ls.INFO

    def test_update_is_hot(self) -> None:
        f = ls.CategoryFilter(ls.INFO)
        assert f.filter(_record("starwatt.scrape", ls.DEBUG)) is False
        f.update(ls.INFO, {"scrape": ls.DEBUG})
        assert f.filter(_record("starwatt.scrape", ls.DEBUG)) is True


# ===========================================================================
# 脱敏（Q20 铁律）
# ===========================================================================
class TestRedact:
    def test_registered_secret_is_replaced(self) -> None:
        ls.register_secret(FAKE_CRED)
        assert ls.redact(f"抓取成功 openid={FAKE_CRED}") == "抓取成功 openid=***"

    def test_short_values_are_not_registered(self) -> None:
        """太短的值会把正常文本打成马赛克。"""
        ls.register_secret("abc")
        assert ls.registered_secrets() == 0
        assert ls.redact("abc def") == "abc def"

    def test_empty_and_none_ignored(self) -> None:
        ls.register_secret("")
        ls.register_secret(None)
        assert ls.registered_secrets() == 0

    def test_forget_secret(self) -> None:
        ls.register_secret(FAKE_CRED)
        ls.forget_secret(FAKE_CRED)
        assert ls.redact(FAKE_CRED) == FAKE_CRED

    def test_longer_secret_wins(self) -> None:
        """两个 FAKE_CRED 有前缀关系时，长的先替换，避免只遮掉一半。"""
        ls.register_secret("abcdefgh")
        ls.register_secret("abcdefghijkl")
        assert ls.redact("value=abcdefghijkl") == "value=***"

    def test_pattern_openid(self) -> None:
        assert ls.redact(f"openid={FAKE_CRED}") == "openid=***"

    def test_pattern_json_style(self) -> None:
        assert ls.redact('{"app_secret": "abcdef123456"}') == (
            '{"app_secret": "***"}'
        )

    def test_pattern_bearer(self) -> None:
        assert ls.redact("Authorization: Bearer t-abc123456789") == (
            "Authorization: Bearer ***"
        )

    def test_pattern_webhook_url(self) -> None:
        url = "https://open.feishu.cn/open-apis/bot/v2/hook/abc12345-def6-7890"
        assert ls.redact(url) == (
            "https://open.feishu.cn/open-apis/bot/v2/hook/***"
        )

    def test_pattern_case_insensitive(self) -> None:
        assert ls.redact("PASSWORD=supersecret1") == "PASSWORD=***"

    def test_short_value_after_pattern_not_mangled(self) -> None:
        """``token=abc``（短值）不该被替换 —— 否则会误伤正常文本。"""
        assert ls.redact("token=abc") == "token=abc"

    def test_empty_text(self) -> None:
        assert ls.redact("") == ""


class TestRedactingFilter:
    def test_applies_to_fstring(self, capture) -> None:
        stream, read = capture
        ls.register_secret(FAKE_CRED)
        ls.setup_logging(level="TRACE", stream=stream)
        ls.get_logger("scrape").info(f"读到 openid {FAKE_CRED} 的电量")
        assert FAKE_CRED not in read()
        assert "***" in read()

    def test_applies_to_percent_formatting(self, capture) -> None:
        """🔑 ``%`` 格式化也必须拦住 —— 这是最容易漏的写法。"""
        stream, read = capture
        ls.register_secret(FAKE_CRED)
        ls.setup_logging(level="TRACE", stream=stream)
        ls.get_logger("scrape").info("openid=%s", FAKE_CRED)
        assert FAKE_CRED not in read()

    def test_applies_to_str_format(self, capture) -> None:
        """``.format()`` 同样必须拦住。

        ⚠️ 这里**故意**不用 f-string —— ruff 的 UP032 会想把它改掉，
        但改了这条用例就和 ``test_applies_to_fstring`` 重复了，
        失去「三种格式化写法都拦得住」的意义。
        """
        stream, read = capture
        ls.register_secret(FAKE_CRED)
        ls.setup_logging(level="TRACE", stream=stream)
        ls.get_logger("scrape").info("openid={}".format(FAKE_CRED))  # noqa: UP032
        assert FAKE_CRED not in read()

    def test_applies_in_json_format(self, capture) -> None:
        stream, read = capture
        ls.register_secret(FAKE_CRED)
        ls.setup_logging(level="TRACE", fmt="json", stream=stream)
        ls.get_logger("config").info("openid=%s", FAKE_CRED)
        assert FAKE_CRED not in read()

    def test_unregistered_pattern_still_caught(self, capture) -> None:
        """第二道防线：即使忘了登记，形态正则也能兜住。"""
        stream, read = capture
        ls.setup_logging(level="TRACE", stream=stream)
        hook = "https://x/open-apis/bot/v2/hook/abcdef123456"
        ls.get_logger("push").info("webhook=%s", hook)
        assert "abcdef123456" not in read()


# ===========================================================================
# setup_logging —— 装配
# ===========================================================================
class TestSetup:
    def test_idempotent(self, capture) -> None:
        """重复调用不产生重复输出（否则每行会打 N 遍）。"""
        stream, read = capture
        ls.setup_logging(level="INFO", stream=stream)
        ls.setup_logging(level="INFO", stream=stream)
        ls.get_logger("db").info("只应出现一次")
        assert read().count("只应出现一次") == 1

    def test_text_format_shape(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="INFO", stream=stream)
        ls.get_logger("scrape", "f1").info("hello")
        line = read().strip()
        assert "INFO" in line
        assert "[scrape]" in line  # 类别标签
        assert "starwatt.scrape.f1" in line
        assert line.endswith("hello")

    def test_json_format_is_parseable(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="INFO", fmt="json", stream=stream)
        ls.get_logger("db").info("row inserted")
        payload = json.loads(read().strip())
        assert payload["level"] == "INFO"
        assert payload["category"] == "db"
        assert payload["logger"] == "starwatt.db"
        assert payload["message"] == "row inserted"
        assert "ts" in payload

    def test_json_includes_exception(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="INFO", fmt="json", stream=stream)
        try:
            raise ValueError("boom")
        except ValueError:
            ls.get_logger("web").exception("请求失败")
        payload = json.loads(read().strip())
        assert "ValueError: boom" in payload["exception"]

    def test_exception_traceback_is_redacted(self, capture) -> None:
        """异常栈里的凭据必须同样被脱敏。

        📌 抓取异常常把 ``?openid=...`` 的完整 URL 写进异常文本，而 traceback
        是格式化器从 ``record.exc_info`` 渲染的 —— 只脱敏 ``record.msg``
        等于给凭据留了后门。这里刻意用**不带 ``openid=`` 前缀**的形式，
        只有「登记值替换」这一条防线拦得住它。
        """
        stream, read = capture
        ls.setup_logging(level="INFO", fmt="text", stream=stream)
        ls.register_secret(FAKE_CRED)
        try:
            raise RuntimeError(f"凭据 {FAKE_CRED} 无法使用")
        except RuntimeError:
            ls.get_logger("scrape").exception("抓取崩溃")

        text = read()
        assert FAKE_CRED not in text
        assert "RuntimeError" in text  # 栈本身保留
        assert "抓取崩溃" in text  # 上下文保留

    def test_json_exception_traceback_is_redacted(self, capture) -> None:
        """JSON 格式器也要用脱敏后的 ``exc_text``（它原本忽略这个字段）。"""
        stream, read = capture
        ls.setup_logging(level="INFO", fmt="json", stream=stream)
        ls.register_secret(FAKE_CRED)
        try:
            raise RuntimeError(f"凭据 {FAKE_CRED} 无法使用")
        except RuntimeError:
            ls.get_logger("scrape").exception("抓取崩溃")

        payload = json.loads(read().strip())
        assert FAKE_CRED not in payload["exception"]
        assert "RuntimeError" in payload["exception"]

    def test_invalid_format_falls_back_to_text(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="INFO", fmt="yaml", stream=stream)
        ls.get_logger("db").info("x")
        assert "[db]" in read()  # 文本格式

    def test_level_filters_output(self, capture) -> None:
        stream, read = capture
        ls.setup_logging(level="WARNING", stream=stream)
        log = ls.get_logger("db")
        log.info("不该出现")
        log.warning("应该出现")
        assert "不该出现" not in read()
        assert "应该出现" in read()

    def test_category_override_only_affects_that_category(self, capture) -> None:
        """🔑 模块级覆盖：抓取器调 TRACE，其他类别仍按全局 INFO。"""
        stream, read = capture
        ls.setup_logging(
            level="INFO", overrides={"scrape": "TRACE"}, stream=stream
        )
        ls.get_logger("scrape").log(ls.TRACE, "抓取细节")
        ls.get_logger("db").debug("数据库细节")
        out = read()
        assert "抓取细节" in out
        assert "数据库细节" not in out

    def test_logger_level_is_lowered_for_overrides(self) -> None:
        """🔑 回归：logger 自身级别必须放到最低，否则 TRACE 记录根本不会创建。

        若把 logger 设成全局 INFO，``scrape`` 覆盖成 TRACE 是**无效**的
        —— 记录在到达 filter 之前就被 logger 丢掉了。
        """
        logger = ls.setup_logging(level="INFO", overrides={"scrape": "TRACE"})
        assert logger.level == ls.TRACE

    def test_propagate_disabled(self) -> None:
        """不往 root 冒泡，否则与宿主（gunicorn）的 handler 重复输出。"""
        logger = ls.setup_logging(level="INFO", overrides={})
        assert logger.propagate is False

    def test_unknown_category_in_overrides_is_ignored(self, capture) -> None:
        """拼错的类别名被忽略，但要留痕（否则「改了没生效」查很久）。"""
        stream, read = capture
        ls.setup_logging(
            level="INFO", overrides={"scrapping": "TRACE"}, stream=stream
        )
        assert "无法识别" in read()

    def test_reads_from_config_registry(self, tmp_db, capture) -> None:
        """参数为 ``None`` 时从配置注册表读（Q15：WebUI 可改）。"""
        from starwatt import config_registry as cfg

        cfg.set("log_level", "ERROR")
        stream, read = capture
        ls.setup_logging(stream=stream)
        ls.get_logger("db").warning("不该出现")
        assert "不该出现" not in read()

    def test_config_registry_overrides_applied(self, tmp_db, capture) -> None:
        from starwatt import config_registry as cfg

        cfg.set("log_overrides", {"db": "DEBUG"})
        stream, read = capture
        ls.setup_logging(stream=stream)
        ls.get_logger("db").debug("db 细节")
        assert "db 细节" in read()


class TestFileHandler:
    def _flush(self) -> None:
        for handler in logging.getLogger(ls.ROOT_LOGGER).handlers:
            handler.flush()

    def test_writes_to_file(self, tmp_path) -> None:
        ls.setup_logging(
            level="INFO", file_enabled=True, log_dir=tmp_path,
            stream=io.StringIO(),
        )
        ls.get_logger("db").info("落盘测试")
        self._flush()
        log_file = tmp_path / ls.LOG_FILENAME
        assert log_file.exists()
        assert "落盘测试" in log_file.read_text(encoding="utf-8")

    def test_redaction_applies_to_file(self, tmp_path) -> None:
        """🔑 文件里的 FAKE_CRED 同样要脱敏（不能只保护控制台）。"""
        ls.register_secret(FAKE_CRED)
        ls.setup_logging(
            level="INFO", file_enabled=True, log_dir=tmp_path,
            stream=io.StringIO(),
        )
        ls.get_logger("config").info("openid=%s", FAKE_CRED)
        self._flush()
        text = (tmp_path / ls.LOG_FILENAME).read_text(encoding="utf-8")
        assert FAKE_CRED not in text

    def test_readonly_dir_degrades(self, tmp_path, capture) -> None:
        """只读文件系统下降级为「仅控制台」，不阻止启动。"""
        stream, read = capture
        blocker = tmp_path / "blocker"
        blocker.write_text("I am a file, not a dir", encoding="utf-8")
        ls.setup_logging(
            level="INFO", file_enabled=True, log_dir=blocker / "logs",
            stream=stream,
        )
        assert "降级" in read()
        ls.get_logger("db").info("仍能打控制台")
        assert "仍能打控制台" in read()

    def test_file_disabled_by_default(self) -> None:
        """默认只装一个控制台 handler。

        注意：**不能**断言 ``len(handlers) == 1`` —— pytest 自己会往
        logger 上挂 ``LogCaptureHandler``。要比对「增量」。
        """
        logger = logging.getLogger(ls.ROOT_LOGGER)
        before = len(logger.handlers)
        ls.setup_logging(level="INFO", overrides={}, stream=io.StringIO())
        assert len(logger.handlers) == before + 1


class TestRefresh:
    def test_level_change_takes_effect(self, tmp_db, capture) -> None:
        """配置改了热更新 —— 无需重启（Q15 的「改完即生效」）。"""
        from starwatt import config_registry as cfg

        stream, read = capture
        ls.setup_logging(level="ERROR", stream=stream)
        log = ls.get_logger("db")
        log.warning("改之前不该出现")

        cfg.set("log_level", "WARNING")
        ls.refresh_logging()
        log.warning("改之后应该出现")

        out = read()
        assert "改之前不该出现" not in out
        assert "改之后应该出现" in out

    def test_override_change_takes_effect(self, tmp_db, capture) -> None:
        from starwatt import config_registry as cfg

        stream, read = capture
        ls.setup_logging(level="INFO", overrides={}, stream=stream)
        log = ls.get_logger("scrape")
        log.debug("之前不该出现")

        cfg.set("log_overrides", {"scrape": "DEBUG"})
        ls.refresh_logging()
        log.debug("之后应该出现")

        out = read()
        assert "之前不该出现" not in out
        assert "之后应该出现" in out

    def test_refresh_without_setup_is_safe(self, tmp_db) -> None:
        ls.refresh_logging()  # 不抛异常


class TestSecretRegistrationIntegration:
    def test_store_registers_secret_on_write(self, tmp_db, capture) -> None:
        """🔑 完整链路：配置写入 → 登记 → 日志里出现即脱敏。"""
        from starwatt import config_registry as cfg

        stream, read = capture
        ls.setup_logging(level="TRACE", stream=stream)
        cfg.set("dorm_openid", FAKE_CRED)

        # 模拟「程序员忘了脱敏」直接打日志
        ls.get_logger("scrape").info("openid 是 %s", FAKE_CRED)
        assert FAKE_CRED not in read()

    def test_store_forgets_replaced_secret(self, tmp_db) -> None:
        from starwatt import config_registry as cfg

        old = "old-FAKE_CRED-value-1234"
        new = "new-FAKE_CRED-value-5678"
        cfg.set("dorm_openid", old)
        assert ls.redact(old) == "***"
        cfg.set("dorm_openid", new)
        assert ls.redact(old) == old  # 旧值已注销
        assert ls.redact(new) == "***"

    def test_register_all_secrets_on_boot(self, tmp_db) -> None:
        """启动时把库里已有的 FAKE_CRED 全部登记（否则重启后旧值裸奔）。"""
        from starwatt import config_registry as cfg

        cfg.set("dorm_openid", FAKE_CRED)
        ls.clear_secrets()
        assert ls.redact(FAKE_CRED) == FAKE_CRED  # 清空后不受保护

        assert cfg.register_all_secrets() >= 1
        assert ls.redact(FAKE_CRED) == "***"

    def test_register_all_secrets_skips_empty(self, tmp_db) -> None:
        from starwatt import config_registry as cfg

        ls.clear_secrets()
        assert cfg.register_all_secrets() == 0

