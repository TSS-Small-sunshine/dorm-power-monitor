# 星瓦 StarWatt · 宿舍电量监控

> **v2.0.0 星核 / Star Core** ← 当前（重写分支 `rewrite/r69`）
> 上一个已发布版本：`v1.1.0 微光 / Glimmer`（R60–R68）
> 版本与代号体系见 [`docs/NAMING.md`](docs/NAMING.md)

看宿舍还剩多少电、每天用多少、这个月大概要交多少钱；电量过低 / 违规 / 电表离线时
自动推送到飞书群，也能在飞书或 QQ 里私聊机器人查状态。

---

## 📊 重写进度（R69）

整个项目正在按 [`docs/REWRITE_PLAN.md`](docs/REWRITE_PLAN.md) 从零重写（破坏性，
所以版本号进 MAJOR）。**当前分支已可运行**，只是分发与切换还没做：

| 里程碑 | 状态 | 内容 |
|---|---|---|
| **M0** 基线 + 契约快照 | ✅ | 45 项隐性行为快照 + 5 类 fixtures（schema / 算法 / 卡片 / API / 行为）；旧代码全删 |
| **M1** 基础层 | ✅ | 日志（7 级别 + 8 类别 + 脱敏）、配置注册表（94 项 / 9 分组）、功能开关、数据层、认证 |
| **M2** 抓取 | ✅ | 学校接口 F1–F5、SSRF 防护、**进程内调度器**（不再需要 cron）、gunicorn + systemd |
| **M3** 通知 | ✅ | 飞书群卡片 / 飞书私聊 9 命令 / QQ 官方机器人；18 个开关全部真正生效 |
| **M4** API 与配置 | ✅ | 27 个业务端点、访问控制、配置中心 API、用户管理、审计 |
| **M5** 前端 SPA | 🔄 **约 75%** | 四段式仪表盘 ✅、管理后台 4 页 ✅；**剩** OOBE 向导页、功能开关页、日志设置页 |
| **M6** 分发与交付 | ⬜ | Dockerfile / compose / 三架构 release / `install.sh` / 文档重写 / 截图 |
| **M7** 生产切换 | ⬜ | 停旧 → 起新 → 验证 → 回滚演练 |

**现在就能用的完整链路**：定时抓取 → 落库 → 仪表盘（概览 / 历史 / 违规 / 电表）
→ 告警推送（飞书群 / 飞书私聊 / QQ）→ 配置中心里改一切 → 用户与审计管理。

**还没做的**：Docker 与一键安装脚本（M6）、OOBE 首次向导页与开关/日志页（M5 尾巴）。
在那之前，首次配置请按下面的「快速开始」手动做一次。

质量门禁现状：`pytest` **1359 passed** · `ruff` 0 · AST 守卫通过 · 凭据扫描通过 ·
`mypy starwatt/` 9 项历史遗留（均在安全敏感模块，未为纯类型修饰改动）·
契约快照 `tests/regression` 47 项全绿。

---

## ✨ 特性

| | |
|---|---|
| **四段式仪表盘** | 概览（剩余电量 + 趋势图 + 4 张统计卡）/ 历史（每日用电 + 采集记录 + 区间筛选）/ 违规 / 电表 |
| **配置中心** | 94 个配置项全部在网页上改，**不用重启**、不用碰 `.env`；secret 加密存储 + 脱敏显示；支持导出 / 导入（整机迁移） |
| **功能开关** | 群推送 / 私聊机器人 / QQ 机器人 3 个总开关 + 8 个告警层 + 7 个命令开关，关闭即**静默降级**（不报错、留 NOTICE 日志） |
| **告警** | L1 低电 / L2 摘要 / L3 日报周报月报 / L4 电表离线 / 违规 / 数据陈旧；静默时段；去重与冷却 |
| **机器人** | 飞书私聊与 QQ 支持 `/状态 /电表 /今日 /历史 /缴费 /违规 /帮助`（可逐个开关） |
| **认证** | 会话 Cookie + CSRF，**没有浏览器原生 Basic Auth 弹窗**；首登强制改密；失败锁定 |
| **无外部依赖** | 不需要 cron、不需要 Redis、不需要外网 CDN；字体与 Chart.js 全部本地化 |
| **可移植** | 一份 SQLite + 一个 `.env`（只有 4 项启动必需）；架构兼容 amd64 / arm64 / **armv7**（通知模块是纯 Python Ed25519） |

