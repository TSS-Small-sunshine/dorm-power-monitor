"""CI 守卫脚本自身的测试 —— ``scripts/ast_guard.py`` / ``scripts/secret_scan.py``。

守卫脚本是「防线的防线」：如果它们悄悄失效（误报多到被忽略、或漏报），
没人会注意到。所以它们本身也要被测。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[2]
if str(PROJ) not in sys.path:
    sys.path.insert(0, str(PROJ))

from scripts import ast_guard, secret_scan  # noqa: E402

# 假凭据必须**拼装**而成：源码里若出现 ≥24 位连续字母数字，
# 本文件会被自己的扫描器命中（自指陷阱）。
FAKE_SECRET = "a1b2c3d4e5f6g7h8i9j0" + "k1l2m3n4"
FAKE_OPENID = "oX9bZk2mQ7pL4vN8wR1tY6" + "uI3aS5dF0gH"


@pytest.fixture(autouse=True)
def _clean_violations():
    """每次用例前后清空 ast_guard 的全局违规列表。"""
    ast_guard.VIOLATIONS.clear()
    yield
    ast_guard.VIOLATIONS.clear()


# ===========================================================================
# ast_guard
# ===========================================================================
class TestAstGuard:
    def test_repo_is_clean(self) -> None:
        assert ast_guard.main() == 0
        assert ast_guard.VIOLATIONS == []

    def test_r8_detects_bom(self, tmp_path, monkeypatch) -> None:
        """R8：带 UTF-8 BOM 的 .py 必须被拦下（M1 真实踩过的坑）。"""
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        (tmp_path / "bommed.py").write_bytes(b"\xef\xbb\xbfx = 1\n")

        ast_guard._check_bom_everywhere()
        assert any("[R8]" in v and "bommed.py" in v for v in ast_guard.VIOLATIONS)

    def test_r8_allows_clean_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        (tmp_path / "clean.py").write_bytes(b"x = 1\n")
        ast_guard._check_bom_everywhere()
        assert ast_guard.VIOLATIONS == []

    def test_r8_skips_vendored_dirs(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        for name in (".venv", "node_modules", "__pycache__"):
            d = tmp_path / name
            d.mkdir()
            (d / "bommed.py").write_bytes(b"\xef\xbb\xbfx = 1\n")
        ast_guard._check_bom_everywhere()
        assert ast_guard.VIOLATIONS == []

    def test_r7_detects_naive_now(self, tmp_path, monkeypatch) -> None:
        """R7：``datetime.now()`` 必须被拦（否则时间无法统一冻结）。"""
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        pkg = tmp_path / "starwatt"
        pkg.mkdir()
        (pkg / "bad.py").write_text(
            "import datetime\nx = datetime.datetime.now()\n", encoding="utf-8"
        )
        ast_guard._check_file(pkg / "bad.py")
        assert any("[R7]" in v for v in ast_guard.VIOLATIONS)

    def test_r7_allows_tz_aware(self, tmp_path, monkeypatch) -> None:
        """带显式时区参数是允许的。"""
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        pkg = tmp_path / "starwatt"
        pkg.mkdir()
        (pkg / "ok.py").write_text(
            "import datetime\nx = datetime.datetime.now(datetime.UTC)\n",
            encoding="utf-8",
        )
        ast_guard._check_file(pkg / "ok.py")
        assert ast_guard.VIOLATIONS == []

    def test_r4_detects_legacy_import(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(ast_guard, "ROOT", tmp_path)
        pkg = tmp_path / "starwatt"
        pkg.mkdir()
        (pkg / "bad.py").write_text("import feishu_bot\n", encoding="utf-8")
        ast_guard._check_file(pkg / "bad.py")
        assert any("[R4]" in v for v in ast_guard.VIOLATIONS)


# ===========================================================================
# secret_scan
# ===========================================================================
class TestSecretScan:
    def test_repo_is_clean(self) -> None:
        assert secret_scan.scan() == []
        assert secret_scan.main() == 0

    def test_catches_hardcoded_secret(self, tmp_path) -> None:
        (tmp_path / "cfg.py").write_text(
            f"FEISHU_APP_SECRET = '{FAKE_SECRET}'\n", encoding="utf-8"
        )
        hits = secret_scan.scan(tmp_path)
        assert len(hits) == 1 and "cfg.py" in hits[0]

    def test_local_env_is_skipped(self, tmp_path) -> None:
        """本地 ``.env`` 按设计装真凭据，且已 gitignore → 不告警。"""
        (tmp_path / ".env").write_text(f"OPENID={FAKE_OPENID}\n", encoding="utf-8")
        assert secret_scan.scan(tmp_path) == []

    def test_catches_env_example_secret(self, tmp_path) -> None:
        """回归：``.env.example`` 会提交进仓库，必须扫；且它同样是点文件
        （``Path('.env.example').suffix`` 为空），曾因此被整个漏掉。"""
        (tmp_path / ".env.example").write_text(
            f"OPENID={FAKE_OPENID}\n", encoding="utf-8"
        )
        assert len(secret_scan.scan(tmp_path)) == 1

    def test_ignores_env_key_constants(self, tmp_path) -> None:
        """值的**内容**是环境变量键名 → 不是凭据（真实误报，已修）。"""
        (tmp_path / "const.py").write_text(
            'ENV_BOOTSTRAP_PASSWORD = "BOOTSTRAP_ADMIN_PASSWORD"\n', encoding="utf-8"
        )
        assert secret_scan.scan(tmp_path) == []

    def test_ignores_short_placeholder(self, tmp_path) -> None:
        (tmp_path / ".env.example").write_text(
            "API_KEY=changeme\nSECRET_KEY=replace-me\n", encoding="utf-8"
        )
        assert secret_scan.scan(tmp_path) == []

    def test_ignores_non_scanned_suffix(self, tmp_path) -> None:
        (tmp_path / "notes.bin").write_text(f"TOKEN={FAKE_SECRET}\n", encoding="utf-8")
        assert secret_scan.scan(tmp_path) == []

    def test_skips_vendored_dirs(self, tmp_path) -> None:
        d = tmp_path / ".venv"
        d.mkdir()
        (d / "leak.py").write_text(
            f"APP_SECRET = '{FAKE_SECRET}'\n", encoding="utf-8"
        )
        assert secret_scan.scan(tmp_path) == []


class TestEffectiveSuffix:
    """点文件的后缀判断（`.env` 漏扫的根因）。"""

    def test_dotfiles(self) -> None:
        assert secret_scan._effective_suffix(".env") == ".env"
        assert secret_scan._effective_suffix(".env.example") == ".env.example"

    def test_regular_files(self) -> None:
        assert secret_scan._effective_suffix("a.py") == ".py"
        assert secret_scan._effective_suffix("notes.bin") == ".bin"

    def test_extensionless(self) -> None:
        assert secret_scan._effective_suffix("Makefile") == ""
        assert secret_scan._effective_suffix("Makefile") not in secret_scan.SUFFIXES

    def test_differs_from_pathlib_suffix(self) -> None:
        """正是这个差别导致 ``.env`` 曾被跳过。"""
        assert Path(".env").suffix == ""
        assert secret_scan._effective_suffix(".env") == ".env"
