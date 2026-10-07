# 重写方案（REWRITE PLAN）

> **状态**：草案 v5（待评审）
> **需求基线**：`REQUIREMENTS.md` v1.4（**22 项决策** + 3 项修复决策 D1/D2/D3）
> **功能基线**：`SCOPE_DECISION.md`（50 必留 / 52 改 / 3 删 + **20 新增**）
> **现状基线**：`FEATURE_INVENTORY.md`（105 项）+ 仓库 `develop` @ `bb031f6`
> **审查基线**：`PLAN_AUDIT.md`（5 阻断 **已全部关闭** + 5 严重 + 7 中等）
> **修复基线**：`BLOCKER_FIXES.md`（B1–B5 修复方案）
> **执行基线**：**§0.1 分支与文件策略（Q22）**
> **工期**：**38 天**（乐观）/ 45（现实）/ 55（含返工）
> **编写日期**：2026-10-06
> **本文档不含任何代码改动**

---

## 0. 一句话概述

> 把当前的「4 个大单体文件 + 服务端渲染 + host cron + 手动 zip 部署」的**单人自用系统**，
> 重写为「**分层架构 + Vue SPA + 容器内调度 + 多架构 Docker 分发 + 全配置中心化**」的**可交付多实例产品**。

**不变**：SQLite schema、时间戳格式、meta 键名、飞书卡片契约、核心算法、45 项隐性行为
**变**：代码结构、前端、测试体系、调度方式、打包方式、访问控制、**配置管理**、**日志体系**、**认证 UI**、文档

**v2/v3 新增的四块基础设施**：

| 新增 | 作用 | 来源 |
|---|---|---|
| **配置注册表** | 每一个配置项都能在 WebUI 改（含阈值、节流、凭据、日志级别） | Q15 |
| **功能开关体系** | 飞书机器人 / 群推送 / 9 个告警层 / 9 个命令，全部可开关且**关闭即静默降级** | Q16 |
| **日志子系统** | 7 个严重级别 + 8 个业务类别 + 模块级覆盖 + 文本/JSON 双格式 | Q20 |
| **认证 UI 体系** | **彻底移除浏览器原生 Basic Auth**，6 个认证界面统一设计 | Q21 |

---

## 0.1 分支与文件策略（Q22）🌿

**用户要求**：**从头开始做，拉全新分支，保证该分支的文件干净。**

### ① 关键顺序约束（不可颠倒）⚠️

```
① 落库 R61–R67 的 24 项到 develop        ← 否则永久丢失（唯一不可逆风险）
        ↓
② 创建 rewrite/r69 分支（从 develop 切出）
        ↓
③ commit #1：加入契约快照生成器 + 产出 fixtures   ← 此时旧代码还在，才能运行它
        ↓
④ commit #2：删除全部 legacy 文件（约 60+ 个）    ← 此提交后工作树才"干净"
        ↓
⑤ 从 M1 开始建设
```

**为什么不能先删再生成**：契约快照的 fixtures 需要**旧代码真实运行**才能产出。如果先删了 `web.py` / `dorm_power.py`，就失去了"行为参照物"，45 项隐性行为只能靠人工回忆。

**为什么不能直接在 develop 上做**：develop 是生产参考基线，要保持可回滚。

### ② 分支基点的选择

| 方案 | 评价 |
|---|---|
| **从 `develop` 切出**（推荐） | ✅ git 历史连续；旧代码随时可查（`git show develop:web.py`）；将来可合回 |
| 孤儿分支（`--orphan`） | ❌ 历史断开；无法追溯；无法 cherry-pick；"干净"的收益不值这个代价 |

**结论：从 `develop` 切出，用"第一个 commit 清空"实现干净工作树。**

### ③ "干净"的定义

| 维度 | 要求 |
|---|---|
| **工作树** | 只含新架构文件（约 40 个），无任何 legacy |
| **git 历史** | 保留（旧代码可通过 `git show develop:<path>` 查阅） |
| **不保留** | `legacy/` 目录 —— 不把旧代码塞进新分支 |
| **可追溯** | `docs/` 保留 5 份新方案文档；旧文档归档到 `develop` |

### ④ 保留清单（进入新分支，17 项）

| # | 项 | 处置 |
|---|---|---|
| 1 | `LICENSE` | 保留（MIT） |
| 2 | `.gitignore` | **重写**（去掉 legacy 规则） |
| 3 | `.github/workflows/ci.yml` | **重写**（加 pytest + ruff + AST 守卫 + 多架构构建） |
| 4 | `README.md` | **重写**（面向非原作者） |
| 5 | `CONTRIBUTING.md` | ★ 新增 |
| 6 | `pyproject.toml` | ★ 新增 |
| 7 | `requirements.txt` + `requirements-dev.txt` | **重写**（加锁文件） |
| 8 | `Dockerfile` / `docker-compose.yml` / `.dockerignore` | ★ 新增 |
| 9 | `install.sh` | ★ 新增（替代 `setup.sh`） |
| 10 | `.env.example` | **重写**（只留 4 项启动必需） |
| 11 | `web.py` | ★ **新内容**（≤15 行 gunicorn 入口） |
| 12 | `app/` | ★ 全新 |
| 13 | `frontend/` | ★ 全新 |
| 14 | `tests/` | ★ 全新（含 fixtures） |
| 15 | `static/fonts/` | **去重后一份**（3 个 TTF，17.3MB） |
| 16 | `deploy/dorm-web.service` | **精简保留**（仅裸机路径需要） |
| 17 | `nginx/dorm.conf` | **精简保留**（可选，Docker 路径不需要） |
| 18 | `docs/`（5 份新方案文档） | 保留：REQUIREMENTS / SCOPE_DECISION / REWRITE_PLAN / PLAN_AUDIT / FEATURE_INVENTORY |

### ⑤ 删除清单（不进入新分支，约 60+ 文件）

| 类别 | 文件 | 数量 |
|---|---|---|
| **旧单体代码** | `config.py` `db.py` `web.py`(旧) `dorm_power.py` `feishu_bot.py` `auth.py` | 6 |
| **旧数据层** | `db/__init__.py` `db/_legacy.py` `db/models.py` `db/repo.py` | 4 |
| **旧 Jinja 模板** | `templates/*.html` | 8 |
| **旧前端** | `static/css/*.css`（4）+ `static/js/**`（11） | 15 |
| **字体重复** | `assets/fonts/*.ttf`（与 `static/fonts/` 去重） | 3 |
| **临时/垃圾** | `_tmp_*.html` `_tmp_*.py` `_tmp_*.txt` `diff_tests.txt` `preview_v2.html` | 14 |
| **数据文件** | `records.db` `tests/modern/records.db` | 2 |
| **旧测试** | `tests/modern/`（18）+ `tests/legacy/`（23） | 41 |
| **旧构建脚本** | `scripts/build/*`（11） | 11 |
| **旧部署脚本** | `scripts/deploy/r4*_*.sh` `r47_*.py` `r49_*.py` `r5*_*.sh` | 9 |
| **旧文档** | `docs/TECHNICAL.md` `docs/Round25_*.md` `docs/reports/` `docs/REFACTOR_DESIGN.md` `R68_RELEASE_NOTES.md` `AGENTS.md` | 6 |
| **旧配置/部署** | `setup.sh` `deploy/dorm-cron.txt` `deploy/dorm-power-monitor.logrotate` | 3 |

**注**：`deploy/dorm-cron.txt` 删除是因为 Q14 改用 APScheduler；`logrotate` 删除是因为 Docker 用 `json-file` 驱动轮转。

### ⑥ 分支命名与生命周期

| 项 | 值 |
|---|---|
| 分支名 | `rewrite/r69` |
| 基点 | `develop` |
| 目标 | 完成后合入 `main`，`develop` 归档为"R51c–R68 历史基线" |
| 标签 | `r69-m0` … `r69-m7`（每阶段一个） |
| 回滚 | 每阶段可独立 revert；生产切换失败则切回 `develop` |

