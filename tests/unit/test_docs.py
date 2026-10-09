"""交付文档契约（M6 §6.5 / §6.6 / N7）。

DoD 原文：「部署指南 / 配置说明 / FAQ / 升级回滚 文档齐备」「`CONTRIBUTING.md` 存在」

文档这种东西不会因为「少写一句」而报错，只会慢慢和代码脱节。这里钉三件事：

1. **该有的文档在**，且不是占位符（有实质内容）
2. **相对链接都能打开** —— 文档之间互相引用时最容易留下死链，
   而读者点开 404 就等于没有这份文档
3. **EXTENDING.md 真的覆盖了 6 类扩展**（Q18 的承诺）
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[2]

#: 面向使用者的四份文档（N7）+ 扩展指南（Q18）+ 贡献指南
REQUIRED_DOCS = {
    PROJ / "docs" / "DEPLOY.md": "部署指南",
    PROJ / "docs" / "CONFIG.md": "配置说明",
    PROJ / "docs" / "FAQ.md": "常见问题",
    PROJ / "docs" / "UPGRADE.md": "升级与回滚",
    PROJ / "docs" / "EXTENDING.md": "扩展指南",
    PROJ / "CONTRIBUTING.md": "贡献指南",
}

#: 扩展指南必须覆盖的六类扩展（Q18 的表格）
SIX_EXTENSIONS = [
    "加一个配置项",
    "加一个通知渠道",
    "适配其他学校",
    "加一个告警层",
    "加一个机器人命令",
    "加一个前端页面",
]

#: 会被检查链接的文件（README + 全部文档）
LINKED_FILES = [PROJ / "README.md", *REQUIRED_DOCS]

_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


class TestRequiredDocs:
    @pytest.mark.parametrize(("path", "label"), sorted(REQUIRED_DOCS.items()))
    def test_exists_with_content(self, path: Path, label: str) -> None:
        assert path.is_file(), f"缺少{label}：{path.name}"
        text = path.read_text(encoding="utf-8")
        # 不是占位符：至少要有标题 + 若干小节
        assert len(text) > 800, f"{path.name} 太短了，像占位符"
        assert text.lstrip().startswith("#"), f"{path.name} 缺标题"
        assert text.count("\n## ") >= 2, f"{path.name} 的小节太少"

    def test_deploy_covers_all_three_paths(self) -> None:
        text = (PROJ / "docs" / "DEPLOY.md").read_text(encoding="utf-8")
        for keyword in ("docker compose up -d", "docker load", "install.sh"):
            assert keyword in text, keyword

    def test_config_documents_both_layers(self) -> None:
        """Q15：`.env` 只放启动必需项，业务配置在注册表里。"""
        text = (PROJ / "docs" / "CONFIG.md").read_text(encoding="utf-8")
        for key in ("DB_PATH", "DORM_DATA_DIR", "FLASK_PORT", "FLASK_SECRET_KEY"):
            assert key in text, key
        assert "加密" in text and "FLASK_SECRET_KEY" in text

    def test_upgrade_has_a_rollback_recipe(self) -> None:
        text = (PROJ / "docs" / "UPGRADE.md").read_text(encoding="utf-8")
        assert "回滚" in text
        assert "备份" in text
        # 回滚要能落地：给出具体命令而不是「回退到旧版本」这种空话
        assert "docker compose down" in text or "git checkout" in text

    def test_faq_answers_the_forgot_password_case(self) -> None:
        """忘记密码是最高频的问题，且答案必须指向真实做法（没有网页重置入口）。"""
        text = (PROJ / "docs" / "FAQ.md").read_text(encoding="utf-8")
        assert "忘记管理员密码" in text
        assert "auth.create_user" in text


class TestExtendingGuide:
    def test_covers_the_six_extension_types(self) -> None:
        text = (PROJ / "docs" / "EXTENDING.md").read_text(encoding="utf-8")
        for title in SIX_EXTENSIONS:
            assert title in text, f"EXTENDING.md 没写「{title}」"

    def test_config_item_still_promises_one_file(self) -> None:
        """DoD 验收：加一个配置项只需改 1 个文件（这条承诺别被悄悄改掉）。"""
        text = (PROJ / "docs" / "EXTENDING.md").read_text(encoding="utf-8")
        assert "registry.py" in text
        assert "**1**" in text

    def test_points_at_the_real_guards(self) -> None:
        """SOP 里提到的守卫必须是真实存在的文件/脚本。"""
        text = (PROJ / "docs" / "EXTENDING.md").read_text(encoding="utf-8")
        for target in ("scripts/ast_guard.py", "tests/regression/fixtures/"):
            assert target in text, target
        assert (PROJ / "scripts" / "ast_guard.py").is_file()


class TestRelativeLinks:
    """README 与文档里的相对链接必须都能打开。"""

    @pytest.mark.parametrize("path", LINKED_FILES, ids=lambda p: p.name)
    def test_relative_links_resolve(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        broken: list[str] = []
        for raw in _MD_LINK.findall(text):
            target = raw.split("#", 1)[0].strip()
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            if target.startswith("/"):
                continue  # 站点绝对路径，不是文件引用
            if not (path.parent / target).exists():
                broken.append(raw)
        assert not broken, f"{path.name} 里的死链：{broken}"


# ===========================================================================
# 截图（M6 §6.7）
# ===========================================================================
SHOTS = PROJ / "docs" / "screenshots"
#: DoD 要求 3 张：登录 / 仪表盘 / 管理后台
REQUIRED_SHOTS = ["01-login.png", "02-dashboard.png", "03-admin-config.png"]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _png_size(path: Path) -> tuple[int, int]:
    """从 IHDR 里读出宽高（不引入图像库）。"""
    head = path.read_bytes()[:24]
    assert head[:8] == PNG_MAGIC, f"{path.name} 不是 PNG"
    width = int.from_bytes(head[16:20], "big")
    height = int.from_bytes(head[20:24], "big")
    return width, height


class TestScreenshots:
    """「3 张截图」这条 DoD 的凭证：文件在、是真 PNG、不是空白图。"""

    @pytest.mark.parametrize("name", REQUIRED_SHOTS)
    def test_exists_and_is_a_real_screenshot(self, name: str) -> None:
        path = SHOTS / name
        assert path.is_file(), f"缺少截图：{name}"
        # 空白图或占位图会非常小；真实界面截图有几十 KB
        assert path.stat().st_size > 10_000, f"{name} 太小，像是空白图"
        width, height = _png_size(path)
        assert width >= 1000 and height >= 600, f"{name} 尺寸异常：{width}x{height}"

    def test_readme_shows_them(self) -> None:
        readme = (PROJ / "README.md").read_text(encoding="utf-8")
        for name in REQUIRED_SHOTS:
            assert f"docs/screenshots/{name}" in readme, name