---

## 🚀 快速开始（开发）

**前置**：Python ≥ 3.10、Node ≥ 20（前端）。Linux / macOS / Windows 都可以。

### 1. 安装依赖

```bash
git clone https://github.com/TSS-Small-sunshine/dorm-power-monitor.git
cd dorm-power-monitor

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cd frontend && npm install && cd ..
```

### 2. 配置 `.env`

`.env` **只有 4 项启动必需的配置**，其余全部在网页里改：

```bash
cp .env.example .env
chmod 600 .env
python -c "import secrets; print(secrets.token_hex(32))"   # 生成 FLASK_SECRET_KEY
```

| 变量 | 说明 |
|---|---|
| `DB_PATH` | SQLite 路径（相对路径按**项目根**解析） |
| `DORM_DATA_DIR` | 数据根目录（DB / 备份 / 日志）；留空 = 项目根 |
| `FLASK_PORT` | 监听端口（默认 5000） |
| `FLASK_SECRET_KEY` | 🔒 会话签名密钥，**同时用于加密配置中心里的 secret**；丢失后需重新填写凭据 |

### 3. 首次启动（建表 + 建号）

首次启动需要一个**一次性**的管理员密码（用后即焚，见 `docs/BLOCKER_FIXES.md` B4）：

```bash
# 临时加到 .env 末尾（随便一个强密码）
echo "BOOTSTRAP_ADMIN_PASSWORD=$(python -c 'import secrets; print(secrets.token_urlsafe(16))')" >> .env
tail -1 .env                       # 记下这个密码，下一步登录用

# 启动（开发与生产都用 gunicorn —— web.py 里没有 app.run()）
gunicorn -c gunicorn.conf.py web:app
```

启动时会自动建表、建号，并**立即把 `BOOTSTRAP_ADMIN_PASSWORD` 从 `.env` 里删掉**，
同时把该账号标记为「首登必须改密」——所以第一次登录后会被强制设置新密码。

> **为什么开发也用 gunicorn**：Flask 自带的 dev server 会 fork 一个 reloader 子进程
> 再导入一次应用，而抓取调度器是**进程内单例** —— 那样会变成「两个进程各抓一次」，
> 且不报任何错。用同一份 `gunicorn.conf.py`（`workers=1` + `preload_app=False`）
> 让 dev 与 prod 行为完全一致。改代码后需要热重载时加 `--reload`。

### 4. 前端

```bash
# 开发：热更新 + 把 /api 代理到 127.0.0.1:5000
cd frontend && npm run dev          # 打开 http://localhost:5173

# 或者：构建产物落到 static/，由后端直接送出
cd frontend && npm run build        # 然后访问 http://localhost:5000
```

### 5. 首次配置

浏览器打开 → 登录（用上一步的密码）→ **强制改密** → 在「管理 → 配置中心」里填：