### ⑦ 新分支的目标文件规模

| 类别 | 文件数 |
|---|---|
| 根目录配置 | 11（pyproject / requirements×2 / Dockerfile / compose / .dockerignore / install.sh / web.py / .env.example / .gitignore / LICENSE） |
| `app/` | ~35 |
| `frontend/` | ~30 |
| `tests/` | ~15 |
| `static/` | 4（3 字体 + vendor Chart.js） |
| `deploy/` `nginx/` | 2 |
| `docs/` | ~10 |
| `.github/` | 2 |
| **合计** | **约 105 个文件**（vs 现状 101 个，但**内容 100% 替换**） |

---

## 1. 目标架构

### 1.1 分层规则（单向依赖，CI 强制）

```
Layer 5  entry      web.py / scheduler.py                  ← ★ 新入口（≤15 行，非兼容 shim）
Layer 4  web        app/web/                               ← HTTP 适配层（Flask 蓝图）
Layer 3  service    app/services/                          ← 用例编排
Layer 2  domain     app/domain/                            ← 纯计算，零 IO
         adapter    app/scraper/  app/notify/              ← 外部系统适配
Layer 1  infra      app/db/  app/auth/  app/config.py      ← 基础设施
Layer 0  stdlib / 三方库
```

| 规则 | 内容 | 校验方式 |
|---|---|---|
| R1 | `app/domain/` **禁止** import `requests` / `flask` / `sqlite3` / `app.db` | AST 守卫 |
| R2 | `app/scraper/` 与 `app/notify/` **禁止**互相 import | AST 守卫 |
| R3 | `app/web/` 只允许依赖 `app/services/` + `app/auth/` | AST 守卫 |
| R4 | 任何 `app/**` **禁止** import 顶层 `web` / `dorm_power` / `feishu_bot` | AST 守卫 |
| R5 | **禁止**跨模块 import 下划线私有符号 | AST 守卫 |
| R6 | `app/**` 单文件 ≤ 400 行 | ruff `C901` + 自定义脚本 |
| R7 | **禁止**裸用 `datetime.now()` / `date.today()`（Q10） | AST 守卫（🔴 强制） |

### 1.2 目标目录树

```
dorm-power-monitor/
├── pyproject.toml                  ★ 项目元数据 + ruff + mypy + pytest 配置
├── requirements.txt                ◆ 加锁文件 requirements.lock（6 依赖，零 Rust）
├── web.py                          ◆ ≤40 行 gunicorn 入口（web:app + start/stop_scheduler）
├── gunicorn.conf.py                ★ WSGI 配置（workers=1 / preload=False / post_fork）
├── Dockerfile                      ★ 多阶段构建（build 阶段装依赖，运行阶段 slim）
├── docker-compose.yml              ★ 用户侧一键启动
├── .dockerignore                   ★
├── install.sh                      ★ 裸机一键安装（amd64/arm64）+ 随机密码生成
├── CONTRIBUTING.md                 ★
├── .github/workflows/
│   ├── ci.yml                      ◆ 加 pytest + ruff + AST 守卫
│   └── release.yml                 ★ buildx 三架构构建 + 推 registry + 导出离线包
│
├── app/
│   ├── __init__.py                 create_app 导出
│   ├── config.py                   ◆ 统一 Settings（.env 只留启动必需项）
│   ├── timeutil.py                 ★ now_cst() —— 全项目唯一时间源（Q10）
│   ├── scheduler.py                ★ APScheduler 装配（Q14）
│   ├── logging_setup.py            ★ 7 级日志 + 类别标签 + 模块级覆盖（Q20）
│   │
│   ├── config_registry/            ★ 配置中心（Q15）
│   │   ├── registry.py                  声明式定义每个配置项（唯一真相）
│   │   ├── store.py                     读写 meta + 类型转换 + 缓存
│   │   ├── crypto.py                    secrets AES-GCM 加密存储（Q17）
│   │   ├── validate.py                  统一校验器（类型/范围/正则/依赖）
│   │   └── portable.py                  导出 / 导入 JSON（N16）
│   │
│   ├── flags.py                    ★ 功能开关注册表 + is_enabled()（Q16）
│   │
│   ├── protocols.py                ★ Notifier / Endpoint 协议定义（Q18）
│   │
│   ├── db/                         ◆ 单一实现，消除"三份真相"
│   │   ├── schema.py                     _SCHEMA（含 users.disabled 新列）
│   │   ├── connection.py                 get_conn / init / WAL
│   │   ├── models.py                     Pydantic v2 实体（唯一模型层）
│   │   └── repositories.py               唯一 SQL 出口
│   │
│   ├── auth/                       ◆ 拆分 auth.py
│   │   ├── models.py  service.py  session.py  csrf.py  decorators.py  audit.py
│   │
│   ├── domain/                     ★ 纯计算，零 IO，100% 可单测
│   │   ├── metrics.py                    日均 / 月预计 / 差值 / 剩余天数
│   │   ├── pricing.py                    单价回退链（唯一实现）
│   │   └── thresholds.py                 低电 / 离线 / stale 判定
│   │
│   ├── scraper/                    ◆ 拆 dorm_power.py
│   │   ├── client.py                     Session + retry + UA/Referer
│   │   ├── endpoints.py                  F1–F5 fetcher（纯解析）
│   │   ├── backfill.py                   30 天回填（一次性 + 重试）
│   │   └── service.py                    run_once / fetch_once
│   │
│   ├── notify/                     ◆ 合并两处推送实现
│   │   ├── card_builder.py               卡片 JSON（无 HTTP）
│   │   ├── renderer.py                   Pillow PNG（字体只留一份）
│   │   ├── transport.py                  唯一 post_card / token / upload
│   │   ├── crypto.py                     AES / HMAC / 签名
│   │   ├── policies.py                   L1–L4 门控 + 静默 + dedupe
│   │   └── dispatcher.py                 9 命令 + 菜单 + 事件
│   │
│   ├── services/                   ★ 用例编排（web 与 scheduler 共享）
│   │   ├── dashboard_service.py
│   │   ├── oobe_service.py               合并新旧两套 OOBE
│   │   ├── admin_service.py
│   │   └── setup_service.py              自助配置（openid → roomId 解析）
│   │
│   └── web/
│       ├── factory.py                    create_app + 蓝图注册 + 错误处理
│       ├── security.py                   认证 / CSRF / 内部 token
│       ├── forms.py                      输入校验
│       └── blueprints/
│           ├── dashboard.py  admin.py  oobe.py  setup.py
│           ├── auth_api.py   feishu.py  health.py
│
├── frontend/                       ★ Vue 3 + Vite + TS + Tailwind
│   ├── package.json  vite.config.ts  tsconfig.json  tailwind.config.ts
│   ├── index.html
│   └── src/
│       ├── main.ts  App.vue  router/  stores/  api/
│       ├── views/          Dashboard / History / Finance / Meter
│       │                   Admin{Users,Config,Test,Audit} / Login / Oobe
│       ├── components/     图表 / 卡片 / 表格 / 开关 / Toast
│       └── styles/         tokens.css + tailwind
│
├── static/                         ◆ 构建产物落到这里（后端 serve）
│   ├── assets/*.js,*.css                 ← Vite 输出
│   ├── fonts/                            单一字体目录（去重，省 17.3MB）
│   └── vendor/chart.umd.min.js           Chart.js 本地化
│
├── tests/
│   ├── conftest.py                 ★
│   ├── unit/  integration/  regression/
│   └── legacy/                           归档旧 round 测试（只读，不入 CI）
│
├── deploy/
│   ├── dorm-web.service            ◆ 裸机路径保留
│   └── nginx/dorm.conf             ◆ 可选
└── docs/                           ◆ 面向非原作者重写
```

