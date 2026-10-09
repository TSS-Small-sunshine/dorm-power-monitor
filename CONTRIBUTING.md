# 贡献指南

欢迎提 Issue 与 PR。这个项目是「宿舍自己搭一套」的小工具，所以**优先级**是：
能跑通 > 好维护 > 功能多。

---

## 一、环境搭建

需要 **Python ≥ 3.10**（CI 用 3.12）与 **Node ≥ 20**。

```bash
git clone https://github.com/TSS-Small-sunshine/dorm-power-monitor.git
cd dorm-power-monitor

# 后端
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt   # 含 pytest / ruff / mypy

# 前端
cd frontend && npm ci && cd ..
```

### 跑起来

```bash
# 后端（gunicorn，和线上一致 —— 不要用 Flask dev server，见下）
.venv/bin/gunicorn -c gunicorn.conf.py web:app        # http://127.0.0.1:5000

# 前端热更新（另开一个终端）
cd frontend && npm run dev                            # http://127.0.0.1:5173
```

`npm run dev`（Vite 5173）会把 `/api` 与 `/healthz` 代理到 5000，
所以开发时也能用**真实会话 Cookie**，前端不需要 mock 一套假数据。

> ⚠️ **不要用 `python web.py` 起服务**。Flask 的 reloader 会让调度器启动两次，
> 于是抓取与推送都会翻倍；CI 里有一条 AST 检查专门拦这个（见
> `tests/unit/test_web_entry.py`）。

### 首次启动

数据库不存在时会自动建表并补齐默认配置。管理员账号走**引导密码**：

```bash
export BOOTSTRAP_ADMIN_PASSWORD='Str0ng-Pass!'   # 或在 .env 里写
# 启动后用它登录，会被强制改密；该行会被自动从 .env 抹掉
```

没配引导密码也没关系 —— 首次访问会进 **OOBE 向导**，一路填完即可。

---

## 二、提交前必跑（与 CI 一致）

```bash
python -m pytest -q                      # 全量测试（约 1480 项）
python -m ruff check --no-cache .        # 静态检查（见下面的「假绿」警告）
python -m scripts.ast_guard              # 分层守卫 R1–R8
python -m scripts.secret_scan            # 凭据扫描
python -m mypy starwatt/                 # 类型检查（存量告警见下）
cd frontend && npm run build             # vue-tsc 类型检查 + 构建
```

### ⚠️ 本地「全绿」可能是假的：两个踩过的坑

CI 是在**全新 clone**（无缓存、无本地残留）上跑的，而开发机上这两样东西
都会让本地结论和 CI 不一致：

1. **过期的 `.ruff_cache`**
   缓存条目按「文件路径 + mtime + size」复用。升级过 ruff 版本之后，旧版本
   写的「干净」结论会被新版本直接复用 —— 于是本地报 `All checks passed!`，
   CI 却报错。**对不上时先 `rm -rf .ruff_cache`，或直接加 `--no-cache`。**

2. **残留的旧目录会改变导入分类**
   ruff 的 isort 按「本地是否存在同名模块」分类。legacy 留下的
   `db/__pycache__/`（未跟踪，但目录存在）会让 `import db` 被判成
   first-party，分组随之改变 —— 同一个文件、同一版 ruff，两边结论不同。
   现在 `pyproject.toml` 里已经**显式**写了 `known-first-party = ["starwatt"]`
   把它钉死，但**删掉残留目录**仍然是更好的做法。

**结论：本地全绿不等于 CI 全绿。** 改完分发/构建相关文件后，最可靠的验证是

```bash
git clone --depth 1 --branch <你的分支> <远端> /tmp/verify && cd /tmp/verify
python -m ruff check --no-cache . && python -m pytest -q
```

`mypy` 目前有 **9 个存量错误**（都在 M1–M3 的旧模块），新增代码**不应**
增加这个数字；顺手修掉是受欢迎的。

---

## 三、代码约定

### 分层（有守卫强制）

```
domain  →  config_registry / db / auth  →  scraper / notify  →  services  →  web  →  web.py
```

只能向上依赖。`scripts/ast_guard.py` 会在 CI 里拦下：

| 规则 | 内容 |
|---|---|
| R1 | `starwatt/domain/**` 禁止 IO（`requests` / `flask` / `sqlite3` / `config_registry`） |
| R4 | 禁止回引旧顶层模块（`web` / `db` / `feishu_bot` / `dorm_power`） |
| R5 | 禁止跨模块 import 下划线私有符号 |
| R7 | 禁止裸用 `datetime.now()` / `date.today()` —— 统一走 `starwatt.timeutil.now_cst()` |
| R8 | 禁止 UTF-8 BOM |

### 风格

* **注释与 docstring 用中文**，并且写**为什么**（踩过的坑、约束来自哪条需求），
  不要复述代码在做什么。
* 面向用户的文案（错误提示、卡片文本）要**逐字**保持与既有契约一致 ——
  它们被 `tests/regression/fixtures/` 冻结了。
* 命名：模块/函数 snake_case，类 PascalCase；配置键与 JSON 字段也是 snake_case。
* ruff 行宽 100（`E501` 已忽略，但别写超长行）。

### 时间

所有「现在」都必须经 `starwatt.timeutil.now_cst()`（Asia/Shanghai）。
裸用 `datetime.now()` 在容器里会变成 UTC，日报会错一天。

---

## 四、测试要求

* **新功能必须带测试**。测试放在 `tests/unit/`（快、无网络）或
  `tests/integration/`（跨模块、可用临时 SQLite）。
