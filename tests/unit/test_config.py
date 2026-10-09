"""``starwatt.config`` 单元测试（Q15 —— 只留 4 项启动必需配置）。"""
from __future__ import annotations

from pathlib import Path

from starwatt import config


class TestBootstrapWhitelist:
    def test_exactly_four_keys(self) -> None:
        """Q15：``.env`` 只保留 4 项「DB 打开前必需」的键。"""
        assert set(config.BOOTSTRAP_KEYS) == {
            "DB_PATH",
            "DORM_DATA_DIR",
            "FLASK_PORT",
            "FLASK_SECRET_KEY",
        }

    def test_env_file_pinned_to_project_root(self) -> None:
        """不跟随 cwd —— cron / systemd 的 cwd 可能是 /var/www。"""
        assert config.ENV_FILE == config.PROJECT_ROOT / ".env"


class TestLoadSettingsDefaults:
    def test_empty_env_uses_project_root(self) -> None:
        s = config.load_settings({})
        assert s.project_root == config.PROJECT_ROOT
        assert s.data_dir == config.PROJECT_ROOT
        assert s.db_path == config.PROJECT_ROOT / "records.db"
        assert s.flask_port == config.DEFAULT_PORT
        assert s.flask_secret_key == ""

    def test_db_file_returns_str(self) -> None:
        s = config.load_settings({})
        assert isinstance(s.db_file, str)


class TestRelativePathResolution:
    def test_relative_data_dir_resolved_against_project_root(self) -> None:
        s = config.load_settings({"DORM_DATA_DIR": "mydata"})
        assert s.data_dir == (config.PROJECT_ROOT / "mydata").resolve()
        assert s.data_dir.is_absolute()

    def test_relative_db_path_resolved_against_data_dir(self) -> None:
        """🔑 ``.env.example`` 里写的是 ``DB_PATH=./records.db`` —— 必须解析成绝对路径。

        修复前会原样返回 ``Path("records.db")``，导致 cron / systemd 以不同
        cwd 启动时指向**不同文件**。
        """
        s = config.load_settings({"DORM_DATA_DIR": "mydata", "DB_PATH": "./records.db"})
        assert s.db_path == (config.PROJECT_ROOT / "mydata" / "records.db").resolve()
        assert s.db_path.is_absolute()

    def test_absolute_db_path_kept(self) -> None:
        abs_dir = Path(config.PROJECT_ROOT).resolve()
        s = config.load_settings({"DB_PATH": str(abs_dir / "x.db")})
        assert s.db_path == abs_dir / "x.db"

    def test_db_path_falls_back_to_data_dir(self) -> None:
        s = config.load_settings({"DORM_DATA_DIR": "mydata"})
        assert s.db_path == (config.PROJECT_ROOT / "mydata" / "records.db").resolve()


class TestPort:
    def test_valid_port(self) -> None:
        assert config.load_settings({"FLASK_PORT": "8080"}).flask_port == 8080

    def test_non_numeric_falls_back(self) -> None:
        assert config.load_settings({"FLASK_PORT": "abc"}).flask_port == config.DEFAULT_PORT

    def test_out_of_range_falls_back(self) -> None:
        assert config.load_settings({"FLASK_PORT": "0"}).flask_port == config.DEFAULT_PORT
        assert config.load_settings({"FLASK_PORT": "70000"}).flask_port == config.DEFAULT_PORT
        assert config.load_settings({"FLASK_PORT": "-1"}).flask_port == config.DEFAULT_PORT

    def test_empty_falls_back(self) -> None:
        assert config.load_settings({"FLASK_PORT": "  "}).flask_port == config.DEFAULT_PORT


class TestSecretKey:
    def test_stripped(self) -> None:
        s = config.load_settings({"FLASK_SECRET_KEY": "  abc  "})
        assert s.flask_secret_key == "abc"

    def test_empty_by_default(self) -> None:
        assert config.load_settings({}).flask_secret_key == ""


class TestImmutability:
    def test_settings_is_frozen(self) -> None:
        import dataclasses

        s = config.load_settings({})
        assert dataclasses.is_dataclass(s)
        try:
            s.flask_port = 1  # type: ignore[misc]
        except dataclasses.FrozenInstanceError:
            pass
        else:
            raise AssertionError("Settings 必须是 frozen")

    def test_with_returns_new_instance(self) -> None:
        s = config.load_settings({})
        t = s.with_(flask_port=1234)
        assert t.flask_port == 1234
        assert s.flask_port == config.DEFAULT_PORT


class TestSingleton:
    def test_get_settings_is_cached(self, monkeypatch) -> None:
        config.override_settings(None)
        try:
            first = config.get_settings()
            second = config.get_settings()
            assert first is second
        finally:
            config.override_settings(None)

    def test_override_settings_replaces(self) -> None:
        config.override_settings(None)
        try:
            custom = config.load_settings({"FLASK_PORT": "6001"})
            config.override_settings(custom)
            assert config.get_settings() is custom
            assert config.get_settings().flask_port == 6001
        finally:
            config.override_settings(None)


class TestSettingsFixture:
    def test_fixture_isolates_db(self, settings_override) -> None:
        """conftest 的 ``settings_override`` 必须把 DB 指到临时目录。"""
        assert settings_override.db_path != config.PROJECT_ROOT / "records.db"
        assert settings_override.db_path.is_absolute()
        assert config.get_settings() is settings_override