★ = 新增 ｜ ◆ = 重构/合并

---

## 2. 关键模块设计

### 2.1 时间源（Q10）—— 全项目唯一

```python
# app/timeutil.py
from datetime import datetime, date
from zoneinfo import ZoneInfo

CST = ZoneInfo("Asia/Shanghai")

def now_cst() -> datetime:
    """朴素北京时间，与服务器时区无关。"""
    return datetime.now(CST).replace(tzinfo=None)

def today_cst() -> date:
    return now_cst().date()

def stamp() -> str:
    """写库格式：YYYY-MM-DD HH:MM:SS"""
    return now_cst().strftime("%Y-%m-%d %H:%M:%S")
```

**强制**：AST 守卫禁止 `app/**` 出现 `datetime.now()` / `datetime.today()` / `date.today()`。
**例外**：`app/timeutil.py` 自身。

### 2.2 调度与服务器启动（Q14 + 修复 B2/B3）

> **完整设计见 `BLOCKER_FIXES.md §2 §3`**。核心是**三道防线**。

```python
# gunicorn.conf.py —— workers=1 + preload=False + post_fork 启动
bind, workers, preload_app = "127.0.0.1:5000", 1, False

def post_fork(server, worker):        # 防线 2：在 worker 内启动，且只启动一次
    from web import start_scheduler_once
    start_scheduler_once()

def worker_int(worker):               # 优雅停止（Q17）
    from web import stop_scheduler
    stop_scheduler()
```

```python
# web.py —— 防线 3：master PID 守卫（误配 → 启动即崩，不静默重复抓取）
_MASTER_PID = os.getpid()

def start_scheduler_once():
    if _scheduler is not None: return              # 幂等
    if os.getpid() == _MASTER_PID:
        raise RuntimeError("调度器不得在 master 进程启动：保持 preload_app=False")
    _scheduler = build_scheduler(); _scheduler.start()
```

| 场景 | 调度器实例数 |
|---|---|
| Docker / 裸机 / 开发（gunicorn） | **1** ✅ |
| ❌ 误配 `--preload` | **启动即崩** ✅ |
| ❌ 误配 `python web.py` | **路径已删除** ✅ |

**⚠️ M3 修正**：`misfire_grace_time` **不补跑停机期的任务**（只覆盖"运行中阻塞"）。**决定：不补跑**，缺口在下次 tick 后恢复。

### 2.3 访问控制（Q11）

```python
# app/auth/decorators.py
def require_auth(role: str | None = None) -> Callable: ...

def require_read_access(fn):
    """读接口守卫：public_readonly=1 时放行，否则要求登录。"""
    @wraps(fn)
    def wrapper(*a, **kw):
        if settings.public_readonly:
            return fn(*a, **kw)
        return require_auth()(fn)(*a, **kw)
    return wrapper
```

**应用点**：`/api/data`、`/api/live`、以及 SPA 首页的 bootstrap 接口。

### 2.4 自助配置（N4）

```
用户粘贴 H5 URL
   ↓
POST /api/setup/parse-url   ← 复用现有 _parse_school_url（含 SSRF 4 层防护）
   ↓ 返回 {openid, roomId, roomNo, EqPrice}
POST /api/setup/verify      ← 用解析结果真跑一次 F1，验证连通
   ↓
POST /api/setup/commit      ← 写入 meta（dorm_openid / last_room_id / eqprice）
```

**安全约束（沿用现有 R37 B5 防护，不得简化）**：
1. host 白名单（对比 `config.DORM_BASE_URL` 的 hostname）
2. IP 字面量拒绝（`ipaddress.ip_address(...).is_global == False`）
3. scheme 白名单（http/https）
4. `allow_redirects=False`

### 2.5 通知层合并（消除 L4/L5）

| 现状 | 目标 |
|---|---|
| `dorm_power._post_feishu` | → `notify.transport.post_card`（**唯一**） |
| `feishu_bot._post_feishu` | → 同上（参数取并集） |
| `web.py:2238/2882` `from dorm_power import _post_feishu` | → `from app.notify.transport import post_card` |
| `_sign` / `_now_beijing` / `_is_due` 在 dorm_power | → `notify.policies` |
| `_should_push` / `_in_quiet_hours` 在 feishu_bot | → `notify.policies` |

### 2.6 数据层收口（消除 L3）

```
app/db/
  schema.py         _SCHEMA（唯一）+ users.disabled 新列
  connection.py     get_conn / init / WAL / foreign_keys=ON
  models.py         Pydantic v2：Record / DailyElec / Violation / Pay /
                    RunStatus / MetaEntry / User / Session / AuditLog
  repositories.py   唯一 SQL 出口，返回 models 实体
```

**删除**：`db/_legacy.py`（"冻结副本"）、`db/repo.py`（重复实现）、根 `db.py`（shim）
**保留**：`db/__init__.py` 作为 re-export（若需兼容）或直接删除（重写可自由改 import）

### 2.7 前端 SPA

```
frontend/src/
  api/client.ts          统一 fetch 封装（CSRF 头 + 401 跳登录 + 错误 toast）
  stores/auth.ts         session 状态
  router/index.ts        路由 + 守卫（未登录 → /login；OOBE 未完成 → /oobe）
  views/
    Dashboard.vue        4 段（概览/历史/违规/电表）—— 复用现有 DOM 结构语义
    Login.vue  Oobe.vue
    admin/{Users,Config,Test,Audit}.vue
  components/
    StatCard.vue  RoomCard.vue  RecordsTable.vue  HistoryRow.vue
    ChartContainer.vue  Spinner.vue  Toast.vue  RangeGroup.vue
```

**保留的前端契约**（SCOPE_DECISION §E）：
- 4 档时间范围 `24h / 3d / 7d / 30d`
- 30 秒轮询间隔
- 键盘快捷键 `1–4` / `R`
- 主题切换（Tailwind dark mode）
- 本地存储段位记忆
- `ts` 为朴素 CST（由 `timeutil` 统一保证）

---

### 2.8 配置注册表（Q15）—— 本次最大的新增设计

**目标**：**每一个**配置项都由注册表声明，WebUI 据此**自动生成表单**，后端据此**自动校验**。

```python
# app/config_registry/registry.py
from dataclasses import dataclass
from enum import Enum

class T(Enum):
    BOOL = "bool"; INT = "int"; FLOAT = "float"; STR = "str"
    SECRET = "secret"; TIME = "time"; ENUM = "enum"; CRON = "cron"
    DURATION = "duration"; URL = "url"; JSON = "json"

@dataclass(frozen=True)
class Setting:
    key: str                      # meta 键名（沿用现有 snake_case，零迁移）
    label: str                    # 中文标签（UI 显示）
    group: str                    # 分组（UI 分栏）
    type: T
    default: object
    help: str = ""                # UI 帮助文本
    validate: str | None = None   # 校验规则表达式，如 "1..720"
    requires_restart: bool = False
    secret: bool = False          # True → 加密存储 + 脱敏返回
    depends_on: str | None = None # 条件显示，如 "push_l2_enable"
    advanced: bool = False        # 默认折叠

REGISTRY: dict[str, Setting] = {s.key: s for s in [...]}
```

**配置分组（8 组）**：

