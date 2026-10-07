"""日志子系统（Q20）—— 7 个级别 + 8 个业务类别 + 模块级覆盖 + 文本/JSON 双格式。

7 个级别
========

``TRACE(5)`` 与 ``NOTICE(25)`` 是**自定义**级别，插在标准 5 级之间：

=====  =====  =========================================================
级别   数值   用途
=====  =====  =========================================================
TRACE  5      逐字段的抓取细节（只在排查时开）
DEBUG  10     请求/响应骨架
INFO   20     正常业务节点
NOTICE 25     **用户主动造成的「什么都没做」** —— 开关关闭、静默时段、
              推送抑制。不是异常，不该刷 WARNING（Q16 的静默降级）
WARNING 30    可恢复的异常（重试、降级到 stale）
ERROR  40     本次操作失败
CRITICAL 50   服务不可用
=====  =====  =========================================================

8 个业务类别
============

``scrape`` / ``push`` / ``auth`` / ``config`` / ``scheduler`` / ``db`` /
``web`` / ``notify`` —— 与 ``starwatt.<类别>.*`` 的 logger 名一一对应，
所以 ``log_overrides = {"scrape": "DEBUG"}`` 能精确把抓取器调成 DEBUG
而不让整站刷屏。

铁律：secret 不得出现在日志里
=============================

三道防线：

1. **显式登记**（最强）—— 配置项被写入时把值交给 :func:`register_secret`，
   :class:`RedactingFilter` 在**每个** handler 上做子串替换
2. **模式匹配** —— ``openid=xxx`` / ``Bearer xxx`` / webhook URL 等常见形态
3. **AST 守卫 + 单元测试** —— 见 ``tests/unit/test_logging_setup.py``

第 1 道是「即使程序员写错了也不会泄露」的兜底：脱敏发生在**格式化之后**，
所以 f-string / ``%`` / ``.format()`` 哪种写法都拦得住。

与审计日志分离
==============

运行日志（本模块）**可轮转、可降级**；``audit_log`` 表**不可篡改、不轮转**。
两者用途不同：运行日志给运维看，审计日志给追责看。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import re
import sys
import threading
from datetime import datetime
from pathlib import Path

__all__ = [
    "CATEGORIES",
    "CRITICAL",
    "DEBUG",
    "ERROR",
    "INFO",
    "LEVELS",
    "LEVEL_BY_NAME",
    "LOG_FILENAME",
    "MASK",
    "NOTICE",
    "ROOT_LOGGER",
    "TRACE",
    "WARNING",
    "CategoryFilter",
    "JsonFormatter",
    "RedactingFilter",
    "TextFormatter",
    "category_of",
    "clear_secrets",
    "forget_secret",
    "get_logger",
    "level_from_name",
    "redact",
    "refresh_logging",
    "register_secret",
    "registered_secrets",
    "setup_logging",
    "teardown_logging",
]

# ---------------------------------------------------------------------------
# 自定义级别
# ---------------------------------------------------------------------------
TRACE = 5
DEBUG = logging.DEBUG  # 10
INFO = logging.INFO  # 20
NOTICE = 25
WARNING = logging.WARNING  # 30
ERROR = logging.ERROR  # 40
CRITICAL = logging.CRITICAL  # 50

logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(NOTICE, "NOTICE")

#: 可选的 7 个级别（UI 下拉用，顺序从详细到严重）
LEVELS: tuple[int, ...] = (TRACE, DEBUG, INFO, NOTICE, WARNING, ERROR, CRITICAL)

#: ``名字 -> 数值``（配置里存的是名字）
LEVEL_BY_NAME: dict[str, int] = {
    logging.getLevelName(level): level for level in LEVELS
}

#: 8 个业务类别 —— 与 ``starwatt.<类别>.*`` 的 logger 名对应
CATEGORIES: tuple[str, ...] = (
    "scrape",
    "push",
    "auth",
    "config",
    "scheduler",
    "db",
    "web",
    "notify",
)

#: 本项目所有 logger 的公共前缀
ROOT_LOGGER = "starwatt"


def level_from_name(name: str, default: int = INFO) -> int:
    """``"DEBUG"`` → ``10``；未知名字回落 ``default``（配置写错不崩）。"""
    if isinstance(name, int):
        return name
    return LEVEL_BY_NAME.get((name or "").strip().upper(), default)


def category_of(logger_name: str) -> str | None:
    """从 logger 名推出业务类别。

    ``starwatt.scrape.f1`` → ``"scrape"``；``starwatt.db`` → ``"db"``；
    不在 8 个类别里的（例如 ``starwatt`` 本身）→ ``None``。
    """
    parts = (logger_name or "").split(".")
    if len(parts) >= 2 and parts[0] == ROOT_LOGGER and parts[1] in CATEGORIES:
        return parts[1]
    return None


def get_logger(category: str, sub: str | None = None) -> logging.Logger:
    """取一个带类别的 logger：``get_logger("scrape", "f1")``。

    logger 名必须是 ``starwatt.<类别>[.<子模块>]``，否则 ``log_overrides``
    对它无效。这里做**校验**而不是静默接受拼错的名字。
    """
    if category not in CATEGORIES:
        raise ValueError(
            f"未知的日志类别：{category!r}（可用：{list(CATEGORIES)}）"
        )
    name = f"{ROOT_LOGGER}.{category}"
    return logging.getLogger(f"{name}.{sub}" if sub else name)


# ---------------------------------------------------------------------------
# 脱敏（Q20 铁律）
# ---------------------------------------------------------------------------
#: 短于这个长度不参与**子串**替换 —— 否则值 ``"a"`` 会把整条日志打成马赛克
_MIN_SECRET_LEN = 8

#: 已登记的 secret 明文（配置写入时登记）
_secrets: set[str] = set()
_secrets_lock = threading.RLock()

#: 常见凭据形态的正则（兜底：即使没登记也能拦住）
#:
#: 分隔符两侧都允许引号 —— ``app_secret=xxx``、``app_secret: xxx``、
#: ``"app_secret": "xxx"`` 三种写法都要能匹配（JSON 里键是被引号包住的）。
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?i)\b(open_?id|access_token|refresh_token|tenant_access_token"
            r"|verification_token|encrypt_key|app_secret|api_key|api[-_]?token"
            r"|password|passwd|secret|token)\b"
            r"(\s*[\"']?\s*[:=]\s*)([\"']?)"
            r"([^\s\"'&,;)\]}]{" + str(_MIN_SECRET_LEN) + r",})"
        ),
        r"\1\2\3***",
    ),
    # Authorization: Bearer xxx
    (
        re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/=-]{8,})"),
        r"\1***",
    ),
    # 飞书群机器人 webhook 的路径就是凭据
    (
        re.compile(r"(?i)(/open-apis/bot/v2/hook/)([A-Za-z0-9-]{8,})"),
        r"\1***",
    ),
)

#: 脱敏后的占位符
MASK = "***"


def register_secret(value: str | None) -> None:
    """登记一个 secret 明文，后续日志里出现即被替换为 ``***``。

    由 ``config_registry.store`` 在写入 ``secret=True`` 的配置项时调用。
    太短的值（< 8 字符）会被忽略 —— 否则会把正常文本打成马赛克。
    """
    if not value or len(value) < _MIN_SECRET_LEN:
        return
    with _secrets_lock:
        _secrets.add(value)


def forget_secret(value: str | None) -> None:
    """注销一个 secret（值被改掉时调用）。"""
    with _secrets_lock:
        _secrets.discard(value or "")


def registered_secrets() -> int:
    """已登记数量（测试与 /healthz 用，**不**返回内容）。"""
    with _secrets_lock:
        return len(_secrets)


def clear_secrets() -> None:
    """清空登记表（测试用）。"""
    with _secrets_lock:
        _secrets.clear()


def redact(text: str) -> str:
    """把 ``text`` 里的凭据替换掉。

    顺序：**先**精确匹配已登记的值（最可靠），**再**跑形态正则。
    """
    if not text:
        return text

    with _secrets_lock:
        known = sorted(_secrets, key=len, reverse=True)  # 长的优先，避免部分遮蔽
    for value in known:
        if value in text:
            text = text.replace(value, MASK)

    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """在**格式化之后**脱敏 —— 因此 f-string / ``%`` / ``.format()`` 都拦得住。

    挂在 handler 上（不是 logger 上）：这样即使某处代码直接调
    ``handler.handle(record)`` 也不会绕过。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()  # 已经格式化进 msg 了，清空避免二次格式化
        except Exception:  # noqa: BLE001 —— 日志绝不能让主流程崩
            record.msg = "<日志脱敏失败，已屏蔽原文>"
            record.args = ()
        return True


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------
_TEXT_FMT = "%(asctime)s %(levelname)-8s [%(category)s] %(name)s: %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class TextFormatter(logging.Formatter):
    """人类可读格式；补上 ``category`` 字段（没有的记 ``-``）。"""

    def __init__(self) -> None:
        super().__init__(fmt=_TEXT_FMT, datefmt=_DATE_FMT)

    def format(self, record: logging.LogRecord) -> str:
        if not hasattr(record, "category"):
            record.category = category_of(record.name) or "-"
        return super().format(record)


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象 —— 便于 Loki / ELK 采集。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            "level": record.levelname,
            "category": getattr(record, "category", None)
            or category_of(record.name)
            or "-",
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 类别级过滤
# ---------------------------------------------------------------------------
class CategoryFilter(logging.Filter):
    """按业务类别应用级别覆盖。

    为什么要 filter 而不是给每个 logger 设 ``setLevel``：
    级别是**运行时可变**的（配置改了立即生效），逐个 logger 改要维护一张
    与 logger 注册表同步的表；filter 每次读当前配置，天然一致。
    """

    def __init__(
        self,
        default_level: int = INFO,
        overrides: dict[str, int] | None = None,
    ) -> None:
        super().__init__()
        self.default_level = default_level
        self.overrides: dict[str, int] = dict(overrides or {})

    def update(self, default_level: int, overrides: dict[str, int]) -> None:
        """热更新（配置改了调用，无需重建 handler）。"""
        self.default_level = default_level
        self.overrides = dict(overrides)

    def level_for(self, logger_name: str) -> int:
        category = category_of(logger_name)
        if category is not None and category in self.overrides:
            return self.overrides[category]
        return self.default_level

    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= self.level_for(record.name)


