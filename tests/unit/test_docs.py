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

#: 面向使用者的四份文档（N7）+ 扩展指南（Q18）+ 贡献指南 + 切换验收（M7）+ 审计报告
REQUIRED_DOCS = {
    PROJ / "docs" / "DEPLOY.md": "部署指南",
    PROJ / "docs" / "DEPLOY_PAAS.md": "托管平台部署指南",
    PROJ / "docs" / "CONFIG.md": "配置说明",
    PROJ / "docs" / "FAQ.md": "常见问题",
    PROJ / "docs" / "UPGRADE.md": "升级与回滚",
    PROJ / "docs" / "M7_CUTOVER.md": "切换与验收指南",
    PROJ / "docs" / "AGENT_RUNBOOK.md": "服务器切换 Runbook（交给服务器 Agent）",
    PROJ / "docs" / "AUDIT_REPORT.md": "全面审计报告",
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


class TestAgentRunbook:
    """`docs/AGENT_RUNBOOK.md` 是交给服务器 Agent 的**唯一依据** —— 它退步会直接害人。

    下面每条断言都对应一次真实踩坑或一次会丢数据的操作，不是形式主义。
    """

    @staticmethod
    def _text() -> str:
        return (PROJ / "docs" / "AGENT_RUNBOOK.md").read_text(encoding="utf-8")

    def test_covers_every_phase(self) -> None:
        text = self._text()
        for marker in (
            "§0 执行契约",
            "§1 项目与本次任务",
            "§2 Phase 0",
            "§3 Phase 2",
            "§4 Phase 3",
            "§5 Phase 4",
            "§6 Phase 5",
            "§7 Phase 6",
            "附录 A",
            "附录 B",
            "附录 C",
        ):
            assert marker in text, marker

    def test_resolves_the_two_names(self) -> None:
        """服务器上只有 dorm-power-monitor、没有 starwatt —— 第一次执行就卡在这。"""
        text = self._text()
        assert "dorm-power-monitor" in text and "StarWatt" in text
        assert "不要以为找错了" in text

    def test_states_the_decisions_instead_of_asking(self) -> None:
        """已定决策必须写在文档里，否则 Agent 每一轮都会再问一遍。"""
        text = self._text()
        assert "已定决策" in text and "不要再问" in text
        for decision in ("Docker", "5001", "5000", "/var/lib/dorm-power-monitor", "chown 10001"):
            assert decision in text, decision

    def test_carries_the_old_secret_key_into_the_container(self) -> None:
        """🔴 不传旧 FLASK_SECRET_KEY → 配置中心里的凭据永久解不开（红线 R2）。"""
        text = self._text()
        assert "--env-file" in text
        assert "FLASK_SECRET_KEY" in text
        # 必须明确「复用」而不是让容器自己生成
        assert "复用数据卷里的 FLASK_SECRET_KEY" in text
        # 并且要有一个能直接验证解密是否成功的检查
        assert "dorm_openid 解密" in text

    def test_mounts_the_copied_database_explicitly(self) -> None:
        """🔴 用 compose 的命名卷会忽略复制过去的库 → 新实例是空库，看起来像数据全丢。"""
        text = self._text()
        assert "-v /var/lib/dorm-power-monitor:/data" in text
        assert "命名卷" in text and "空库" in text
        # 旧写法（compose up -d）不得作为正式切换步骤出现
        assert "docker compose up -d" not in text

    def test_verifies_data_survived(self) -> None:
        """「数据没丢」必须有三重证据，而不是一句承诺。"""
        text = self._text()
        assert "sha256sum /opt/dorm-power-monitor/records.db" in text
        assert "COUNT(*), MIN(ts), MAX(ts)" in text
        assert "行数" in text and "基线" in text

    def test_has_the_three_red_lines(self) -> None:
        text = self._text()
        assert "红线" in text
        assert "不得删除、覆盖、移动" in text
        assert "不得覆盖" in text
        assert "不得停止/重启旧服务" in text

    def test_says_when_to_stop_and_ask(self) -> None:
        """不自作主张是这份文档最重要的行为约束。"""
        text = self._text()
        assert "停下来报告" in text or "停下来问" in text
        assert "不猜" in text

    def test_never_asks_for_secret_values_in_reports(self) -> None:
        text = self._text()
        # 允许两种措辞，但必须明确要求「只给键名、不给值」
        assert "键名" in text
        assert ("只贴键名" in text) or ("只报键名" in text) or ("只要键名" in text)
        assert "不要值" in text or "不要把值贴" in text


class TestAuditReport:
    """`docs/AUDIT_REPORT.md` 是「哪些坑踩过」的唯一留档。

    它退步的代价是：下一个改这段代码的人会把这些坑重新踩一遍 ——
    所以每条断言都对应一个**真实修过的缺陷**，不是形式主义。
    """

    @staticmethod
    def _text() -> str:
        return (PROJ / "docs" / "AUDIT_REPORT.md").read_text(encoding="utf-8")

    def test_documents_every_defect(self) -> None:
        """每个已修缺陷都要在报告里留名（用定位它的关键字）。"""
        text = self._text()
        for marker in (
            "post_form",  # P0：客户端拒绝 JSON 数组
            "update_dt",  # 电表「最后上报」
            "daily_avg",  # 概览日均（窗口 + 充值两处）
            "hourly_used",  # 窗口副作用
            "violations_today",  # 今日违规
            "exc_info",  # 脱敏漏异常栈
            ".backup",  # runbook 的 WAL 备份
            "2.0.1",  # 版本指向 / compose 默认值
        ):
            assert marker in text, marker

    def test_records_the_evidence_and_the_pattern(self) -> None:
        """报告必须写清「怎么发现的」与那条规律 —— 否则只是流水账。"""
        text = self._text()
        assert "真实响应" in text  # 证据来源
        assert "孪生兄弟" in text  # 规律
        assert "为什么测试全绿却漏了这些" in text
        assert "遗留建议" in text  # 未采纳项也要留痕


class TestDbCompatibilityTool:
    """``scripts/check_db.py`` —— 「我这是哪一版」的答案，别让它从文档里消失。"""

    def test_tool_exists(self) -> None:
        assert (PROJ / "scripts" / "check_db.py").is_file()

    @pytest.mark.parametrize("doc", ["FAQ.md", "DEPLOY.md", "UPGRADE.md", "M7_CUTOVER.md"])
    def test_docs_tell_users_to_run_it(self, doc: str) -> None:
        text = (PROJ / "docs" / doc).read_text(encoding="utf-8")
        assert "scripts.check_db" in text, f"{doc} 没告诉用户跑自检工具"

    def test_cutover_guide_starts_with_the_database(self) -> None:
        """切换指南必须先解决「我的库能不能用」，这是用户最不确定的一步。"""
        text = (PROJ / "docs" / "M7_CUTOVER.md").read_text(encoding="utf-8")
        assert text.index("第 0 步") < text.index("第 1 步") < text.index("第 2 步")
        assert "零迁移" in text and "回滚" in text
        # 必须给出「旁路试跑 + 副本」这条安全路径
        assert "副本" in text
        assert "check_db" in text


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