| 组 | 内容 |
|---|---|
| **站点** | 站点名 / 时区（锁定只读）/ 主题色 |
| **数据采集** | `dorm_openid`(secret) / `dorm_base_url` / `last_room_id` / `eqprice` / 抓取间隔 / F2·F3·F5 节流 / 重试次数 / 超时 / UA / 学期锚定日期 |
| **告警阈值** | 低电红·橙·蓝三档 / 离线判定 / stale 判定 / 违规冷却 / 违规标红阈值 |
| **推送（群）** | 总开关 / `feishu_webhook_url`(secret) / `feishu_secret`(secret) / L1·L2·L3·L4·违规·stale 分项开关 / 推送时间 / 静默时段 / 接收人 |
| **机器人（私聊）** | 总开关 / `feishu_app_id` / `feishu_app_secret`(secret) / `feishu_verification_token`(secret) / `feishu_encrypt_key`(secret) / 9 个命令开关 |
| **认证** | session 时长 / 滑动或绝对 / 锁定次数·时长 / bcrypt rounds / 密码策略 / `public_readonly` |
| **日志** | 全局级别 / 模块级覆盖 / 格式（文本·JSON）/ 文件轮转天数 / 文件路径 |
| **高级** | `api_internal_token`(secret) / 备份策略 / DB 路径（只读展示） |

**存储分层**：

```
.env（启动必需，4 项，不可在 UI 改）
  DB_PATH / FLASK_SECRET_KEY / FLASK_PORT / DORM_DATA_DIR
        ↓ 启动时读取
meta 表（运行时配置，全部可在 UI 改）
  ← 现有 33 键（键名不变）
  + 新增键（沿用 snake_case）
        ↓ 通过 config_registry.store 访问（类型转换 + 内存缓存 + 失效通知）
业务代码
```

**API**：

| 端点 | 说明 |
|---|---|
| `GET /api/admin/config/schema` | 返回注册表元数据（**前端据此渲染表单**） |
| `GET /api/admin/config` | 返回当前值（secret 脱敏为 `••••1234`） |
| `PUT /api/admin/config` | 批量更新（逐键校验 + 逐键审计） |
| `POST /api/admin/config/test` | 对可测试项做连通性测试（webhook / 飞书 token / 学校 API） |
| `GET /api/admin/config/export` | 导出 JSON（可选是否含明文 secret） |
| `POST /api/admin/config/import` | 导入 JSON（校验后写入 + 审计） |
| `POST /api/admin/config/reset` | 单项或分组重置为默认值 |

**安全（Q17）**：
- `secret=True` 的项：AES-GCM 加密后存 DB，密钥由 `FLASK_SECRET_KEY` 派生
- GET 永不返回明文，只返回 `••••` + 末 4 位
- PUT 时若值等于脱敏占位符 → 视为"不修改"
- 导出默认**不含**明文 secret；含明文时 UI 需二次确认 + 审计
- `FLASK_SECRET_KEY` 丢失 → 配置不可解 → 启动时校验并给出明确错误提示

---

### 2.9 功能开关体系（Q16）

```python
# app/flags.py
FLAGS: dict[str, str] = {
    "push_group_enabled":     "群推送总开关",
    "push_bot_enabled":       "私聊机器人总开关",
    "push_l1_enable":         "L1 低电告警",
    "push_l2_enable":         "L2 整点播报",
    "push_daily_enable":      "L3 日报",
    "push_weekly_enable":     "L3 周报",
    "push_monthly_enable":    "L3 月报",
    "push_offline_enable":    "L4 电表离线",      # 🆕
    "push_violation_enable":  "违规告警",         # 🆕
    "push_stale_enable":      "stale 数据陈旧",   # 🆕
    "cmd_remain_enabled":     "/状态 /剩余",      # 🆕
    "cmd_meter_enabled":      "/电表",
    "cmd_today_enabled":      "/今日",
    "cmd_history_enabled":    "/历史",
    "cmd_pay_enabled":        "/缴费",
    "cmd_violations_enabled": "/违规",
    "cmd_help_enabled":       "/帮助",
}

def is_enabled(flag: str) -> bool:
    """总开关优先，其次分项开关，默认 True。"""
```

**关键约束 —— 关闭 ≠ 报错**：

| 场景 | 关闭后的行为 |
|---|---|
| 群推送关闭 | `post_card()` 直接返回，记 `NOTICE` |
| 机器人关闭 | `/feishu/event` 返回 `200 {"ok": true}` 但不处理（**飞书侧不报错**） |
| 某告警层关闭 | 该层 `should_push()` 返回 False，记 `NOTICE` |
| 某命令关闭 | 回复"该命令已禁用"，记 `NOTICE` |

---

### 2.10 日志子系统（Q20）

```python
# app/logging_setup.py
TRACE, NOTICE = 5, 25           # 自定义级别
logging.addLevelName(TRACE, "TRACE")
logging.addLevelName(NOTICE, "NOTICE")

CATEGORIES = ("scrape","push","auth","config","scheduler","db","web","notify")

class CategoryFilter(logging.Filter):
    """按业务类别 + 模块级级别覆盖做过滤。"""

def setup_logging(cfg) -> None:
    """控制台 + 可选文件（按天轮转）；格式文本或 JSON。"""
```

**配置项（WebUI 可改）**：

| 键 | 默认 | 说明 |
|---|---|---|
| `log_level` | `INFO` | 全局级别（7 选 1） |
| `log_overrides` | `{}` | 模块级覆盖，如 `{"scrape":"DEBUG","db":"WARNING"}` |
| `log_format` | `text` | `text` / `json` |
| `log_file_enabled` | `false` | 是否写文件 |
| `log_file_retention_days` | `7` | 保留天数 |

**铁律**：`openid` / `token` / `secret` / `password` 在**任何级别**都不得出现
→ AST 守卫检查 f-string 与 `%` 格式化 + 单元测试对日志输出做正则断言。

**与审计日志分离**：运行日志可轮转可降级；`audit_log` 表不可篡改、不轮转。

---

### 2.11 质量属性设计（Q17）

| 属性 | 措施 | 验证方式 |
|---|---|---|
| **安全** | secrets AES-GCM / session token 哈希 / CSRF / 登录锁定 / SSRF 4 层 / fail-closed / 输入校验 / 审计 / 容器非 root + 只读根 FS + `cap_drop: ALL` | 单元测试 + 安全检查清单 |
| **稳定** | 单点失败隔离 / 幂等写 / 重试+退避 / `/healthz` / 优雅关闭（SIGTERM 停调度器）/ 定时备份 / 磁盘空间检查 / 单 worker 断言 | 故障注入测试 |
| **鲁棒** | 边界处理（负数 delta / 空数据 / NULL / 超长输入）/ 降级路径（stale 兜底）/ 时区锁定 / 配置校验 / 崩溃自恢复 | 边界用例集（≥ 40 例） |

---

### 2.12 扩展点协议（Q18）

```python
# app/protocols.py
class Notifier(Protocol):
    name: str
    def enabled(self) -> bool: ...
    def send_card(self, card: dict) -> None: ...
    def send_text(self, text: str) -> None: ...

class Endpoint(Protocol):
    key: str                    # "F1" / "F2" ...
    interval_sec: int | None    # None = 每次抓取
    def fetch(self, ctx: FetchContext) -> list[dict]: ...
    def persist(self, rows: list[dict]) -> None: ...
```

**扩展 SOP**（写进 `docs/EXTENDING.md`）：

| 想做什么 | 步骤 | 涉及文件数 |
|---|---|---|
| 加配置项 | ① `registry.py` 加一行 ② 完 | **1** |
| 加通知渠道（微信/邮件） | ① 实现 `Notifier` ② 注册 ③ `registry.py` 加凭据项 | **2** |
| 适配其他学校 | ① 实现 `Endpoint` 集合 ② `registry.py` 加 base_url | **2** |
| 加告警层 | ① `policies.py` 加判定 ② `flags.py` 加开关 ③ `registry.py` 加阈值 | **3** |
| 加飞书命令 | ① `dispatcher.py` 的 `COMMANDS` 加一行 ② 写 handler | **1** |
| 加前端页面 | ① `views/X.vue` ② `router` 加路由 | **2** |

---

### 2.13 认证 UI 设计（Q21）🎨

**背景**：现状有 **6 处认证界面、4 套互不相干的样式**，其中 **浏览器原生 Basic Auth 弹窗**（`WWW-Authenticate: Basic`）**完全无法定制** —— 这是"丑"的根源。