# ---------------------------------------------------------------------------
# 装配
# ---------------------------------------------------------------------------
#: 本模块装上的 handler（teardown 用）
_installed: list[logging.Handler] = []

#: 已安装的类别过滤器（refresh 时热更新）
_filters: list[CategoryFilter] = []

#: 日志文件名
LOG_FILENAME = "starwatt.log"


def _effective_floor(default_level: int, overrides: dict[str, int]) -> int:
    """logger 自身级别要放到**最低**的那个 —— 否则记录在到达 filter 前就被丢了。

    例：全局 INFO、``scrape`` 覆盖为 TRACE。若 logger 级别是 INFO，
    ``starwatt.scrape.f1`` 的 TRACE 记录**根本不会创建**，
    CategoryFilter 再对也看不到它。
    """
    return min([default_level, *overrides.values()])


def _make_handler(
    stream,
    fmt: str,
    cat_filter: CategoryFilter,
) -> logging.Handler:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(cat_filter)  # 类别级过滤
    handler.addFilter(RedactingFilter())  # 脱敏（必须最后加）
    return handler


def _make_file_handler(
    log_dir: Path,
    fmt: str,
    retention_days: int,
    cat_filter: CategoryFilter,
) -> logging.Handler:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_dir / LOG_FILENAME),
        when="midnight",
        backupCount=max(1, retention_days),
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(cat_filter)
    handler.addFilter(RedactingFilter())
    return handler