* **不要改 `tests/regression/fixtures/` 来让测试变绿**。那些 fixture 是
  M0 阶段在**旧代码上真实运行**产出的行为基准（卡片 JSON、API 字段集、
  算法向量、表结构、45 项隐性行为）。改它们等于改需求 —— 请先确认那确实是
  有意的行为变更，并在 PR 里说明。
* **部署产物也有契约测试**（`tests/unit/test_deploy.py`）：Dockerfile、
  compose、entrypoint、install.sh、release.yml 的关键不变量都被钉住了。
  改这些文件时先看测试想表达什么。
* 外部 HTTP 一律 mock。`tests/conftest.py` 提供了 `no_network` fixture
  （把 `requests` 的 get/post 换成抛异常），**需要时在测试签名里声明**它，
  就能保证这条测试永远不会偷偷发真实请求。

---

## 五、提交与分支

* 分支：`main`（稳定）/ `rewrite/*`（大改）/ `feat/*`、`fix/*`。
* 提交信息用 **Conventional Commits**，说明里写清「为什么这么改」：

  ```
  fix(oobe): keep the wizard on the current step when a save fails

  207 used to advance the flow, so the error was invisible and completion
  could be marked even when dorm_openid was never written.
  ```

* 一个 PR 做一件事。涉及以下内容时请在描述里点明：
  * 改动了用户可见文案（会被契约测试拦住）
  * 改动了数据库结构（只增不删）
  * 改动了配置键（老用户的 `.env` / `meta` 表要兼容）

---

## 六、安全

* **绝不提交** `.env`、`records.db`、任何真实 openid / webhook / 密钥。
  `python -m scripts.secret_scan` 会扫，CI 也会扫。
* 测试里用的凭据必须是明显的假值（`Str0ng-Pass!`、`https://example.com/...`）。
* 新加的 `secret` 类配置项必须用 `T.SECRET`（加密存储 + 日志脱敏），
  用 `T.STR` 存凭据等同于明文入库。
* 涉及认证、CSRF、SSRF 的改动请在 PR 里单独说明，并附上测试。

---

## 七、目录导览

```
starwatt/
  config.py            启动配置（.env 的 4 项）+ 加载
  config_registry/     94 项业务配置的声明式注册表（加配置只改这里）
  db/                  连接 / 模型 / 仓储 / 迁移
  domain/              纯计算（无 IO，好单测）
  scraper/             学校接口端点 + SSRF 防护
  notify/              飞书 / QQ / 卡片 / 告警策略 / 命令
  services/            业务编排（dashboard / admin / oobe / setup / backup）
  auth/                会话 / CSRF / 密码（scrypt）/ 装饰器
  web/                 Flask 蓝图 + SPA 托管
  scheduler.py         进程内调度（APScheduler）
  flags.py             18 个功能开关（父子 + 抑制原因）
  logging_setup.py     7 级 × 8 类日志
  timeutil.py          Asia/Shanghai 的唯一时间来源
web.py                 gunicorn 入口（调度器在 worker 里起）
frontend/              Vue 3 + Vite + Tailwind（产物落到 static/）
scripts/               ast_guard / secret_scan
tests/                 unit / integration / regression（契约快照）
```

---

## 八、发版

一次发版只有三步（其余全由 CI 做）：

```bash
# 1. 把版本号写进三处（新用户 `docker compose up -d` 拿到的就是它）
#    starwatt/__init__.py:      __version__ = "2.0.2"
#    pyproject.toml:            version = "2.0.2"
#    docker-compose.yml:        ${STARWATT_TAG:-2.0.2-starcore}
#    tests/unit/test_deploy.py: 期望值同步改（这条测试就是防漂移的）
python -m pytest tests/unit/test_deploy.py -q

# 2. 提交并推 main
git commit -am "chore(release): 2.0.2" && git push

# 3. 打 tag —— 这一步才真正触发构建与发布
git tag -a v2.0.2-starcore -m "StarWatt 2.0.2 星核 / Star Core"
git push origin v2.0.2-starcore
```

推完 tag 后 CI 会：跑测试（verify）→ 构建 amd64 + arm64 → 构建 armv7（失败不阻断）→
导出镜像与离线包并**真起容器冒烟** → 建 Release。

**tag 命名**：必须 `v` 开头，且是合法 semver（`v2.0.2-starcore` 里的 `-starcore`
被当作 prerelease 后缀，`latest` 仍会打上；带 `-rc` / `-beta` / `-alpha` 的则不会）。

**镜像标签**会自动生成：`2.0.2-starcore`、`v2.0.2-starcore`、`latest`，
以及 armv7 专用的 `2.0.2-starcore-armv7`。

> ⚠️ **没有** `2.0` 这种主次版本标签：`-starcore` 让版本号按 semver 算 prerelease，
> 而 docker metadata 对 prerelease 只生成完整版本号 + `latest`，
> 跳过 `major.minor` 规则（实测确认过，见 `docs/DEPLOY.md` 的标签表）。

### 发错了怎么办

* 只错在标签（例如忘了 `latest`）：合并后用 **Actions → Retag** 手动跑一次，
  它用 `docker buildx imagetools create` 在清单层面复制引用（几秒，保留多架构）
* 错在代码：修好后打**新** tag（不要移动已发布的 tag —— 别人的镜像可能已经拉过了）
* 已发布的 Release 可以编辑说明，产物用 `gh release upload --clobber` 覆盖

## 九、想扩展什么

`docs/EXTENDING.md` 里给了六类扩展的分步 SOP（配置项 / 通知渠道 /
适配其他学校 / 告警层 / 机器人命令 / 前端页面），每类都写明了改哪几个文件、
会被哪条测试拦住。**加一个配置项只需要改 1 个文件**。