**目标**：**删除全部原生认证机制，6 个认证界面统一走一套设计系统。**

#### ① 后端：彻底移除 Basic Auth

| 动作 | 位置 |
|---|---|
| ❌ 删除 `_admin_required` 装饰器 | `web.py:1218-1255` |
| ❌ 删除 `WWW-Authenticate: Basic` 响应头 | `web.py:1251` |
| ❌ 删除 `request.authorization` 解析 | `web.py:1246` |
| ❌ 删除 `meta.admin_password` 全部读写 | `web.py:1240/2288/2396/3086` |
| ❌ 删除 `ADMIN_PASSWORD` env 回退 | `web.py:1240` |
| ❌ 删除 `/admin/set-password` 恢复页 | `web.py:2292-2400`（对应审计分歧点 1） |
| ✅ 所有认证统一走 `require_auth()`（R63 session） | — |
| ✅ 未认证一律返回 **JSON 401**（不是 HTML 401，不是 Basic 挑战） | — |

> **连带收益**：这同时关闭了审计报告中的 **M2（明文密码路径）** —— 两个问题一次解决。

#### ② 前端：`AuthLayout` 统一骨架

```
frontend/src/layouts/AuthLayout.vue      ← 登录 / 改密 / OOBE 密码步骤共用
├── 品牌区（logo + 站点名）
├── 卡片区（玻璃拟态，Tailwind token）
├── 表单槽位
├── 内联错误区（aria-live）
└── 页脚（版本号 + 帮助链接）
```

**6 个认证界面**（全部复用 `AuthLayout`）：

| # | 路由 | 页面 | 关键交互 |
|---|---|---|---|
| 1 | `/login` | 登录 | 用户名/密码 + 👁 可见性切换 + 锁定倒计时 |
| 2 | `/change-password` | **首登强制改密**（Q19） | 显示随机密码 + 新密码 + 强度计 |
| 3 | `/session-expired` | 会话过期 | 保留原目标路径，登录后回跳 |
| 4 | `/oobe/*` | OOBE 各步（含密码步） | 与登录页共用组件 |
| 5 | 降级路径 | 无 JS / curl | **复用同一套 HTML 设计**，不再是裸页 |
| 6 | `/reset-password` | 密码重置 | ⚠️ 建议**不做**（见审计分歧点 1） |

#### ③ 统一交互规范（11 项）

| 项 | 要求 |
|---|---|
| 密码可见性切换 | 👁 按钮，键盘可达 |
| 强度计 | 实时反馈（复用 OOBE 现有逻辑） |
| 错误提示 | **内联**（不弹 `alert`、不跳页、不刷屏） |
| 加载态 | 按钮 spinner + 禁用，防重复提交 |
| 锁定提示 | 倒计时（"还需等待 12:34"） |
| 自动聚焦 | 首屏聚焦用户名 |
| `autocomplete` | `username` / `current-password` / `new-password` |
| 键盘可达 | 全程 Tab + Enter 提交 |
| ARIA | `aria-live` 播报错误；`aria-invalid` 标记字段 |
| 响应式 | 4 断点（≤767 / 768–1023 / 1024–1439 / ≥1440） |
| 深色/浅色 | 跟随设计令牌，两套都要好看 |

#### ④ 验收要点

- [ ] 全项目 `grep -r "WWW-Authenticate"` 为空
- [ ] 全项目 `grep -r "request.authorization"` 为空
- [ ] 全项目 `grep -r "meta.admin_password"` 为空
- [ ] 未登录访问受保护路由 → **JSON 401**（不是浏览器弹窗）
- [ ] 6 个认证界面视觉一致（同一 `AuthLayout`）
- [ ] 4 断点 × 6 界面视觉走查通过
- [ ] 键盘全程可达（Tab 走一遍不用鼠标）
- [ ] 无内联 `<style>` 硬编码认证页面

---

## 3. 里程碑计划

> **总计 8 个阶段 / 38 个工作日**（修复 5 个阻断级问题后，由 30 天修订为 38 天）
> **对外口径**：38 天（乐观）/ **45 天（现实）** / 55 天（含返工）
> 每阶段独立可交付、可验证、可回滚。**可随时停在任一阶段。**

### M0 — 基线保全 + 分支建立 + 契约快照（4 天）🔴 最高优先

> 按 **Q22** 的顺序约束执行；**commit #1 与 #2 的顺序不可颠倒**

| 步 | 提交 | 内容 |
|---|---|---|
| **0.1** | `develop` | **落库 R61–R67 的 24 项未提交成果**（唯一不可逆风险，必须先做） |
| **0.2** | — | 从 `develop` 切出 `rewrite/r69` |
| **0.3** | `r69` #1 | 建 `pyproject.toml` + `requirements-dev.txt` + `tests/conftest.py` |
| **0.4** | `r69` #1 | **写契约快照生成器**（此时旧代码仍在，可运行）→ 产出 fixtures 到 `tests/regression/fixtures/` |
| **0.5** | `r69` #1 | 5 类快照全部产出：45 项隐性行为 / 卡片 JSON / API 字段集 / 算法向量 / schema |
| **0.6** | `r69` #2 | **删除全部 legacy 文件**（约 60+，见 §0.1 ⑤）→ 工作树变干净 |
| **0.7** | `r69` #3 | 建 CI（`pytest` + `ruff` + AST 守卫 R1–R7 + 快照 diff） |
| **0.8** | `r69` #3 | 验证：在**没有旧代码**的分支上，快照测试仍能跑通 |

**验收**
- [ ] `develop` 上 24 项已落库，`git status` 干净
- [ ] `rewrite/r69` 工作树只含新架构文件（`git ls-files` 无 legacy）
- [ ] `tests/regression/fixtures/` 有完整 5 类快照
- [ ] **删除 legacy 后 `pytest` 仍全绿**（证明快照是自包含的）
- [ ] `ruff check .` 零错误
- [ ] AST 守卫 R1–R7 通过

**回滚**：删 `rewrite/r69` 分支，`develop` 不受影响

**⚠ 风险**：0.6 步删除 60+ 文件在当前分支上不可逆 —— 但旧代码在 `develop` 里完好，可随时 `git checkout develop -- <path>` 取回。

---

### M1 — 基础设施层（5 天）

| 步 | 内容 |
|---|---|
| 1.1 | `app/timeutil.py`（Q10）+ AST 守卫 |
| 1.2 | `app/config.py` 统一 Settings（`.env` 只留启动必需 4 项） |
| 1.3 | `app/db/` **五文件**（schema / connection / **migrations** / models / repositories） |
| 1.4 | `app/auth/` 拆分 + **`password.py`（stdlib scrypt，替代 bcrypt）** + `sessions` 存哈希 + `users.disabled` + `users.must_change_password` |
| 1.5 | **`app/config_registry/` 五文件**（Q15）：registry（含 **`kind` 三类划分**）/ store / crypto / validate / portable |
| 1.6 | **`app/flags.py`**（Q16）：17 个开关注册 + `is_enabled()` |
| 1.7 | **`app/logging_setup.py`**（Q20）：7 级 + 类别 + 模块级覆盖 + 文本/JSON |
| 1.8 | **`app/protocols.py`**（Q18）：`Notifier` / `Endpoint` 协议 |
| 1.9 | 单元测试：`app/domain/` 100% 覆盖 + config_registry 全分支 |

**验收**：现有 `records.db` 直接可用；schema 快照通过；**所有现有配置项已迁入注册表且可从 store 读到**
**回滚**：`git revert`（纯新增 + 搬迁）

---

### M2 — 抓取层 + 调度（3 天）