#: 内置默认值（配置注册表读不到时用）
_FALLBACK: dict[str, object] = {
    "level": "INFO",
    "overrides": {},
    "format": "text",
    "file_enabled": False,
    "retention_days": 7,
}


def _read_config() -> dict:
    """从配置注册表读日志配置；**读不到就回落内置默认**。

    为什么必须容错：日志是最底层的子系统 —— 它绝不能让启动失败。
    首次启动时 ``db.init()`` 可能还没跑（``meta`` 表还不存在），
    此时读配置会抛 ``sqlite3.OperationalError``。
    同样，配置表被写坏时也应当能起服务、把问题**记进日志**。
    """
    try:
        from starwatt import config_registry as cfg

        return {
            "level": cfg.get_str("log_level", "INFO"),
            "overrides": cfg.get_json("log_overrides", {}) or {},
            "format": cfg.get_str("log_format", "text"),
            "file_enabled": cfg.get_bool("log_file_enabled", False),
            "retention_days": cfg.get_int("log_file_retention_days", 7),
        }
    except Exception:  # noqa: BLE001 —— 见上：读不到配置不能阻止打日志
        return dict(_FALLBACK)


def _parse_overrides(raw: object, on_bad_name=None) -> dict[str, int]:
    """把 ``{"scrape": "DEBUG"}`` 解析成 ``{"scrape": 10}``。

    拼错的类别名会被忽略 —— 但通过 ``on_bad_name`` 回调留痕，
    否则「我改了级别怎么没生效」会查很久。
    """
    out: dict[str, int] = {}
    for name, value in (raw or {}).items():  # type: ignore[union-attr]
        if name not in CATEGORIES:
            if on_bad_name is not None:
                on_bad_name(name)
            continue
        out[name] = level_from_name(value) if isinstance(value, str) else int(value)
    return out