1. **数据采集**：`openid`（[怎么拿](#-如何获取-openid)）、学校接口地址
2. **站点**：站点名称、主题色
3. **推送（群）**：飞书群机器人 webhook（可选）
4. **机器人（私聊）**：飞书应用凭据 / QQ 机器人凭据（可选）

填完点「保存」，回仪表盘点一次「刷新」即可看到数据。
配置错在哪一目了然：**管理 → 连通测试** 会真的发一条测试消息。

---

## 📦 生产部署

```bash
# 1. 代码 + 依赖（同「快速开始」1、2 步）

# 2. 构建前端产物
cd frontend && npm ci && npm run build && cd ..

# 3. systemd
sudo cp deploy/dorm-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dorm-web
sudo journalctl -u dorm-web -f

# 4. 反向代理（可选）：nginx / Caddy 指向 127.0.0.1:5000
#    ⚠️ 生产请开 HTTPS —— 会话 Cookie 在 HTTPS 下才带 Secure
```

要点：

- **不需要 cron**：抓取由进程内调度器驱动（默认 10 分钟一轮），systemd 起来即可
- **必须 `workers=1` + `preload_app=False`**：`gunicorn.conf.py` 已配好并有测试守卫，
  改错会导致「调度器起两次」或「fork 后不起」
- 调度器由 `gunicorn.conf.py` 的 `post_fork` 在 **worker 进程**内启动，
  并把 arbiter 的 PID 传给 `web.start_scheduler_once()` 用于守卫
  （gunicorn 的调用顺序是 `post_fork` → `load_wsgi`，所以「模块导入时的 PID」
  **不能**用来判断 master/worker）
- 生产建议 `DB_PATH=/var/lib/dorm-power-monitor/records.db` 并让服务用户可写

> Docker / `docker compose` / 一键 `install.sh` / 离线包：**M6 待做**。
> 在那之前请按上面的裸机步骤部署。

---

## 📂 项目结构

```
dorm-power-monitor/
├── starwatt/                     # Python 包（产品名做包名）
│   ├── domain/                   # ★ 纯计算：算法与阈值（零 IO，AST 守卫强制）
│   ├── config_registry/          # ★ 配置注册表：94 项元数据 + 加密存储 + 导入导出
│   ├── db/                       # schema / 迁移 / 仓储（SQLite + WAL）
│   ├── auth/                     # 密码 / 会话 / CSRF / 装饰器 / bootstrap
│   ├── scraper/                  # 学校接口 F1–F5 + SSRF 防护 + 一轮抓取编排
│   ├── notify/                   # 飞书（群/私聊）+ QQ + 卡片 + 渲染 + 策略
│   ├── services/                 # 用例编排（Web 与调度器共享）
│   ├── web/                      # Flask 工厂 + 7 个蓝图 + 访问控制
│   ├── scheduler.py              # 进程内调度器（替代 cron）
│   ├── flags.py                  # 18 个功能开关
│   └── logging_setup.py          # 7 级别 + 8 类别 + 自动脱敏
├── frontend/                     # Vue 3 + Vite + TS + Tailwind（构建产物落 static/）
├── static/                       # 前端产物 + 字体（后端直接送出）
├── tests/                        # unit / integration / regression（契约快照）
├── scripts/                      # ast_guard.py（分层守卫）/ secret_scan.py
├── deploy/                       # systemd 单元
├── docs/                         # 需求 / 计划 / 报告
├── web.py                        # gunicorn 入口（web:app）+ 启动钩子
└── gunicorn.conf.py              # workers=1 / preload_app=False（有测试守卫）
```

**分层方向**（由 AST 守卫 R1/R4/R5/R7/R8 强制，违反即 CI 红）：

```
domain（纯计算，零 IO）
   ↑
config_registry · db · auth
   ↑
scraper · notify
   ↑
services
   ↑
web（Flask 蓝图）
   ↑
web.py（gunicorn 入口）
```

---

## 🛠️ 开发

### 跑测试与门禁

```bash
python -m pytest -q                       # 1359 项
python -m ruff check .                    # 静态检查（含行长）
python -m scripts.ast_guard               # 分层守卫 R1/R4/R5/R7/R8
python -m scripts.secret_scan             # 凭据扫描（防误提交）
python -m mypy starwatt/                  # 渐进严格（9 项历史遗留）
python -m compileall -q -x '(\.venv|__pycache__|\.git|node_modules)' .

cd frontend && npm run build              # 含 vue-tsc 类型检查
```

### 契约快照（重写不丢功能的唯一防线）

`tests/regression/fixtures/*.json` 是 **M0 在旧代码上真实运行**产出的基准，
新实现必须逐项一致：`schema`（表/字段/索引名）、`algorithms`（本月预计 / 日均 /
1 小时差值）、`cards`（6 种卡片 JSON）、`api`（`/api/*` 字段集）、`behaviors`
（45 项隐性行为）。**改动这些 fixtures 等同于改契约**，需要先确认。

### 加一个配置项（前端零改动）

只改 `starwatt/config_registry/registry.py` 一个文件：

```python
cfg("my_option", "我的选项", GROUP, T.INT, 10,
    help="说明文字会显示在配置中心",
    validate="1..100"),
```

刷新页面即出现对应控件（类型 → 控件的映射见 `frontend/src/components/config/ConfigField.vue`）。

### 加一个通知渠道

实现 `starwatt/protocols.py` 的 `Notifier` 协议，然后在
`starwatt/notify/channels.py` 注册一行即可（见该模块 docstring）。

---

## 📋 配置

**分两处，边界很清楚：**

| 位置 | 放什么 | 怎么改 |
|---|---|---|
| `.env` | **只有启动必需的 4 项**（DB 路径 / 数据目录 / 端口 / 会话密钥）+ 首启的一次性 `BOOTSTRAP_ADMIN_PASSWORD` | 编辑文件后重启 |
| **配置中心**（网页） | 其余**全部**：学校凭据 / 飞书与 QQ 凭据 / 推送开关与时段 / 告警阈值 / 抓取节流 / 认证策略 / 日志级别 | 管理 → 配置中心，**不用重启** |

配置中心的能力：

- **脱敏**：secret 一律显示为 `••••1234`，只在真正改动时提交（原样保存不会覆盖真凭据）
- **加密存储**：secret 用 `FLASK_SECRET_KEY` 派生的密钥 AES-GCM 加密后存 SQLite
- **条件显示**：父开关关闭时子项自动隐藏（避免改了没用的项）
- **逐项校验**：一次保存里失败的项会带中文原因留下，成功的项照常生效
- **导出 / 导入**：默认脱敏导出；整机迁移时用「导出（含凭据）」+ 导入时勾选「导入接受凭据」
- **恢复默认**：只重置配置，**不动**抓取进度等运行时状态

---

## 🔑 如何获取 openid

1. **微信**里打开宿舍电费小程序 / H5 页面
2. 复制微信内嵌浏览器地址栏的完整 URL，形如：
   ```
   https://<学校域名>/campus/webchat/dormEmRealRead/finduser?openid=oV3xX5-xxxxxxxx
   ```
3. 把 `oV3xX5-xxxxxxxx` 这一段（**仅 openid 的值**）填到
   **管理 → 配置中心 → 数据采集 → openid**

> 也可以直接把整条 URL 粘到「自助配置」页（`/api/setup/parse-url`），
> 它会自动解析出 openid / roomId，并校验域名是否与已配置的学校地址一致。

⚠️ **openid 等同于你宿舍电费的密码**。泄露后：解绑并重新绑定微信账号 → 学校会发
新的 openid；只改配置不会让旧值失效。

---

## 🤖 机器人配置

三套东西**互相独立**，按需配：

### A. 飞书群机器人（告警推送）

最省事的一条：只要一个 webhook，就能收到电量告警卡片。

1. 在目标群里：设置（`…`）→ 群机器人 → 添加机器人 → **自定义机器人**
2. 复制 **webhook URL**（可选：开启「签名校验」并复制密钥）
3. 填到 **配置中心 → 推送（群）**：`飞书群 webhook` / `签名密钥`
4. 到 **管理 → 连通测试 → 飞书群机器人** 点一次，群里应立刻收到测试卡片

推送开关与时段（L1 低电 / L2 摘要 / L3 日报周报月报 / L4 离线 / 违规 / 陈旧、
静默时段）都在 **配置中心 → 推送（群）** 里；总开关关闭后是**静默降级**：
不发消息、记一条 NOTICE 日志，不会报错刷屏。

### B. 飞书私聊机器人（9 个命令）

让舍友在飞书里私聊机器人发 `/状态`、`/电表` 等：

1. [飞书开放平台](https://open.feishu.cn/app) → **创建企业自建应用**
2. **凭证与基础信息** → 复制 `App ID` / `App Secret`
3. **权限管理** → 开通：接收消息、发送消息、上传图片（`im:message`、`im:resource` 等）
4. **事件订阅** → 请求地址填 `https://<你的域名>/feishu/event`；
   复制 `Verification Token`（开启加密则再复制 `Encrypt Key`）
5. **机器人** → 启用机器人能力；**版本管理与发布** → 发布并等待审批
6. 填到 **配置中心 → 机器人（私聊）**：App ID / App Secret / Verification Token / Encrypt Key
7. 打开 **机器人总开关**（默认关闭，避免没配好就报错）

命令与开关：`/状态 /剩余 /电表 /今日 /历史 [N] /缴费 /违规 /帮助`，
每一个都能单独禁用（禁用后回「该命令已禁用」并记 NOTICE）。

### C. QQ 官方机器人（可选）

1. [QQ 开放平台](https://q.qq.com/) → 创建机器人 → 拿到 `AppID` / `AppSecret`
2. **开发设置** → 复制 **Bot Secret**（用于回调签名，**不是** AppSecret）
3. 回调地址填 `https://<你的域名>/qq/events`（需要公网 HTTPS）
4. 填到 **配置中心 → 机器人（QQ）**，并打开 **QQ 机器人总开关**
5. 在 QQ 里给机器人发一句话，它会把该会话记为目标（之后告警也能推 QQ）

> 回调验签是 **fail-closed** 的：密钥没配好时一律 401，绝不会「先处理再补验」。

---

## 🏫 适配其他学校

学校接口的路径与字段集中在 `starwatt/scraper/endpoints.py`，适配分两种情况：

**方法一：同厂商系统**（"易班" / "完美校园" 等 H5，路径结构一致）

只改配置即可：**配置中心 → 数据采集 → 学校接口地址** 填新域名，
再把从新学校 H5 页面拿到的 openid 填进去。

**方法二：完全不同的系统**

1. 在 `starwatt/scraper/endpoints.py` 里照着现有端点实现新的
   `Endpoint`（`key` / `interval_sec` / `path` / `parse()`）
2. 需要新字段时同步改 `starwatt/db/models.py` 与 `schema.py`
   （迁移写进 `starwatt/db/migrations.py`，**只增不删**）
3. 纯计算部分放进 `starwatt/domain/`（那里禁止 IO，便于单测）
4. 跑 `pytest` 与 `scripts.ast_guard` 确认没破坏分层

---

## 🐛 故障排查

### 抓不到数据 / 仪表盘一直是旧数据

```bash
# 1. 看日志（类别标签就是给你过滤用的）
sudo journalctl -u dorm-web -f | grep '\[scrape\]'

# 2. 看抓取状态（配置中心里也能看到 last_scrape_status）
python -c "from starwatt.db.connection import init; init(); \
from starwatt.config_registry import get_str; \
print(get_str('last_scrape_status'), get_str('last_scrape_at'))"

# 3. openid 失效了？重新走一次微信里的电费页面，看 URL 里的 openid 是否变了
# 4. 页面上点一次「刷新」（管理 → 连通测试 / 仪表盘右上角），看返回的中文原因
```

**不要再去查 cron** —— R69 起抓取由进程内调度器驱动，`crontab -l` 里那行
（如果你有）应该删掉，否则会和调度器抢着抓。

### 飞书/QQ 收不到消息

1. **管理 → 连通测试** 点对应按钮 —— 这是最快的判断方式（会真的发一条）
2. 群里没收到：webhook 是否正确、机器人是否被移出群
3. 私聊不回复：应用是否**已发布并通过审批**、事件订阅地址是否可达、
   `Verification Token` 是否填对
4. 都正常但没消息：检查 **功能开关** 里的总开关与分项开关（关闭是静默的，
   只会在日志里留一条 NOTICE：`grep NOTICE`）

### 打不开网页 / 500

```bash
sudo systemctl status dorm-web          # 进程在不在
sudo ss -tlnp | grep 5000               # 端口在不在听
curl -s localhost:5000/healthz          # 探活（不碰数据库）
sudo journalctl -u dorm-web -n 100      # 看错误
```

### 登录不上

- **忘记密码**：在服务器上重置（没有、也不会有「无认证的网页重置入口」——
  那是旧版的安全漏洞）：

```bash
python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码", role="admin")   # 建一个新管理员
print("已创建 admin2，登录后可删除旧账号")
PY
```

- **被锁定**（连续失败）：等 15 分钟，或清空失败计数
  `python -c "from starwatt.db.connection import init; init(); \
   from starwatt.db.repositories import FailedAttemptRepo; ..."`（见 `auth/service.py`）

### PNG 卡片中文变方框（口口口）

字体在 `static/fonts/`（仓库自带 MiSans）。确认文件存在：

```bash
ls -la static/fonts/MiSans-*.ttf
```

### 想彻底重来

```bash
sudo systemctl stop dorm-web
mv records.db records.db.bak        # 先备份！
# 重新启动即可：程序会自动建表，再走一次首启建号（见「快速开始」第 3 步）
sudo systemctl start dorm-web
```

> 只想重置**配置**（保留历史数据）：配置中心右上角「恢复默认」。

---

## 📚 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/REQUIREMENTS.md`](docs/REQUIREMENTS.md) | 需求与 23 个决策点（Q1–Q23） |
| [`docs/REWRITE_PLAN.md`](docs/REWRITE_PLAN.md) | 重写计划 M0–M7 + 验收标准（DoD） |
| [`docs/FEATURE_INVENTORY.md`](docs/FEATURE_INVENTORY.md) | 旧版 105 项功能清单（必留 / 改 / 删 / 新增） |
| [`docs/BLOCKER_FIXES.md`](docs/BLOCKER_FIXES.md) | 12 个阻塞项（B1–B12）的处置方案 |
| [`docs/SCOPE_DECISION.md`](docs/SCOPE_DECISION.md) | 范围决策与理由 |
| [`docs/NAMING.md`](docs/NAMING.md) | 版本号与「星 × 电」代号体系 |
| [`docs/reports/`](docs/reports/) | 里程碑报告（`m0_REPORT.md`、`m4_REPORT.md`） |

---

## 🤝 贡献

1. 先读 `docs/REWRITE_PLAN.md` 的分层规则 —— 它由 `scripts/ast_guard.py` 强制：
   `domain/` 禁止 IO、禁止跨模块引用私有符号、禁止裸用 `datetime.now()`
2. 提交前跑一遍「开发 → 跑测试与门禁」里的全部命令（CI 会跑同样的检查）
3. **不要改契约快照**（`tests/regression/fixtures/`）来让测试变绿 ——
   那是旧代码的真实行为基准，改它等于改需求
4. 提交信息用 `feat(mN): ...` / `fix: ...` 风格，并在正文里说明「为什么」

---

## 📜 License

MIT（见 [`LICENSE`](LICENSE)）。

## 🙏 致谢

- 字体：[MiSans](https://hyperos.mi.com/font)（小米）、NotoEmoji（Google）
- 图表：[Chart.js](https://www.chartjs.org/)
- 版本代号叙事「一粒尘埃，如何照亮一间宿舍」来自本项目