| 步 | 内容 |
|---|---|
| 2.1 | `app/scraper/{client,endpoints,backfill,service}.py`（实现 `Endpoint` 协议） |
| 2.2 | `app/scheduler.py`（Q14）+ **`post_fork` 启动时机设计**（审计 B3） |
| 2.3 | **新建 `web.py`** 为 gunicorn 入口（`web:app` + `create_app()` + `logging_setup()` + 单 worker 断言） |
| 2.4 | **更新 `deploy/dorm-web.service`**：`ExecStart` 改为 gunicorn（审计 B2） |

**验收**：`gunicorn web:app` 启动成功；调度器**只启动一次**（日志验证）；`records` 表新增行
**回滚**：`git revert`

> **Q22 影响**：原方案里"`dorm_power.py` 改为兼容 shim"这一步**已删除** —— 该文件在新分支不存在，抓取能力由 `app/scraper/` 全新实现。

---

### M3 — 通知层（3 天）

| 步 | 内容 |
|---|---|
| 3.1 | `app/notify/{crypto,transport,card_builder,renderer,policies,dispatcher}.py` |
| 3.2 | 验证**无跨模块私有调用**（AST 守卫 R5） |
| 3.3 | 字体目录去重（`assets/fonts/` 删除，只留 `static/fonts/`） |

**验收**：卡片 JSON 快照一致；签名/AES 向量测试通过
**回滚**：`git revert`

> **Q22 影响**：原方案里"消除 `web.py` 对 `dorm_power._post_feishu` 的调用"**已无意义** —— 两个文件都不在新分支。改为"从一开始就不产生这种耦合"。

---

### M4 — API 层 + 访问控制 + 配置 API（5 天）

| 步 | 内容 |
|---|---|
| 4.1 | `app/services/` 四个 service |
| 4.2 | `app/web/` 7 个蓝图（dashboard / admin / oobe / setup / auth_api / feishu / health） |
| 4.3 | 访问控制（Q11）：默认需登录 + `public_readonly` 开关 |
| 4.4 | **OOBE 收敛为 5 步**（B4/B5）：删除"建号"步；端点 **10 → 2**（`GET /api/oobe/state` + `POST /api/oobe/advance`）；复用配置注册表 → **消除 L6** |
| 4.5 | 自助配置 API（N4） |
| 4.6 | **配置 API 七个端点**（Q15）：schema / get / put / test / export / import / reset |
| 4.7 | **开关接入**（Q16）：`policies` / `transport` / `dispatcher` 全部走 `is_enabled()` |
| 4.8 | **bootstrap 建号 + 强制首登改密**（B4）：首启用 `BOOTSTRAP_ADMIN_PASSWORD` 建号 → 用后即焚 → `must_change_password=1` 拦截所有非改密接口 |
| 4.9 | **彻底移除 Basic Auth**（Q21）：删 `_admin_required` / `WWW-Authenticate` / `request.authorization` / `meta.admin_password` 全部路径；未认证统一返回 JSON 401 |

**验收**：API 契约快照通过；`public_readonly` 两态验证；**逐个开关关闭后行为符合"静默降级"**；配置导入导出往返一致；**`grep -r "WWW-Authenticate\|request.authorization\|meta.admin_password"` 为空**
**回滚**：`git revert`

---

### M5 — 前端 SPA（7 天）

| 步 | 内容 |
|---|---|
| 5.1 | Vite + Vue3 + TS + Tailwind 脚手架 |
| 5.2 | 设计令牌 + 基础组件 |
| 5.3 | **`AuthLayout` 统一骨架**（Q21）+ 6 个认证界面 |
| 5.4 | Dashboard 4 段（对齐现有 DOM 语义与算法） |
| 5.5 | Admin 5 页 |
| 5.6 | **配置中心页**（Q15）：**由 `GET /config/schema` 动态渲染 8 个分组表单**，含脱敏显示、条件显示、重置、导入导出 |
| 5.7 | **功能开关页**（Q16）：总开关 + 分项开关 + 命令开关，带实时状态提示 |
| 5.8 | **日志设置页**（Q20）：级别 / 模块级覆盖 / 格式 / 轮转 |
| 5.9 | Chart.js 本地化 + 构建产物落到 `static/` |

**验收**：4 断点 × 11 页面视觉走查；**新增配置项只需改后端注册表、前端零改动**（自动渲染验证）；**6 个认证界面视觉一致且无原生弹窗**；无公网 CDN 依赖
**回滚**：后端可单独 revert，前端产物不影响 API

---

### M6 — 分发与交付（4 天）

| 步 | 内容 |
|---|---|
| 6.1 | `Dockerfile`（多阶段）+ `docker-compose.yml` + `.dockerignore` |
| 6.2 | `release.yml`：buildx 三架构 + 推 registry + 导出离线包（N1/N2/N5） |
| 6.3 | `install.sh` 裸机一键安装（N3，仅 amd64/arm64）+ **随机密码生成**（Q19） |
| 6.4 | Docker entrypoint 同样生成随机密码并打印一次 |
| 6.5 | 文档重写（N7）：部署指南 / 配置说明 / FAQ / 升级回滚 / CONTRIBUTING |
| 6.6 | **`docs/EXTENDING.md`**（Q18）：6 类扩展的分步 SOP |
| 6.7 | 截图 3 张 |

**验收**：`docker compose up -d` 在 amd64/arm64 跑通；离线包可 `docker load`；**新用户按文档 15 分钟内装完且首登被强制改密**；按 `EXTENDING.md` 加一个配置项只需改 1 个文件
**回滚**：分发产物独立，不影响已部署实例

---

### M7 — 生产切换（1 天）

按 Q13 的 SOP：停旧 → 起新 → 验证 → （失败则秒级回滚）

**验收**：见 §6 验收标准

---

## 4. 测试策略

### 4.1 测试金字塔

| 层 | 目录 | 目标 | 内容 |
|---|---|---|---|
| **契约快照** | `tests/regression/` | 5 类 | 45 项隐性行为 / 卡片 JSON / API 字段集 / 算法向量 / schema |
| 单元 | `tests/unit/` | ≥ 80 | `app/domain/*`（100% 覆盖）、`db` coercion、`notify/crypto`、`card_builder` |
| 集成 | `tests/integration/` | ≥ 30 | Flask test client 打全部端点、`run_once` 编排、OOBE 全流程、自助配置 |
| 归档 | `tests/legacy/` | 41（现状） | 保留只读，**不进 CI** |

### 4.2 覆盖率门禁

| 范围 | 目标 |
|---|---|
| `app/domain/` | **100%**（强制） |
| `app/db/` | ≥ 85% |
| `app/notify/crypto.py` | **100%**（安全敏感） |
| `app/timeutil.py` | **100%** |
| 全仓 | ≥ 70% |

### 4.3 CI 门禁

```yaml
- ruff check .                    # 含 R6 行长规则
- mypy app/                       # 渐进严格
- pytest -q --cov=app --cov-fail-under=70
- python -m scripts.ast_guard     # R1–R7 分层 + 时区守卫
- 契约快照 diff（必须为 0）
- secret scan
```

### 4.4 契约快照的 5 类必测项（防"静默丢功能"）

| 类 | 覆盖 |
|---|---|
| **行为快照** | 45 项 ⚠ 隐性行为的输入→输出对照 |
| **卡片快照** | 6 种卡片 JSON 结构（L1/L2/L3/L4/stale/PNG 尺寸） |
| **API 快照** | `/api/*` 的字段名集合 + 小数位 |
| **算法向量** | 本月预计电费 / 日均 / 1 小时差值 / 剩余天数（含边界与降级） |
| **Schema 快照** | 10 张表的表名 + 字段名 + 索引名 |

---

## 5. 部署与分发（N1–N5）

### 5.1 Docker 路径（主）

```
用户操作：
  $ docker compose up -d
     ↓
  容器内：gunicorn(1 worker) + Flask + APScheduler + Vue 静态产物
     ↓
  数据卷：./data/records.db  ← 可挂载已有 DB（零迁移）
  配置卷：./data/.env
```

