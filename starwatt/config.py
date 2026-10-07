"""启动配置 —— 只读 ``.env`` 里「DB 打开前必需」的 4 项（Q15）。

分层
====

Q15 要求「所有配置项都能在 WebUI 改」。但有一类配置**必须在 DB 打开之前
就已知**，否则程序根本起不来 —— 它们只能留在 ``.env``::

    DB_PATH           不知道 DB 在哪，就打不开 DB
    FLASK_SECRET_KEY  用它派生 secrets 加密密钥；它自己不能加密自己
    FLASK_PORT        起服务前就要知道监听哪个端口
    DORM_DATA_DIR     数据目录位置（DB / 备份 / 日志的根）

**其余全部配置项**（抓取 / 阈值 / 推送 / 机器人 / 认证 / 日志 / 高级）
都在 ``starwatt.config_registry``，存 ``meta`` 表，WebUI 可改。

本模块**不 import 任何子模块** —— 它是所有层的最底层依赖。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path

from dotenv import load_dotenv

#: 项目根（``starwatt/`` 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: ``.env`` 位置 —— 钉在项目根，不跟随 cwd
#: （cron / systemd 的 cwd 可能是 /var/www，默认搜索路径会找不到）
ENV_FILE = PROJECT_ROOT / ".env"

#: 从 ``.env`` 载入 ``os.environ``；**已存在的进程环境变量优先**
#: （systemd ``EnvironmentFile=`` 注入的值不会被文件覆盖）。
#: 文件不存在时是 no-op。
load_dotenv(dotenv_path=ENV_FILE, override=False)

#: 启动必需项的键名（Q15 白名单）
BOOTSTRAP_KEYS: tuple[str, ...] = (
    "DB_PATH",
    "DORM_DATA_DIR",
    "FLASK_PORT",
    "FLASK_SECRET_KEY",
)

#: 默认监听端口
DEFAULT_PORT = 5000


@dataclass(frozen=True, slots=True)
class Settings:
    """启动期不可变配置。

    ``frozen=True`` 让「运行中配置被偷偷改掉」变成 ``FrozenInstanceError``；
    测试要换配置时用 :func:`override_settings`。
    """

    project_root: Path
    data_dir: Path
    db_path: Path
    flask_port: int
    flask_secret_key: str

    @property
    def db_file(self) -> str:
        """``sqlite3.connect()`` 需要的字符串路径。"""
        return str(self.db_path)

    def with_(self, **changes: object) -> Settings:
        """返回一个改了若干字段的新实例（``frozen`` 的替代写法）。"""
        return replace(self, **changes)  # type: ignore[arg-type]


def _resolve_dir(raw: str | None, fallback: Path) -> Path:
    """把目录配置解析成绝对路径（相对路径按项目根解析）。"""
    text = (raw or "").strip()
    if not text:
        return fallback
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """从环境变量构造 :class:`Settings`。

    ``env`` 只用于测试注入；默认读 ``os.environ``（已含 ``.env`` 的值）。

    相对路径规则
    ------------

    ``DORM_DATA_DIR`` / ``DB_PATH`` 允许写相对路径（``.env.example`` 里就是
    ``DB_PATH=./records.db``），统一**按项目根解析**，避免 cron / systemd
    以不同 cwd 启动时指向不同文件。
    """
    src = os.environ if env is None else env

    data_dir = _resolve_dir(src.get("DORM_DATA_DIR"), PROJECT_ROOT)

    db_raw = (src.get("DB_PATH") or "").strip()
    if db_raw:
        db_path = Path(db_raw).expanduser()
        if not db_path.is_absolute():
            db_path = (data_dir / db_path).resolve()
    else:
        db_path = data_dir / "records.db"

    port_raw = (src.get("FLASK_PORT") or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_PORT
    except ValueError:
        port = DEFAULT_PORT
    if not (1 <= port <= 65535):
        port = DEFAULT_PORT

    return Settings(
        project_root=PROJECT_ROOT,
        data_dir=data_dir,
        db_path=db_path,
        flask_port=port,
        flask_secret_key=(src.get("FLASK_SECRET_KEY") or "").strip(),
    )


# ---------------------------------------------------------------------------
# 懒加载单例（可被测试覆盖）
# ---------------------------------------------------------------------------
_settings: Settings | None = None


def get_settings() -> Settings:
    """返回全局 :class:`Settings`（首次调用时从环境构造）。"""
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def override_settings(new: Settings | None) -> None:
    """测试用：替换全局 Settings；传 ``None`` 恢复懒加载。"""
    global _settings
    _settings = new


__all__ = [
    "BOOTSTRAP_KEYS",
    "DEFAULT_PORT",
    "ENV_FILE",
    "PROJECT_ROOT",
    "Settings",
    "get_settings",
    "load_settings",
    "override_settings",
]