def setup_logging(
    *,
    level: int | str | None = None,
    overrides: dict[str, int | str] | None = None,
    fmt: str | None = None,
    file_enabled: bool | None = None,
    retention_days: int | None = None,
    log_dir: Path | None = None,
    stream=None,
) -> logging.Logger:
    """装配日志。**幂等** —— 重复调用会先卸掉自己装的 handler。

    所有参数为 ``None`` 时从 ``config_registry`` 读（Q15：WebUI 可改）；
    显式传参则**只**用传进来的值，不再读库。

    Returns:
        项目根 logger（``starwatt``）。
    """
    logger = logging.getLogger(ROOT_LOGGER)

    # 只有在确实需要时才读配置 —— 显式传参的调用方（测试、CLI 早期启动）
    # 不应被「meta 表还不存在」绊住
    need_config = (
        level is None or overrides is None or fmt is None
        or file_enabled is None or retention_days is None
    )
    conf = _read_config() if need_config else dict(_FALLBACK)

    raw_level = conf["level"] if level is None else level
    default_level = (
        level_from_name(raw_level) if isinstance(raw_level, str) else int(raw_level)
    )

    raw_overrides = conf["overrides"] if overrides is None else overrides
    bad_names: list[str] = []
    parsed_overrides = _parse_overrides(raw_overrides, on_bad_name=bad_names.append)

    log_format = str(conf["format"] if fmt is None else fmt).lower()
    if log_format not in ("text", "json"):
        log_format = "text"
    want_file = bool(conf["file_enabled"] if file_enabled is None else file_enabled)
    days = int(conf["retention_days"] if retention_days is None else retention_days)

    _detach(logger)

    logger.setLevel(_effective_floor(default_level, parsed_overrides))
    logger.propagate = False  # 不再往 root 冒泡，避免重复输出

    cat_filter = CategoryFilter(default_level, parsed_overrides)
    _filters.append(cat_filter)

    console = _make_handler(stream or sys.stderr, log_format, cat_filter)
    logger.addHandler(console)
    _installed.append(console)

    # ⚠️ 这些告警必须在 handler 装好**之后**发 —— 否则它们进不了日志
    # （用户按提示去查「改了级别没生效」时会发现日志里什么都没有）
    for name in bad_names:
        logger.warning("log_overrides 里的类别名无法识别，已忽略：%r", name)

    if want_file:
        directory = log_dir or _default_log_dir()
        try:
            file_handler = _make_file_handler(
                directory, log_format, days, cat_filter
            )
            logger.addHandler(file_handler)
            _installed.append(file_handler)
        except OSError as exc:
            # 只读文件系统 / 权限不足 → 降级为「只打控制台」，不阻止启动
            logger.warning("无法写入日志文件（%s），已降级为仅控制台输出", exc)

    return logger


def _default_log_dir() -> Path:
    """``<数据目录>/logs``。"""
    from starwatt.config import get_settings

    return get_settings().data_dir / "logs"


def _detach(logger: logging.Logger) -> None:
    """卸掉本模块之前装的 handler（幂等重装 / 测试清理）。"""
    for handler in _installed:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:  # noqa: BLE001 —— 关闭失败不该影响启动
            pass
    _installed.clear()
    _filters.clear()


def refresh_logging() -> None:
    """配置改了之后热更新级别（无需重启）。

    只改**级别**；格式 / 文件开关的变化需要 :func:`setup_logging` 重建 handler。
    读不到配置时**保持现状**（不抛异常）—— 热更新失败不该影响正在跑的进程。
    """
    conf = _read_config()
    default_level = level_from_name(conf["level"])
    overrides = _parse_overrides(conf["overrides"])

    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(_effective_floor(default_level, overrides))
    for cat_filter in _filters:
        cat_filter.update(default_level, overrides)


def teardown_logging() -> None:
    """卸掉本模块装的 handler（测试与优雅关闭用）。"""
    _detach(logging.getLogger(ROOT_LOGGER))