**Dockerfile 设计（多阶段）**：

| 阶段 | 基础镜像 | 内容 |
|---|---|---|
| `frontend-build` | `node:22-alpine` | `npm ci && npm run build` → `static/` |
| `python-deps` | `python:3.12-slim` | `pip install -r requirements.lock`（**含编译 Pillow/pycryptodome/pydantic-core**） |
| `runtime` | `python:3.12-slim` | 拷贝 deps + 代码 + 前端产物 + 字体；`USER 非 root`；`HEALTHCHECK /healthz` |

**多架构构建（CI）**：

```yaml
# .github/workflows/release.yml
- uses: docker/setup-qemu-action@v3
- uses: docker/setup-buildx-action@v3
- uses: docker/build-push-action@v6
  with:
    platforms: linux/amd64,linux/arm64,linux/arm/v7
    push: true
    tags: ghcr.io/<owner>/dorm-power-monitor:r69
```

> ⚠️ **armv7 走 QEMU 模拟**，Rust 依赖（pydantic-core / bcrypt）编译较慢，单次构建预计 30–60 分钟。
> 但**用户侧只需 `docker pull`，不编译**。

**离线镜像包（N5）**：
```bash
docker save ghcr.io/<owner>/dorm-power-monitor:r69 | gzip > dorm-r69-amd64.tar.gz
# 用户侧
docker load < dorm-r69-amd64.tar.gz
```
CI 在 release 时自动产出 3 个架构的离线包作为 Release Asset。

### 5.2 裸机路径（兜底，仅 amd64/arm64）

```bash
$ curl -fsSL https://<repo>/install.sh | bash
```

`install.sh` 步骤：
1. 检测架构（amd64 / arm64；**armv7 直接报错并引导用 Docker**）
2. 检查 Python ≥ 3.10
3. 创建 `/opt/dorm-power-monitor` + venv
4. `pip install -r requirements.lock`
5. 交互式收集配置（或引导去 Web OOBE）
6. 写 systemd unit（**只需 1 个服务，不再需要 cron**）
7. `systemctl enable --now`

### 5.3 两条路径的等价性

| 能力 | Docker | 裸机 |
|---|---|---|
| Web + API | ✅ gunicorn | ✅ gunicorn（systemd） |
| 抓取调度 | ✅ APScheduler（进程内） | ✅ APScheduler（进程内） |
| **用户需配 cron** | ❌ **不需要** | ❌ **不需要** |
| 数据持久化 | ✅ 卷挂载 | ✅ `/opt/.../records.db` |
| 升级 | `docker compose pull && up -d` | `install.sh --upgrade` |
| 支持架构 | amd64 / arm64 / **armv7** | amd64 / arm64 |

---

## 6. 验收标准（DoD）

### 6.1 每个里程碑的 DoD

- [ ] `pytest -q` 全绿
- [ ] `ruff check .` 0 error
- [ ] `mypy app/` 无新增错误
- [ ] **契约快照 diff = 0**
- [ ] AST 守卫 R1–R7 通过
- [ ] `git status --short` 干净
- [ ] 打 tag `r69-mN`
- [ ] 更新 `docs/reports/r69_mN_REPORT.md`

### 6.2 重写完成 DoD

**功能等价性**
- [ ] 50 项「必留」行为**逐项验证**与旧版一致
- [ ] 52 项「改」按 `SCOPE_DECISION.md` 的理由完成调整
- [ ] 3 项「删」确认已移除且无副作用
- [ ] 12 项「新增」全部可用

**契约**
- [ ] 10 张表的表名 / 字段名 / 索引名与 §3.3 一致
- [ ] 6 种飞书卡片 JSON 结构一致
- [ ] `/api/*` 字段名与小数位一致
- [ ] 核心算法（本月预计 / 日均 / 1 小时差值）向量测试通过
- [ ] `.env` 键名与 meta 键名不变

**质量**
- [ ] `app/domain/` 覆盖率 100%
- [ ] `app/**` 单文件 ≤ 400 行
- [ ] 无 `from X import _private` 跨模块调用
- [ ] 无裸用 `datetime.now()`（AST 守卫通过）
- [ ] 无公网 CDN 依赖

**配置中心（Q15）**
- [ ] `GET /api/admin/config/schema` 返回完整注册表（8 分组）
- [ ] **每一个**配置项都能在 WebUI 修改并生效（逐项勾选验证）
- [ ] 硬编码常量已全部迁入注册表（`grep` 验证代码中无残留魔法数字）
- [ ] secret 项：GET 脱敏 / DB 中加密存储 / 明文不落日志
- [ ] `requires_restart` 项在 UI 上有明确提示
- [ ] 导出 → 导入 往返一致（含与不含明文 secret 两种）
- [ ] 新增一个配置项只需改 `registry.py` 一个文件，**前端零改动**

**功能开关（Q16）**
- [ ] 群推送总开关 / 私聊机器人总开关 / 9 个分项 / 9 个命令开关全部可切换
- [ ] **关闭后是"静默降级"而非报错**（逐开关验证）
- [ ] 关闭动作在日志中留 `NOTICE` 记录
- [ ] 机器人关闭时 `/feishu/event` 返回 200，飞书侧无报错

**认证 UI（Q21）🎨**
- [ ] `grep -r "WWW-Authenticate"` 全项目为空
- [ ] `grep -r "request.authorization"` 全项目为空
- [ ] `grep -r "meta.admin_password"` 全项目为空
- [ ] `_admin_required` 装饰器已删除
- [ ] 未登录访问受保护路由 → **JSON 401**（不弹浏览器原生对话框）
- [ ] 6 个认证界面全部复用 `AuthLayout`，视觉一致
- [ ] 4 断点 × 6 界面视觉走查通过
- [ ] 键盘全程可达（Tab 走一遍不用鼠标）
- [ ] 密码可见性切换 / 强度计 / 内联错误 / 加载态 / 锁定倒计时 全部可用
- [ ] 无内联 `<style>` 硬编码认证页面

**日志（Q20）**
- [ ] 7 个级别全部可用（TRACE / DEBUG / INFO / NOTICE / WARNING / ERROR / CRITICAL）
- [ ] 8 个业务类别标签可用且可过滤
- [ ] 模块级级别覆盖生效（如 `scrape=DEBUG` 只影响 scrape）
- [ ] 文本 / JSON 两种格式都可输出
- [ ] 文件轮转 + 保留天数生效
- [ ] **`openid` / `token` / `secret` / `password` 在任何级别都不出现**（正则断言测试通过）

**质量属性（Q17）**
- [ ] 安全检查清单全项通过（secrets / session / CSRF / SSRF / fail-closed / 容器加固）
- [ ] 故障注入测试通过（DB 不可写 / 学校 API 超时 / 飞书不可达 / 磁盘满）
- [ ] 边界用例集 ≥ 40 例全通过（负数 delta / 空数据 / NULL / 超长输入）
- [ ] 优雅关闭：SIGTERM 后调度器停止、正在跑的抓取完成

**解耦（Q18）**
- [ ] `docs/EXTENDING.md` 存在且 6 类扩展 SOP 完整
- [ ] 按 SOP 实测：加一个配置项（1 文件）/ 加一个命令（1 文件）均成功
- [ ] `Notifier` / `Endpoint` 协议有至少 1 个实现 + 1 个测试替身

**分发**
- [ ] `docker compose up -d` 在 amd64 + arm64 跑通
- [ ] armv7 镜像可 `docker pull` 并启动
- [ ] 离线包可 `docker load` 并启动
- [ ] `install.sh` 在干净的 amd64 VPS 上跑通
- [ ] **新用户按文档 15 分钟内完成部署**（实测）

**交付**
- [ ] 部署指南 / 配置说明 / FAQ / 升级回滚 文档齐备
- [ ] `CONTRIBUTING.md` 存在
- [ ] 3 张截图
- [ ] 现有 `records.db` 直接挂载可用，**零迁移**

