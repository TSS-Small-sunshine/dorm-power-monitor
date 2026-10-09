"""部署产物契约（M6 §6.1 / §6.3 / §6.4）。

为什么这些断言值得存在
=====================

本机（和多数开发机）**没有 Docker**，`docker build` / `docker compose up` 跑不了；
但镜像与脚本里的错误**几乎都不会在开发时暴露**，只会在用户服务器上炸：

* ``GUNICORN_BIND`` 忘了改成 ``0.0.0.0`` → 容器起来了、外面访问不到（看着像挂了）
* entrypoint 没持久化 ``FLASK_SECRET_KEY`` → 每次重启换密钥 →
  **加密的凭据永久解不开**（配置中心里所有 secret）
* ``install.sh`` 覆盖了已有 ``.env`` → 同上，而且是升级场景的**数据丢失级**事故
* 前端没在镜像里构建 → ``docker run`` 出来是空壳（``static/`` 产物不在仓库里）

所以这里像 ``test_web_entry.py`` 钉 systemd 单元那样，把部署产物的**关键契约**
钉死；能用真 bash 执行的地方就真执行（语法 + 行为）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PROJ = Path(__file__).resolve().parents[2]

DOCKERFILE = PROJ / "Dockerfile"
DOCKERIGNORE = PROJ / ".dockerignore"
COMPOSE = PROJ / "docker-compose.yml"
ENTRYPOINT = PROJ / "docker-entrypoint.sh"
INSTALL = PROJ / "install.sh"
UNIT = PROJ / "deploy" / "dorm-web.service"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _bash() -> str | None:
    """找一个可用的 bash（Linux CI 直接有；Windows 上找 Git 自带的那份）。"""
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        r"D:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\bin\bash.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="本机没有 bash，跳过脚本执行类断言")


# ===========================================================================
# 文件齐备
# ===========================================================================
class TestDeployFiles:
    def test_all_present(self) -> None:
        for path in (DOCKERFILE, DOCKERIGNORE, COMPOSE, ENTRYPOINT, INSTALL, UNIT):
            assert path.is_file(), f"缺少部署文件：{path.name}"

    @pytest.mark.parametrize(
        "name",
        [
            "docker-entrypoint.sh",
            "install.sh",
            "Dockerfile",
            "docker-compose.yml",
            ".dockerignore",
            "deploy/dorm-web.service",
        ],
    )
    def test_no_crlf(self, name: str) -> None:
        """CRLF 会让这些文件在 Linux 上以「看不出原因」的方式失败。

        * ``docker-entrypoint.sh`` → ``exec /app/docker-entrypoint.sh:
          no such file or directory``（shebang 末尾多了个 \\r）
        * ``install.sh`` → ``/usr/bin/env: 'bash\\r': No such file or directory``
        * systemd 单元 → ``ExecStart`` 解析出奇怪的值，服务起不来

        这个项目在 Windows 上开发、在 Linux 上跑，所以必须有这道闸。
        配套的 ``.gitattributes``（``eol=lf``）保证检出时就不会带回 CRLF。
        """
        raw = (PROJ / name).read_bytes()
        assert b"\r\n" not in raw, f"{name} 里有 CRLF —— Linux 上会报 bad interpreter"

    def test_gitattributes_pins_lf_for_scripts(self) -> None:
        text = (PROJ / ".gitattributes").read_text(encoding="utf-8")
        assert "*.sh" in text and "eol=lf" in text
        assert "*.service" in text
        assert "Dockerfile" in text


# ===========================================================================
# Dockerfile
# ===========================================================================
class TestDockerfile:
    def test_is_multi_stage(self) -> None:
        """三阶段：前端构建 → 依赖编译 → 运行时（编译工具不进最终镜像）。"""
        source = _read(DOCKERFILE)
        stages = [line for line in source.splitlines() if line.startswith("FROM ")]
        assert len(stages) >= 3, stages
        assert any("node:" in line for line in stages), "缺前端构建阶段"
        assert any("python:" in line for line in stages), "缺 Python 阶段"

    def test_frontend_is_built_inside_the_image(self) -> None:
        """``static/`` 产物不在仓库里（.gitignore），镜像必须自己构建。"""
        source = _read(DOCKERFILE)
        assert "npm ci" in source
        assert "npm run build" in source
        assert "COPY --from=frontend" in source

    def test_bind_is_reachable_from_outside(self) -> None:
        """⚠️ gunicorn.conf.py 默认绑 127.0.0.1 —— 容器里必须覆盖成 0.0.0.0。"""
        assert "GUNICORN_BIND=0.0.0.0" in _read(DOCKERFILE)

    def test_runs_as_non_root(self) -> None:
        source = _read(DOCKERFILE)
        assert "useradd" in source
        assert "USER starwatt" in source

    def test_healthcheck_uses_healthz(self) -> None:
        source = _read(DOCKERFILE)
        assert "HEALTHCHECK" in source
        assert "/healthz" in source

    def test_data_lives_on_a_volume(self) -> None:
        source = _read(DOCKERFILE)
        assert "DORM_DATA_DIR=/data" in source
        assert 'VOLUME ["/data"]' in source

    def test_installs_runtime_requirements_only(self) -> None:
        """镜像里不装测试工具（requirements-dev 会拖进 pytest / mypy / ruff）。"""
        source = _read(DOCKERFILE)
        assert "requirements.txt" in source
        assert "requirements-dev" not in source

    def test_entrypoint_is_wired(self) -> None:
        source = _read(DOCKERFILE)
        assert 'ENTRYPOINT ["/app/docker-entrypoint.sh"]' in source
        assert "gunicorn" in source and "web:app" in source


class TestDockerignore:
    """镜像里绝不能有本地密钥与用户数据。"""

    @pytest.mark.parametrize(
        "pattern",
        [
            ".env",
            "records.db",
            ".venv",
            "node_modules",
            "static/assets",
            "static/index.html",
            "tests",
        ],
    )
    def test_excludes(self, pattern: str) -> None:
        assert pattern in _read(DOCKERIGNORE)


# ===========================================================================
# docker-compose.yml
# ===========================================================================
class TestCompose:
    def test_data_volume(self) -> None:
        source = _read(COMPOSE)
        assert "starwatt-data:/data" in source
        assert "starwatt-data:" in source.split("volumes:")[-1]

    def test_healthcheck_matches_the_image(self) -> None:
        source = _read(COMPOSE)
        assert "healthcheck:" in source
        assert "/healthz" in source

    def test_loopback_by_default(self) -> None:
        """默认只绑回环，前面挂 nginx；想直接暴露要显式改 BIND_ADDR。"""
        assert "${BIND_ADDR:-127.0.0.1}" in _read(COMPOSE)

    def test_every_env_has_a_default(self) -> None:
        """没有 ``.env`` 也要能 ``docker compose up -d``（否则新用户第一步就卡住）。"""
        source = _read(COMPOSE)
        assert "env_file:" not in source
        for key in ("FLASK_SECRET_KEY", "BOOTSTRAP_ADMIN_PASSWORD", "FLASK_PORT"):
            assert f"${{{key}:-" in source, key

    def test_hardening(self) -> None:
        source = _read(COMPOSE)
        assert "read_only: true" in source
        assert "no-new-privileges:true" in source
        assert "cap_drop:" in source
        assert "- /tmp" in source  # read_only 下 gunicorn 需要可写的 /tmp

    def test_restart_policy_and_single_replica(self) -> None:
        source = _read(COMPOSE)
        assert "restart: unless-stopped" in source
        assert "replicas: 1" in source  # 调度器是进程内单例

    def test_can_build_locally_when_no_image_is_published(self) -> None:
        """⚠️ 官方镜像还没发布时，`docker compose up -d` 必须仍然能跑。

        做法是同时给 ``image:`` 与 ``build:`` —— Compose 本地找不到镜像就现场构建。
        少了 ``build:``，新用户第一条命令就是「pull access denied」。
        """
        source = _read(COMPOSE)
        assert "image: ghcr.io/" in source
        assert "build:" in source and "context: ." in source

    def test_default_tag_matches_the_release_tag_scheme(self) -> None:
        """compose 的默认 tag 必须与 release.yml 产出的 tag 对得上。

        发布侧用 ``type=semver,pattern={{version}}``：推 ``v2.0.0-starcore``
        会产出 tag ``2.0.0-starcore`` —— 也就是 compose 里的默认值。
        这两处一旦漂移，用户 `docker compose up -d` 就会去拉一个不存在的 tag。
        """
        compose = _read(COMPOSE)
        default_tag = compose.split("${STARWATT_TAG:-", 1)[1].split("}", 1)[0]
        assert default_tag == "2.0.0-starcore", default_tag
        assert "type=semver,pattern={{version}}" in _read(RELEASE)

    def test_image_repository_matches_compose(self) -> None:
        """release.yml 推的镜像名必须与 compose 默认拉的一致 —— 否则用户拉不到。

        仓库 owner 里有大写字母（TSS-Small-sunshine），而 GHCR 只接受小写，
        所以 workflow 里必须小写化；这里同时钉住「包名」与「小写化」两件事。
        """
        assert "ghcr.io/tss-small-sunshine/starwatt:" in _read(COMPOSE)
        release = _read(RELEASE)
        assert "IMAGE_NAME: starwatt" in release
        assert "tr '[:upper:]' '[:lower:]'" in release

    def test_latest_tag_rule_uses_a_dashed_prerelease_marker(self) -> None:
        """`latest` 的启用条件必须带连字符判断预发布。

        踩过的坑：原本写的是「标签里不含 `rc`」—— 而本项目的版本代号是
        **starcore**，里面正好含 `rc`，于是 `latest` 永远打不上，
        而 `docker pull` 不带 tag 时默认就找 `latest`。

        ⚠️ 只看**代码行**：文件里有一条注释专门说明这个坑，
        纯文本 grep 会把那条注释本身当成违规（踩过两次了）。
        """
        code = [
            line
            for line in _read(RELEASE).splitlines()
            if not line.lstrip().startswith("#")
        ]
        body = "\n".join(code)
        assert "!contains(github.ref_name, '-rc')" in body
        assert "contains(github.ref_name, 'rc')" not in body.replace(
            "contains(github.ref_name, '-rc')", ""
        )

    def test_retag_workflow_preserves_multi_arch(self) -> None:
        """补标签必须走 imagetools（清单级复制）—— pull/tag/push 会丢多架构。"""
        retag = (PROJ / ".github" / "workflows" / "retag.yml").read_text(encoding="utf-8")
        assert "imagetools create" in retag
        assert "workflow_dispatch" in retag
        assert "packages: write" in retag

    def test_dockerfile_copy_sources_exist(self) -> None:
        """Dockerfile 里 COPY 的源路径必须真的存在（否则只有构建时才报错）。"""
        missing: list[str] = []
        for line in _read(DOCKERFILE).splitlines():
            stripped = line.strip()
            if not stripped.upper().startswith("COPY ") or "--from=" in stripped:
                continue
            parts = stripped.split()[1:]
            for source in parts[:-1]:  # 最后一个是目标
                if source in (".", "./"):
                    continue
                if not (PROJ / source.rstrip("/")).exists():
                    missing.append(source)
        assert not missing, f"Dockerfile COPY 的源不存在：{missing}"


# ===========================================================================
# docker-entrypoint.sh
# ===========================================================================
class TestEntrypoint:
    def test_strict_mode(self) -> None:
        assert "set -euo pipefail" in _read(ENTRYPOINT)

    def test_persists_secret_key_to_the_volume(self) -> None:
        """⚠️ 不持久化 = 每次重启换密钥 = 加密凭据永久解不开。"""
        source = _read(ENTRYPOINT)
        assert "SECRET_FILE" in source
        assert ".flask_secret_key" in source
        assert "chmod 600" in source

    def test_password_rule_covers_every_class(self) -> None:
        """生成的密码必须过后端强度校验，否则 bootstrap 建号会被拒。"""
        assert "Aa1!" in _read(ENTRYPOINT), "生成规则里必须含大写、数字、特殊字符"

    def test_no_sigpipe_trap(self) -> None:
        """``tr -dc ... </dev/urandom | head -c N`` 会因 SIGPIPE 打死脚本（pipefail）。

        ⚠️ 只看**代码行**：文件里有一条注释专门说明这个坑，
        用纯文本 grep 会把那条注释本身当成违规（踩过一次）。
        """
        code = [
            line
            for line in _read(ENTRYPOINT).splitlines()
            if not line.lstrip().startswith("#")
        ]
        assert not any("tr -dc" in line for line in code), "用了会触发 SIGPIPE 的写法"

    @needs_bash
    def test_syntax(self) -> None:
        result = subprocess.run([BASH, "-n", str(ENTRYPOINT)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    @needs_bash
    def test_first_run_generates_a_valid_password(self, tmp_path: Path) -> None:
        """真跑一遍：首启必须生成**能通过后端校验**的密码，并把密钥落盘。"""
        from starwatt.auth.password import validate_strength

        data = tmp_path / "data"
        env = dict(os.environ)
        env.update({"DORM_DATA_DIR": str(data), "DB_PATH": str(data / "records.db")})
        env.pop("FLASK_SECRET_KEY", None)
        env.pop("BOOTSTRAP_ADMIN_PASSWORD", None)

        first = self._run(env)
        assert "首次启动" in first
        assert "READY" in first, "必须 exec 交棒给 CMD"
        assert (data / ".flask_secret_key").is_file(), "密钥必须落盘到数据卷"

        password = ""
        for line in first.splitlines():
            if "初始密码" in line:
                password = line.split("：", 1)[-1].strip()
        assert password, first
        assert validate_strength(password) is None, validate_strength(password)

        # 第二次启动：数据库已存在 → 不再打印横幅，且复用同一把密钥
        (data / "records.db").write_text("", encoding="utf-8")
        key_before = (data / ".flask_secret_key").read_text(encoding="utf-8")
        second = self._run(env)
        assert "首次启动" not in second
        assert (data / ".flask_secret_key").read_text(encoding="utf-8") == key_before

    @needs_bash
    def test_explicit_password_is_respected(self, tmp_path: Path) -> None:
        data = tmp_path / "data"
        env = dict(os.environ)
        env.update(
            {
                "DORM_DATA_DIR": str(data),
                "DB_PATH": str(data / "records.db"),
                "BOOTSTRAP_ADMIN_PASSWORD": "MyOwn-Pass1!",
            }
        )
        env.pop("FLASK_SECRET_KEY", None)
        out = self._run(env)
        assert "MyOwn-Pass1!" not in out, "用户显式给了密码就不该再生成/打印一个"

    @staticmethod
    def _run(env: dict) -> str:
        result = subprocess.run(
            [BASH, str(ENTRYPOINT), "/bin/echo", "READY"],
            env=env,
            cwd=str(PROJ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout or ""


# ===========================================================================
# install.sh（裸机）
# ===========================================================================
class TestInstallScript:
    def test_strict_mode(self) -> None:
        assert "set -euo pipefail" in _read(INSTALL)

    def test_never_overwrites_env(self) -> None:
        """🔑 铁律 1：``.env`` 里的密钥丢了 = 所有加密凭据解不开。

        所以「写 .env」只能发生在「文件不存在」的分支里 —— 用位置判断：
        heredoc 必须出现在 ``沿用已存在的 .env`` 那句之后。
        """
        source = _read(INSTALL)
        assert 'if [ -f "$ENV_FILE" ]; then' in source
        assert "沿用已存在的 .env" in source
        assert source.index('cat >"$ENV_FILE"') > source.index("沿用已存在的 .env")

    def test_generates_both_secrets(self) -> None:
        source = _read(INSTALL)
        assert "FLASK_SECRET_KEY=$(_hex 32)" in source
        assert "BOOTSTRAP_ADMIN_PASSWORD=$NEW_PASSWORD" in source

    def test_password_rule_covers_every_class(self) -> None:
        assert "Aa1!" in _read(INSTALL)

    def test_substitutes_paths_into_the_unit(self) -> None:
        """单元模板里写死 /opt/... —— 必须按实际安装路径替换。"""
        source = _read(INSTALL)
        assert "s#/opt/dorm-power-monitor#$INSTALL_DIR#g" in source
        assert "s#^User=.*#User=$SERVICE_USER#" in source

    def test_keeps_existing_data(self) -> None:
        source = _read(INSTALL)
        assert "records.db" in source
        assert "零迁移" in source

    def test_checks_prerequisites(self) -> None:
        source = _read(INSTALL)
        assert "3, 10" in source  # Python ≥ 3.10
        assert "command -v systemctl" in source
        assert "id -u" in source  # 需要 root

    @needs_bash
    def test_syntax(self) -> None:
        result = subprocess.run([BASH, "-n", str(INSTALL)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    @needs_bash
    def test_help_works_without_root(self) -> None:
        result = subprocess.run(
            [BASH, str(INSTALL), "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode == 0
        assert "--upgrade" in (result.stdout or "")

    @needs_bash
    def test_unknown_flag_fails_loudly(self) -> None:
        result = subprocess.run(
            [BASH, str(INSTALL), "--nope"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        assert result.returncode != 0
        assert "未知参数" in (result.stdout or "")


# ===========================================================================
# 托管平台配置（Vercel / Netlify / EdgeOne / Fly / Render）
# ===========================================================================
V_PLATFORMS = {
    "vercel": PROJ / "vercel.json",
    "edgeone": PROJ / "edgeone.json",
    "netlify": PROJ / "netlify.toml",
    "fly": PROJ / "fly.toml",
    "render": PROJ / "render.yaml",
}


class TestPlatformDeployConfigs:
    """这些配置只有「部署到那家平台」时才会被执行 —— 本地跑不到，所以钉住契约。

    共同的硬约束（写错任何一条都会表现为「页面白屏」或「后端不工作」）：
      * 前端产物里资源引用带 `/static/` 前缀（vite 的 base 与 Flask 对齐），
        所以静态托管上必须把 `/static/*` 映射回根目录
      * SPA 兜底规则必须是**最后一条**，否则它会吃掉 /api 与静态资源
      * 后端需要持久磁盘 + 常驻进程，所以「全功能」只能在支持容器与卷的平台上
    """

    def test_all_present(self) -> None:
        for name, path in V_PLATFORMS.items():
            assert path.is_file(), f"缺少 {name} 的配置：{path.name}"

    # ---- JSON 平台：Vercel / EdgeOne ------------------------------------
    @pytest.mark.parametrize("name", ["vercel", "edgeone"])
    def test_json_platforms_parse_and_serve_static(self, name: str) -> None:
        import json

        data = json.loads(V_PLATFORMS[name].read_text(encoding="utf-8"))
        assert data["outputDirectory"] == "static"
        # 前端构建命令必须从仓库根跑（frontend/ 才是 npm 工程）
        assert "frontend" in data["buildCommand"]
        assert "frontend" in data["installCommand"]

    @pytest.mark.parametrize("name", ["vercel", "edgeone"])
    def test_json_platforms_map_static_prefix(self, name: str) -> None:
        import json

        rewrites = json.loads(V_PLATFORMS[name].read_text(encoding="utf-8"))["rewrites"]
        static_rules = [r for r in rewrites if r["source"].startswith("/static/")]
        assert static_rules, "缺少 /static/* 映射：JS/CSS/字体都会 404"
        assert static_rules[0]["destination"].startswith("/:"), static_rules[0]

    @pytest.mark.parametrize("name", ["vercel", "edgeone"])
    def test_json_platforms_spa_fallback_is_last(self, name: str) -> None:
        import json

        rewrites = json.loads(V_PLATFORMS[name].read_text(encoding="utf-8"))["rewrites"]
        assert rewrites[-1]["destination"] == "/index.html", "SPA 兜底必须放最后一条"

    def test_vercel_proxies_the_api(self) -> None:
        """Vercel 能把 /api 反代到外部后端 —— 浏览器只看到一个域名，Cookie 照常工作。"""
        import json

        rewrites = json.loads(V_PLATFORMS["vercel"].read_text(encoding="utf-8"))["rewrites"]
        api = [r for r in rewrites if r["source"].startswith("/api/")]
        assert api and api[0]["destination"].startswith("https://"), api

    def test_edgeone_does_not_pretend_to_proxy(self) -> None:
        """EdgeOne 的重写只对静态资源有效，**不能**反代外部后端。

        所以它的配置里不该出现 /api 规则 —— 假装能反代会让用户以为配好了，
        实际请求会打到静态站点上（404 或返回 index.html）。
        想在这上面接后端得写 Cloud Function，见 docs/DEPLOY_PAAS.md。
        """
        import json

        rewrites = json.loads(V_PLATFORMS["edgeone"].read_text(encoding="utf-8"))["rewrites"]
        assert not [r for r in rewrites if r["source"].startswith("/api")], (
            "EdgeOne 的 rewrites 不能反代外部后端"
        )

    # ---- TOML 平台：Netlify / Fly ---------------------------------------
    def test_netlify_toml(self) -> None:
        tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+")
        data = tomllib.loads(V_PLATFORMS["netlify"].read_text(encoding="utf-8"))
        assert data["build"]["publish"] == "static"
        assert "frontend" in data["build"]["command"]
        redirects = data["redirects"]
        assert redirects[-1]["to"] == "/index.html", "SPA 兜底必须放最后一条"
        assert any(r["from"].startswith("/api/") for r in redirects), "缺少 /api 反代"
        assert any(r["from"].startswith("/static/") for r in redirects), "缺少 /static 映射"

    def test_fly_toml_keeps_the_scheduler_alive(self) -> None:
        """⚠️ 调度器必须常驻：机器一停就不抓取、不发日报。

        `auto_stop_machines = true`（Fly 的默认值）在没流量时会把机器停掉 ——
        对普通网站没问题，对这个应用等于「静默失效」。
        """
        tomllib = pytest.importorskip("tomllib", reason="需要 Python 3.11+")
        data = tomllib.loads(V_PLATFORMS["fly"].read_text(encoding="utf-8"))
        assert data["http_service"]["auto_stop_machines"] is False
        assert data["http_service"]["min_machines_running"] == 1
        assert data["mounts"]["destination"] == "/data", "数据库必须有持久卷"
        assert data["env"]["DORM_DATA_DIR"] == "/data"
        assert data["http_service"]["checks"][0]["path"] == "/healthz"
        # 容器里必须绑 0.0.0.0，否则平台转发与探活都进不来
        assert data["env"]["GUNICORN_BIND"].startswith("0.0.0.0:")

    # ---- YAML 平台：Render（PyYAML 不是项目依赖，用结构断言）-----------
    def test_render_yaml(self) -> None:
        text = V_PLATFORMS["render"].read_text(encoding="utf-8")
        assert "runtime: docker" in text
        assert "dockerfilePath: ./Dockerfile" in text
        assert "healthCheckPath: /healthz" in text
        assert "mountPath: /data" in text, "数据库必须有持久磁盘"
        assert "plan: starter" in text, "Free 套餐不支持磁盘、且会休眠（休眠就不抓取）"
        assert "GUNICORN_BIND" in text and "0.0.0.0:" in text
        # Render 按 PORT(默认 10000) 转发与探活，容器要监听同一个端口
        assert 'value: "10000"' in text

    # ---- 所有平台共用：构建命令与产物目录 ------------------------------
    def test_build_command_matches_vite_output(self) -> None:
        """构建命令跑的是 frontend 的 npm script，产物落到 ../static。

        这三处（package.json 的 build、vite 的 outDir、平台配置的
        outputDirectory）任意一处漂移，平台就会部署一个空目录。
        """
        pkg = (PROJ / "frontend" / "package.json").read_text(encoding="utf-8")
        vite = (PROJ / "frontend" / "vite.config.ts").read_text(encoding="utf-8")
        assert '"build"' in pkg
        assert "outDir: '../static'" in vite


# ===========================================================================
# .github/workflows/release.yml
# ===========================================================================
RELEASE = PROJ / ".github" / "workflows" / "release.yml"


class TestReleaseWorkflow:
    """发布流水线的关键契约。

    这些点错了都不会在开发时暴露：镜像推不上去（大小写）、armv7 拖垮整个发布、
    离线包其实起不来。CI 里跑一次才几秒钟，比发版时才发现便宜得多。
    """

    def test_exists_and_triggers_on_tags(self) -> None:
        source = _read(RELEASE)
        assert "tags: ['v*']" in source
        assert "workflow_dispatch" in source

    def test_permissions_allow_packages_and_releases(self) -> None:
        source = _read(RELEASE)
        assert "packages: write" in source
        assert "contents: write" in source

    def test_image_name_is_lowercased(self) -> None:
        """GHCR 要求镜像名全小写，而仓库名里有大写字母（TSS-Small-sunshine）。"""
        assert "tr '[:upper:]' '[:lower:]'" in _read(RELEASE)

    def test_main_platforms_are_amd64_and_arm64(self) -> None:
        assert "platforms: linux/amd64,linux/arm64" in _read(RELEASE)

    def test_armv7_is_isolated_and_non_blocking(self) -> None:
        """RK5：armv7 构建失败不得拖垮发布（降级为「仅离线包」）。"""
        source = _read(RELEASE)
        assert "linux/arm/v7" in source
        assert "continue-on-error: true" in source
        assert "needs.build.result == 'success'" in source

    def test_runs_tests_before_building(self) -> None:
        source = _read(RELEASE)
        assert "needs: verify" in source
        assert "python -m pytest -q" in source
        assert "python -m scripts.secret_scan" in source

    def test_offline_bundle_is_really_booted(self) -> None:
        """「离线包可 docker load 并启动」这条 DoD 的证据只能是真起容器。"""
        source = _read(RELEASE)
        assert "docker load -i" in source
        assert "docker run -d" in source
        assert "/healthz" in source
        assert 'id="app"' in source  # 首页是 SPA → 前端产物确实进了镜像
        assert "初始密码" in source  # entrypoint 真的打印了随机密码

    def test_bundle_uses_a_named_volume(self) -> None:
        """容器内是非 root(10001)：宿主目录挂 /data 会写不进去，必须用卷。"""
        assert "-v starwatt-smoke:/data" in _read(RELEASE)

    def test_release_uploads_the_bundles(self) -> None:
        source = _read(RELEASE)
        assert "gh release create" in source
        assert "dist/*.tar.gz" in source

    def test_offline_bundle_carries_docs_and_installer(self) -> None:
        source = _read(RELEASE)
        for item in ("docker-compose.yml", ".env.example", "install.sh", "deploy/dorm-web.service"):
            assert item in source, item