### 6.3 生产切换验收（M7）

- [ ] `/healthz` 200 且三段探测均 ok
- [ ] 首页 4 段全部渲染（概览 / 历史 / 违规 / 电表）
- [ ] 登录 → Admin 5 页全部可用
- [ ] 未登录时 `/api/live` 返回 401；`public_readonly=1` 时返回 200
- [ ] 飞书群收到一次常规卡片
- [ ] 飞书私聊 9 个命令全部正常
- [ ] 定时抓取按 `*/10` 运行（看日志）
- [ ] L3 日报在 BJ 09:00 推送（或手动触发验证）
- [ ] **回滚演练**：停新 → 起旧 → 30 秒内恢复

---

## 7. 风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 | 阶段 |
|---|---|---|---|---|---|
| RK1 | **未提交成果丢失** | 中 | 🔴 极高 | M0.1 先落库 | M0 |
| RK2 | 重写静默丢功能 | 高 | 🔴 高 | 45 项行为快照 + 契约测试作为门禁 | M0–M5 |
| RK3 | `run_once` 副作用漏搬 | 中 | 🟠 中 | 集成守卫 + `--fetch-only` 冒烟 | M2 |
| RK4 | APScheduler 与 web 生命周期冲突 | 中 | 🟠 中 | 单 worker 断言 + 线程级兜底 + 幂等抓取 | M2 |
| RK5 | armv7 镜像构建超时/失败 | 中 | 🟠 中 | QEMU + 缓存；失败则 armv7 降级为"仅 Docker 离线包" | M6 |
| RK6 | 前端重做导致视觉/交互回归 | 高 | 🟡 低 | 4 断点 × 8 页面走查 + DOM 语义对齐 | M5 |
| RK7 | Docker 镜像体积过大影响分发 | 中 | 🟡 低 | 字体去重（省 17.3MB）+ 多阶段构建 | M6 |
| RK8 | 国内拉镜像慢 | 高 | 🟠 中 | 离线镜像包（N5） | M6 |
| RK9 | 切换期间新旧双写 DB | 低 | 🟠 中 | 切换 SOP 强制"停旧→起新" | M7 |
| RK10 | 工期超预期 | 高 | 🟡 低 | 每阶段独立可交付，可随时停 | 全程 |
| RK11 | 6 个已泄露 secrets 未轮换 | — | 🔴 高 | **与重写解耦，建议立即处理** | 独立 |
| RK12 | 新用户装不上（文档不够傻瓜） | 中 | 🟠 中 | M6 实测"新用户 15 分钟" | M6 |
| RK13 | **配置注册表遗漏项**（"所有配置可改"做不到 100%） | 高 | 🟠 中 | M1 做**全量常量审计**（grep 魔法数字）；验收时逐项勾选 | M1/M4 |
| RK14 | **secrets 加密后密钥丢失 → 配置不可解** | 低 | 🔴 高 | 启动时校验 `FLASK_SECRET_KEY`；导出备份提示；文档显式警告 | M1 |
| RK15 | 配置迁到 DB 后**首启顺序问题**（DB 未初始化时读不到配置） | 中 | 🟠 中 | `.env` 保留 4 项启动必需；注册表 default 兜底 | M1 |
| RK16 | **日志级别细分导致日志量爆炸**（TRACE/DEBUG 全开） | 中 | 🟡 低 | 默认 `INFO`；TRACE 仅手动开启；文件轮转 + 保留天数 | M1 |
| RK17 | 前端动态渲染配置表单的复杂度 | 中 | 🟠 中 | 先实现 11 种类型的渲染器 + 契约测试；复杂项用 `advanced` 折叠 | M5 |
| RK18 | 开关语义不一致（有的报错有的静默） | 中 | 🟠 中 | 统一在 `is_enabled()` 层拦截 + 逐开关的"静默降级"测试 | M4 |

---

## 8. 新增需求（Q15–Q22）的落点对照

| 需求 | 设计落点 | 里程碑 | 新增文件 |
|---|---|---|---|
| **Q15 所有配置 WebUI 可改** | §2.8 配置注册表 + 存储迁 DB + secrets 加密 + 导出导入 | M1(1.5) / M4(4.6) / M5(5.5) | `app/config_registry/` 5 文件 |
| **Q16 飞书机器人开关** | §2.9 功能开关注册表 + 静默降级 | M1(1.6) / M4(4.7) / M5(5.6) | `app/flags.py` |
| **Q17 安全/稳定/鲁棒** | §2.11 质量属性设计 + §2.8 安全措施 + 验收清单 | 全程 | — |
| **Q18 解耦扩展** | §2.12 `Notifier`/`Endpoint` 协议 + 扩展 SOP | M1(1.8) / M6(6.6) | `app/protocols.py` + `docs/EXTENDING.md` |
| **Q19 随机密码** | 安装时 `secrets.token_urlsafe(16)` + 强制首登改密 | M1(1.4) / M4(4.8) / M6(6.3-6.4) | — |
| **Q20 日志分级** | §2.10 7 级 + 类别 + 模块级覆盖 + 文本/JSON | M1(1.7) / M5(5.8) | `app/logging_setup.py` |
| **Q21 认证页面自主设计** | §2.13 移除 Basic Auth + `AuthLayout` + 6 个认证界面 | M4(4.9) / M5(5.3) | `layouts/AuthLayout.vue` + 6 view |
| **Q22 分支与文件策略** | §0.1 全新分支 + 干净文件树 + 顺序约束 | **M0(0.1–0.8)** | — |

**工期影响**：24 天 → **31 天**（M1 +2d / M4 +2d / M5 +2d / M6 +2d，含 Q21 的 +1d）
**M0 影响**：2 天 → **4 天**（新增"生成快照 → 再清空 legacy"两阶段）

---

## 9. 待办

| # | 事项 | 状态 |
|---|---|---|
| 1 | 你审阅 `SCOPE_DECISION.md` 的 105 项预分类 + 20 项新增，推翻/确认 | ⏳ |
| 2 | 分歧点 1：F6 密码恢复入口（建议删） | ⏳ |
| 3 | 分歧点 2：D5 飞书 fail-open（建议改 fail-closed） | ⏳ |
| 4 | **立即轮换 6 个已泄露 secrets**（TODO #23，与重写无关） | ⏳ 🔴 |
| 5 | **立即落库 R61–R67 的 24 项未提交成果**（M0.1） | ⏳ 🔴 |
| 6 | 确认 Q15 的一个边界：`.env` 只留 4 项启动必需 —— 是否同意？（见下方说明） | ⏳ |

### 关于待办 6 的说明

Q15 要求"所有配置 WebUI 可改"，但有一类配置**必须在 DB 打开之前就已知**，否则程序起不来：

| 项 | 为什么不能放 DB |
|---|---|
| `DB_PATH` | 不知道 DB 在哪，就打不开 DB |
| `FLASK_SECRET_KEY` | 用它派生 secrets 加密密钥；且它自己不能加密自己 |
| `FLASK_PORT` | 起服务前就要知道监听哪个端口 |
| `DORM_DATA_DIR` | 数据目录位置 |

**建议**：这 4 项留在 `.env`（安装时由 `install.sh` 写入），**其余全部迁到 DB 并可在 UI 改**。

---

## 10. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | draft-1 | 初稿：目标架构 + 7 个模块设计 + 8 个里程碑 + 测试/分发/验收/风险 |
| 2026-10-06 | draft-2 | **新增 Q15–Q20 六项需求**：§2.8 配置注册表 / §2.9 功能开关 / §2.10 日志子系统 / §2.11 质量属性 / §2.12 扩展点协议；里程碑扩为 30 天；DoD 增加 5 组验收；风险 +6 项 |

---

**方案结束** —— 本文档不含任何代码改动。


