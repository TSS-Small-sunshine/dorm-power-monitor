# R69 重构设计文档（REFACTOR_DESIGN）

> **状态**：设计草案（待评审） — **本文档不包含任何代码改动**
> **基线**：`develop` @ `bb031f6`（工作区含未提交 R61–R67 成果）
> **编写日期**：2026-10-06
> **适用范围**：`dorm-power-monitor` 全仓结构重构
> **铁律**：行为冻结 · 先建网再动刀 · 一次一模块 · 入口不变

---

## 0. 文档说明

本文档回答三个问题：

| 章节 | 问题 |
|---|---|
| §1 | **现状是什么**（基线、依赖关系、必须冻结的契约） |
| §2–§3 | **要变成什么**（目标架构、模块职责、接口契约） |
| §4–§7 | **怎么安全地变**（迁移步骤、测试策略、风险、验收标准） |

**不在本文档范围内**：新增功能、UI 改版、学校接口适配扩展、性能优化。

---

## 1. 现状基线

### 1.1 代码规模

| 文件 | 行数 | 职责密度 |
|---|---|---|
| `web.py` | 2,806 | 41 路由 / 7 类关注点 |
| `dorm_power.py` | 2,200 | HTTP + 解析 + 卡片 + 推送 + 算法 + CLI |
| `feishu_bot.py` | 1,879 | 渲染 + 上传 + token + 命令 + 加密 + 事件 |
| `auth.py` | 1,017 | 认证 + 会话 + 锁定 + 审计 + CSRF |
| `db/_legacy.py` | 727 | 冻结副本（注释明写"不要改"） |
| `db/repo.py` | 463 | **生产零调用** |
| `db/models.py` | 252 | **生产零调用** |
| `db/__init__.py` | 216 | 双重 re-export |
| `db.py` | 35 | shim |
| `config.py` | 146 | 常量 + 5 个 lazy getter |
| **合计** | **≈ 9,741** | |

辅助资产：8 模板 / 4 CSS / 15 JS / 41 测试 / 11 构建脚本 / 9 部署脚本 / git 跟踪 47 文件

### 1.2 现状依赖关系（含问题标注）

```
                         ┌──────────────┐
                         │  config.py   │◄─── 5 个 lazy getter 反向 import db
                         └──────┬───────┘
                                │
     ┌──────────────────────────┼──────────────────────────┐
     ▼                          ▼                          ▼
┌─────────┐  import    ┌──────────────┐   import    ┌──────────────┐
│ web.py  │───────────►│ feishu_bot.py│────────────►│ dorm_power.py│
│         │            │              │             │              │
│  ⚠ 2238 │────────────┼──────────────┼────────────►│              │
│  ⚠ 2882 │  直接调用私有函数 _post_feishu          │              │
└────┬────┘            └──────┬───────┘             └──────┬───────┘
     │                        │                            │
     └────────────────────────┴────────────────────────────┘
                              ▼
                     ┌─────────────────┐
                     │ db/  (三份真相) │
                     │ _legacy.py      │ ← 实际路径
                     │ models.py       │ ← 无人调用
                     │ repo.py         │ ← 无人调用
                     └─────────────────┘
```

**标注说明**
- ⚠ `web.py:2238` / `web.py:2882`：`from dorm_power import _post_feishu` —— 跨模块调用**私有函数**
- `db/` 内 `_legacy.py`（生产路径）与 `models.py`/`repo.py`（无人调用）**并行存在**，构成"三份真相"
- 推送职责被切分到两个模块：`_sign`/`_now_beijing`/`_is_due` 在 `dorm_power.py`；`_should_push`/`_in_quiet_hours`/`push_if_enabled` 在 `feishu_bot.py`

### 1.3 重复实现清单（重构必须消除）

| 重复项 | 位置 A | 位置 B | 处置 |
|---|---|---|---|
| `_post_feishu` | `dorm_power.py:1820` | `feishu_bot.py:2076` | 合并为 `notify/transport.py` |
| `_read_eqprice` | `web.py:338` | `dorm_power.py:449` | 合并为 `domain/pricing.py` |
| `_coerce_float` | `db/_legacy.py` | `dorm_power.py:568` | 合并为 `db/coerce.py` |
| OOBE 保存逻辑 | `web.py:1641-1970`（8 端点） | `web.py:1981-2140`（legacy 单端点） | 合并为 `services/oobe_service.py` |
| 卡片构建 | `dorm_power.py:1592-1818` | `feishu_bot.py:538-795`（PNG 版） | 统一走 `notify/card_builder.py` |
| `:root` 设计令牌 | `dashboard.css`（`#0a0a0b` 系） | `oobe/admin/login.css`（`#0f172a` 系） | 收敛为 `static/css/tokens.css` |

### 1.4 额外发现（R62 拆分未完成）

R68 发行说明称"`web.py` 用 `render_template()` + `url_for('static', ...)` 取代字符串拼接"，**与代码不符**：

| 证据 | 现状 |
|---|---|
| `web.py:658-667, 1396-1402, 2524-2527` | 8 个模板在**模块导入时**被 `_load_template()` 读成模块级字符串（`INDEX_HTML` / `OOBE_HTML` / `LOGIN_HTML` / `ADMIN_*_HTML`） |
| `web.py:696, 1627, 2493, 2538, 2803, 2819, 2926` | 全部使用 `render_template_string(...)` |
| `web.py` 中 `url_for` 出现次数 | **2** |
| `templates/*.html` 中硬编码 `/static/...` | **40** 处 |

**后果**：无法使用 Jinja 的 `{% extends %}` / `{% include %}`；模板改动必须重启进程；静态资源指纹/缓存策略无法实施。Phase 5 需一并修复。

### 1.5 🔴 基线风险：R61–R67 成果未提交

```
HEAD  = bb031f6  "R68: fill in actual SHA256 in release notes"
        a56dfcb  "R68: bundle R60-R67 deliverable"   ← 仅 5 个文件：
                 .env.example / R68_RELEASE_NOTES.md / README.md
                 / scripts/deploy/deploy_round68.sh / r68_env_migration.md

工作区未提交 24 项：
  未跟踪 ??  auth.py · db/ · templates/ · static/
             tests/modern/test_round61..67.py
  已修改 M   web.py (3,396 → 2,806 行) · db.py (779 → 35 行)
             requirements.txt · .env.example · nginx/dorm.conf
             tests/modern/test_round35/49/50.py
```

**判定**：`develop` 上**已提交的源码仍是 R51c 形态**（`web.py` 3,396 行内嵌 HTML、`db.py` 单文件 779 行、无 `auth.py`）。R61–R67 全部交付物（db 包化、auth 模块、模板拆分、Web Components、OOBE）**只存在于工作区**；R68 的 zip 是从工作区打包的。

**因此 Phase 0 的第一步（不可跳过、不可重排）就是把这 24 项落库**。否则任意 `git checkout` / `git clean` / 分支切换都会永久丢失整个 R60–R67 交付物。

---

## 2. 目标架构

### 2.1 分层规则（单向依赖，禁止反向）

```
Layer 5  entry     web.py / dorm_power.py / db.py        （兼容入口 shim）
Layer 4  web       app/web/                              （HTTP 适配层）
Layer 3  service   app/services/                         （用例编排）
Layer 2  domain    app/domain/                           （纯计算，零 IO）
         notify    app/notify/                           （外部系统适配）
         scraper   app/scraper/                          （外部系统适配）
Layer 1  infra     app/db/  app/config.py  app/auth/     （基础设施）
Layer 0  stdlib / 三方库
```

**强制约束（CI 用 import-linter 或 AST 守卫校验）**

| 规则 | 说明 |
|---|---|
| R1 | `app/domain/` **不得** import `requests` / `flask` / `sqlite3` / `app.db` |
| R2 | `app/notify/` 与 `app/scraper/` **不得**互相 import |
| R3 | `app/web/` **不得** import `app/notify/transport.py` 以外的 notify 内部实现（通过 service 层调用） |
| R4 | 任何 `app/**` **不得** import 顶层 `web` / `dorm_power` / `feishu_bot` |
| R5 | 跨层调用只允许"上层 → 下层"，同层只允许 `domain → infra` |
| R6 | 禁止跨模块 import 下划线私有符号（消除 `from dorm_power import _post_feishu`） |

### 2.2 目标目录树

```
dorm-power-monitor/
├── pyproject.toml                  ★新增  元数据 + ruff + mypy + pytest 配置
├── web.py                          保留   3 行 shim → app.web:main
├── dorm_power.py                   保留   3 行 shim → app.scraper.cli:main
├── db.py                           保留   shim（同现状）
├── config.py                       保留   shim → app.config
├── requirements.txt / requirements-dev.txt   ★拆分
│
├── app/
│   ├── __init__.py                         create_app 导出
│   ├── config.py                   ◆合并  config.py + 统一 Settings（.env 键名不变）
│   │
│   ├── db/                         ◆合并  _legacy.py + repo.py + models.py
│   │   ├── schema.py                        _SCHEMA 唯一真相
│   │   ├── connection.py                    get_conn / init / 迁移
│   │   ├── coerce.py                        _coerce_float / _coerce_str（唯一）
│   │   ├── models.py                        Pydantic v2 实体（真正被使用）
│   │   └── repositories.py                  唯一 SQL 入口
│   │
│   ├── auth/                       ◆拆分  auth.py
│   │   ├── models.py  service.py  csrf.py  decorators.py  audit.py
│   │
│   ├── domain/                     ★新增  纯计算，零 IO，100% 可单测
│   │   ├── metrics.py                       日均 / 月预计 / 剩余天数 / 差值
│   │   ├── pricing.py                       _read_eqprice 唯一实现
│   │   └── thresholds.py                    低电 / 离线 / stale 判定
│   │
│   ├── scraper/                    ◆拆分  dorm_power.py
│   │   ├── client.py                        Session + retry + UA/Referer
│   │   ├── endpoints.py                     F1–F5 fetcher（纯解析）
│   │   ├── service.py                       run_once / backfill 编排
│   │   └── cli.py                           main / --tick
│   │
│   ├── notify/                     ◆合并  dorm_power 卡片 + feishu_bot
│   │   ├── card_builder.py                  卡片 JSON（无 HTTP）
│   │   ├── renderer.py                      Pillow PNG
│   │   ├── transport.py                     唯一 _post_feishu / token / upload
│   │   ├── crypto.py                        AES / HMAC / 签名
│   │   ├── policies.py                      L1–L4 门控 + quiet hours + dedupe
│   │   └── dispatcher.py                    命令路由 + 事件处理
│   │
│   ├── services/                   ★新增  用例编排（web 与 cli 共享）
│   │   ├── dashboard_service.py             /api/live /api/data 数据装配
│   │   ├── oobe_service.py                  合并新旧两套 OOBE 保存逻辑
│   │   └── admin_service.py                 配置读写 / 用户管理 / 测试触发
│   │
│   └── web/
│       ├── factory.py                       create_app + 蓝图注册 + 错误处理
│       ├── security.py                      CSRF / 内部 token / 装饰器
│       ├── forms.py                         输入校验（密码强度 / URL / cron）
│       └── blueprints/
│           ├── dashboard.py  admin.py  oobe.py
│           ├── auth_api.py   feishu.py  health.py
│
├── static/
│   ├── css/tokens.css              ★新增  统一设计令牌（唯一 :root）
│   ├── css/{dashboard,oobe,admin,login}.css   ◆只保留布局与组件样式
│   ├── vendor/chart.umd.min.js     ★新增  Chart.js 本地化（去公网 CDN）
│   └── js/ (15 个，保持现状)
│
├── templates/                              ◆改 render_template + {% extends base.html %}
│   └── base.html                   ★新增  公共骨架
│
├── tests/
│   ├── conftest.py                 ★新增  统一 fixture
│   ├── unit/  integration/  regression/
│   └── legacy/                             归档旧 round 测试（只读）
│
└── scripts/ deploy/ nginx/ docs/           保持现状
```

★ = 新增 ｜ ◆ = 重构/合并

### 2.3 模块职责表

| 模块 | 唯一职责 | 允许依赖 | 迁移自 |
|---|---|---|---|
| `app/config.py` | 读取/校验配置，暴露不可变 Settings 对象 | `os` / `pydantic-settings` | `config.py` |
| `app/db/schema.py` | 定义 `_SCHEMA` 与迁移版本 | — | `db/_legacy.py` |
| `app/db/connection.py` | 连接生命周期、WAL、`init()` | `app.db.schema` | `db/_legacy.py` |
| `app/db/repositories.py` | 所有 SQL 语句（唯一出口） | `app.db.*` | `db/_legacy.py` + `db/repo.py` |
| `app/auth/*` | 用户/会话/锁定/审计/CSRF | `app.db` | `auth.py` |
| `app/domain/*` | 纯函数计算，**零 IO** | stdlib only | `web.py` / `dorm_power.py` 算法段 |
| `app/scraper/client.py` | HTTP 会话、重试、伪装头 | `requests` | `dorm_power.py:122-509` |
| `app/scraper/endpoints.py` | F1–F5 请求 + 响应解析 | `app.scraper.client` | `dorm_power.py:577-978` |
| `app/scraper/service.py` | `run_once` / backfill 编排 | `scraper.*` + `notify.policies` | `dorm_power.py:2126-2394` |
| `app/scraper/cli.py` | 命令行入口 / `--tick` | `scraper.service` | `dorm_power.py:2421` |
| `app/notify/card_builder.py` | 构造卡片 JSON（无 HTTP） | `app.domain` | `dorm_power.py:1468-1818` |
| `app/notify/renderer.py` | Pillow PNG 渲染 | `PIL` | `feishu_bot.py:169-795` |
| `app/notify/transport.py` | **唯一** webhook POST / token / 上传 | `requests` | 两个 `_post_feishu` |
| `app/notify/policies.py` | L1–L4 门控、静默时段、dedupe | `app.db` | `feishu_bot.py:2034-2110` + `dorm_power.py:1076-1238` |
| `app/notify/dispatcher.py` | 命令路由、事件处理 | `notify.*` | `feishu_bot.py:1186-2010` |
| `app/services/*` | 用例编排（web 与 cli 共享） | `domain` + `db` + `notify` + `scraper` | `web.py` 路由体 |
| `app/web/blueprints/*` | 仅 HTTP 适配：解析请求 → 调 service → 序列化 | `app.services` | `web.py` 41 路由 |

### 2.4 目标依赖矩阵（✅ 允许 ｜ ❌ 禁止）

| ↓依赖→ | config | db | auth | domain | scraper | notify | services | web |
|---|---|---|---|---|---|---|---|---|
| `config` | — | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `db` | ✅ | — | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| `auth` | ✅ | ✅ | — | ❌ | ❌ | ❌ | ❌ | ❌ |
| `domain` | ✅ | ❌ | ❌ | — | ❌ | ❌ | ❌ | ❌ |
| `scraper` | ✅ | ✅ | ❌ | ✅ | — | ✅ | ❌ | ❌ |
| `notify` | ✅ | ✅ | ❌ | ✅ | ❌ | — | ❌ | ❌ |
| `services` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ❌ |
| `web` | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ | ✅ | — |

> 关键变化：`config` 不再反向 import `db`（当前 `config.py:144-176` 的 5 个 lazy getter 需要 `db.get_meta`，目标改为由 `app/services/settings_service.py` 读取 meta 后注入，或用回调注册）。

---

## 3. 接口契约（冻结清单）

> **本节所有条目在重构中必须逐字节保持不变**。Phase 0 会为每一条生成自动化快照测试；Phase 2–6 每次提交都必须通过。

### 3.1 HTTP 契约 —— 41 个路由

| # | 路径 | Methods | 装饰器 | 目标蓝图 |
|---|---|---|---|---|
| 1 | `/` | GET | — | `dashboard` |
| 2 | `/api/data` | GET | — | `dashboard` |
| 3 | `/api/live` | GET | — | `dashboard` |
| 4 | `/api/refresh` | POST | auth(admin) + csrf | `dashboard` |
| 5 | `/healthz` | GET | — | `health` |
| 6 | `/feishu/event` | POST | 飞书签名校验 | `feishu` |
| 7 | `/feishu/event` | GET | — | `feishu` |
| 8 | `/api/auth/login` | POST | — | `auth_api` |
| 9 | `/api/auth/logout` | POST | — | `auth_api` |
| 10 | `/api/auth/me` | GET | — | `auth_api` |
| 11 | `/oobe` | GET | — | `oobe` |
| 12 | `/admin/oobe` | GET | — | `oobe` |
| 13 | `/api/oobe/save-state` | POST | — | `oobe` |
| 14 | `/api/oobe/next` | POST | — | `oobe` |
| 15 | `/api/oobe/prev` | POST | — | `oobe` |
| 16 | `/api/oobe/skip-step` | POST | — | `oobe` |
| 17 | `/api/oobe/validate-feishu` | POST | — | `oobe` |
| 18 | `/api/oobe/validate-webhook` | POST | — | `oobe` |
| 19 | `/api/oobe/complete` | POST | — | `oobe` |
| 20 | `/admin/api/oobe/save` | POST | `_admin_required` | `oobe`（legacy 兼容） |
| 21 | `/admin` | GET | auth(admin) | `admin` |
| 22 | `/admin/api/scrape/config` | GET, POST | auth(admin) + csrf | `admin` |
| 23 | `/admin/api/push/config` | GET, POST | auth(admin) + csrf | `admin` |
| 24 | `/admin/api/test-push` | POST | auth(admin) + csrf | `admin` |
| 25 | `/admin/api/admin-password` | POST | auth(admin) + csrf | `admin` |
| 26 | `/admin/set-password` | GET, POST | — | `admin` |
| 27 | `/admin/api/import-url` | POST | auth(admin) + csrf | `admin` |
| 28 | `/login` | GET, POST | — | `auth_api` |
| 29 | `/admin/users` | GET | auth(admin) | `admin` |
| 30 | `/api/admin/users` | GET | auth(admin) + csrf | `admin` |
| 31 | `/api/admin/users` | POST | auth(admin) + csrf | `admin` |
| 32 | `/api/admin/users/<int:user_id>` | PUT | auth(admin) + csrf | `admin` |
| 33 | `/api/admin/users/<int:user_id>` | DELETE | auth(admin) + csrf | `admin` |
| 34 | `/admin/config` | GET | auth(admin) | `admin` |
| 35 | `/admin/test` | GET | auth(admin) | `admin` |
| 36 | `/api/admin/test-scrape` | POST | auth(admin) + csrf | `admin` |
| 37 | `/api/admin/test-push` | POST | auth(admin) + csrf | `admin` |
| 38 | `/admin/audit` | GET | auth(admin) | `admin` |
| 39 | `/api/admin/audit` | GET | auth(admin) + csrf | `admin` |
| 40 | `/api/admin/config` | GET | auth(admin) + csrf | `admin` |
| 41 | `/api/admin/config` | PUT | auth(admin) + csrf | `admin` |

**快照测试要求**：`{url_rule, methods, endpoint_prefix}` 三元组集合必须与上表**完全一致**（顺序无关，集合相等）。

### 3.2 JSON 契约

#### `GET /api/data?hours=24|72|168|720[&start=&end=]`

```json
{
  "hours": 24,
  "stats": {
    "remain": 12.34,        // float | null
    "hourly_used": 0.56,    // float | null
    "read_time": "2026-10-06 12:00:00",  // string | null
    "daily_avg": 1.23       // float | null
  },
  "rows": [
    { "id": 1, "ts": "2026-10-06 12:00:00", "read_time": "2026-10-06 11:59:00", "remain": 12.34 }
  ]
}
```

- `start` / `end` 显式区间**优先于** `hours`（`web.py:711-714`）
- `remain` 保留 2 位小数（`_stats` 中 `round(latest_remain, 2)`）

#### `GET /api/live`

```json
{
  "stats": { "remain": null, "hourly_used": null, "read_time": null, "daily_avg": null },
  "run_status": {
    "vol": 220.12, "cur": 0.456, "yggl": 12.345,
    "run_status": "在线", "update_dt": "2026-10-06 12:00:00"
  },
  "latest_ts": "2026-10-06 12:00:00",
  "scrape_status": "ok",       // "ok" | "stale" | "failed" | "unknown"
  "stale": false,              // 恒等于 scrape_status == "stale"
  "eqprice": 0.5,
  "monthly_projection": 123.45,
  "monthly_breakdown": {
    "eqprice": 0.5, "used_kwh": 10.0, "days_observed": 6,
    "days_left": 25, "avg_daily": 2.0, "monthly_projection": 123.45
  }
}
```

- `run_status` 整体为 `null` 或**全部 5 个键存在**（不允许部分缺键）
- `vol`/`cur`/`yggl` 保留 **3** 位小数（`_round3`）
- `stats_rows` 固定取 **168h** 窗口（`web.py:759`）
- `monthly_projection` 与 `monthly_breakdown.monthly_projection` 必须一致
- `eqprice` 回退链：`meta.eqprice` → `DORM_EQPRICE` → `0.5`（`web.py:338-360`）

### 3.3 数据库契约（10 张表，字段名冻结）

| 表 | 主键 | 字段 | 来源 |
|---|---|---|---|
| `records` | `id` AUTOINCREMENT，`ts` UNIQUE | `id, ts, read_time, remain` | R1 / R49 |
| `daily_elec` | `(roomId, dt)` | `roomId, dt, total_eq, esbm, eebm, zong_eq` | R2 |
| `violations` | `(roomId, dt, wg_reason)` | `roomId, dt, wg_reason, wg_power` | R2 |
| `pay_history` | `(roomId, dt, ...)` | `roomId, dt, pay_type, fee_type, money` | R2 |
| `run_status` | `roomId` | `roomId, meter_no, dt, run_status, work_status, update_dt, vol, cur, yggl, stop_reason` | R2 |
| `meta` | `key` | `key, value`（value 恒为 TEXT） | R2 / R34A |
| `users` | `id` AUTOINCREMENT | `id, username(UNIQUE), password_hash, role, created_at, last_login_at` | R63 |
| `sessions` | `id`（`token` UNIQUE） | `id, user_id, token, expires_at, created_at, ip, user_agent` | R63 |
| `failed_attempts` | `id` | `id, username, ip, attempted_at` | R63 |
| `audit_log` | `id` | `id, user_id, action, target, ip, user_agent, created_at, details` | R63 |

**索引**（11 个）：
`idx_records_ts` · `idx_daily_elec_dt` · `idx_violations_dt` · `idx_pay_history_dt` ·
`idx_sessions_token` · `idx_sessions_expires` · `idx_failed_attempts_username` ·
`idx_failed_attempts_time` · `idx_audit_user` · `idx_audit_action` · `idx_audit_time`

**DB 级时间默认值**：`users` / `sessions` / `failed_attempts` / `audit_log` 的 `created_at`
用 `DEFAULT (datetime('now', '+8 hours'))` —— SQLite 的 `datetime('now')` **恒为 UTC**，
`+8 hours` 后即北京时间。**该默认值与服务器时区无关**，是安全的（Q10 方案 B 下无需改动）。

**迁移策略**：`init()` 必须保持幂等（`CREATE TABLE IF NOT EXISTS` + `DROP COLUMN raw_html` 安全 no-op）

**禁止事项**（AGENTS.md 硬约束 #6）：不改字段名、不改表名、不删列。

### 3.4 关键函数契约（签名冻结）

```python
# app/scraper/service.py —— web.py 与 cron 共同依赖
def run_once(fetch_only: bool = False) -> Optional[tuple[dict, str]]: ...
def fetch_once() -> dict: ...

# app/scraper/cli.py —— cron 命令行
def main(argv: list[str] | None = None) -> None: ...   # 支持 --tick / --fetch-only

# app/notify/transport.py —— 唯一推送出口
def post_card(card: dict, *, raise_on_error: bool = False) -> None: ...
def post_text(text: str) -> None: ...

# app/notify/policies.py
def should_push(layer: str) -> bool: ...
def in_quiet_hours() -> bool: ...
def push_if_enabled(layer: str, card: dict) -> bool: ...

# app/notify/dispatcher.py
def handle_event(body: dict, room_id: Optional[str]) -> dict: ...
def dispatch_text(text: str, room_id: Optional[str]) -> dict: ...

# app/db/repositories.py —— 兼容旧 db.* 调用（通过 db/__init__ re-export 保留）
def insert(remain, read_time=None) -> None: ...          # 2 参数（R36 契约）
def query(hours=24, start_dt=None, end_dt=None) -> list[dict]: ...
def latest() -> Optional[dict]: ...
def get_meta(key) / set_meta(key, value) -> ...
def get_run_status(room_id) / upsert_run_status(...) -> ...
def recent_daily_elec(room_id, days) / record_daily_elec(...) -> ...
def recent_violations(room_id, days) / record_violation(...) -> ...
def recent_pay(room_id, limit) / record_pay(...) -> ...
def record_cadence_status(...) / set_scrape_status(...) -> ...
def init() / get_conn() -> ...

# app/domain/metrics.py（新增，纯函数）
def compute_stats(rows: list[dict]) -> dict: ...
def compute_monthly_projection(eqprice, stats, room_id) -> dict: ...
def calc_days_remaining(remain_kwh, ...) -> ...: ...
```

### 3.5 飞书卡片契约

| 卡片 | 触发 | 必含字段 | 现状位置 |
|---|---|---|---|
| 常规 stat card | 每次成功抓取 | `header.title` / `elements[].fields` / footer 时间戳 | `dorm_power.py:1592` |
| 低电红卡 | `remain < THRESHOLD` | 剩余 + 7 天均值 | `dorm_power.py:1923` |
| 离线红卡 | `run_status != 0` | 电表状态 | `dorm_power.py:1779` |
| stale 黄卡 | 抓取失败 | 兜底 body | `dorm_power.py:957` |
| L3 日报/周报/月报 | tick 门控 | 区间汇总 | `dorm_power.py:1468` |
| PNG stat card | 私聊命令 | 800×600，MiSans + NotoEmoji | `feishu_bot.py:538-795` |

**禁止**：改动 `card` dict 的键名、`header.template` 取值、命令文案（`HELP_TEXT`）、`COMMANDS` / `MENU_KEYS` 映射表。

### 3.6 配置契约（`.env` 键名冻结）

```
FEISHU_WEBHOOK  FEISHU_SECRET  FEISHU_APP_ID  FEISHU_APP_SECRET
FEISHU_VERIFICATION_TOKEN  FEISHU_ENCRYPT_KEY
DORM_OPENID  DORM_BASE_URL  DORM_ROOM_ID  DORM_EQPRICE
DB_PATH  SCRAPE_INTERVAL_MINUTES
FLASK_HOST  FLASK_PORT  FLASK_DEBUG  FLASK_SECRET_KEY
DORM_COOKIE_SECURE
AUTH_INITIAL_ADMIN_USERNAME  AUTH_INITIAL_ADMIN_PASSWORD
API_INTERNAL_TOKEN
```

**meta 表键名同样冻结**（29 个常量，见 `db/_legacy.py:135-168` + `web.py` 运行时键）：
`last_room_id` `eqprice` `last_scrape_at` `last_scrape_status` `backfill_done` `oobe_step` `oobe_completed` `admin_password` `dorm_openid` `dorm_base_url` `dorm_room_id` `feishu_webhook_url` `api_internal_token` `push_l1_enable` `push_l2_enable` `push_daily_enable` `push_weekly_enable` `push_monthly_enable` `push_daily_time` `push_weekly_time` `push_monthly_time` `push_receivers_l1` `push_receivers_l2` `push_receivers_report` `push_receivers_alert` `quiet_hours_start` `quiet_hours_end` `last_daily_report_date` `last_weekly_report_iso` `last_monthly_report_mo` `last_low_battery_alert_at` `last_violation_alert_at` `last_stale_alert_at`

### 3.7 前端契约

| 项 | 契约 |
|---|---|
| 数据注入 | `window.__DASHBOARD_DATA__ = { hours, rows, daily_elec, initial_latest_ts }` |
| Web Components 标签 | `<dorm-stat-card>` `<dorm-room-card>` `<dorm-list-row>` `<dorm-history-row>` `<dorm-records-table>` `<dorm-chart-container>` `<dorm-spinner>` `<dorm-toast>` |
| 关键 DOM id | `#chart` `#range-group` `#refresh-btn` `#theme-toggle` `#hero-status-dot` `#hero-status-text` `#status-pill` `#status-bar` `#toast-container` `#last-update` |
| CSS 关键类 | `.topnav` `.hero` `.status-bar` `.panel` `.data-table` `.toast` `.range-btn` `.refresh-btn` `.icon-btn` `.grid-3` |
| 断点 | ≤767 / 768–1023 / 1024–1439 / ≥1440 |
| 轮询间隔 | 30 s（`/api/live`） |
| localStorage 键 | `dorm-power-monitor.section` |
| 时区处理 | `ts` 为**朴素本地时间（CST）**，JS 侧补 `+08:00` 解析 |

---

## 4. 迁移步骤

> **通用约定**
> - 分支：`refactor/r69`（从 `develop` 切出）
> - 每个 Phase 结束打一个 tag：`r69-p0`、`r69-p1` …
> - 每个 Phase 必须**独立可部署**：部署后生产行为与前一版**完全一致**
> - 每个 Phase 结束必须满足：`pytest` 全绿 + `ruff check` 零错误 + 契约快照未变
> - 回滚方式统一为：`git revert <phase-merge-commit>` 或切回上一个 `r69-pN` tag

---

### Phase 0 — 基线落库 + 测试安全网（1.5 天）

**目标**：把不可逆风险清零，并建立能挡住"重构改坏契约"的回归网。

**前置**：无（这是第一步）

**操作步骤**

| 步 | 动作 | 命令 / 说明 |
|---|---|---|
| 0.1 | **备份工作区**（防手滑） | 复制整个 `dorm-power-monitor/` 到工作区外；记录 `git status` 输出到 `docs/reports/r69_baseline_status.txt` |
| 0.2 | 从 `develop` 切分支 | `git checkout -b refactor/r69` |
| 0.3 | **落库未提交的 24 项** | 分 3 个提交：<br>① `chore: add R61-R63 data layer + auth module`（`db/`、`auth.py`、`db.py`）<br>② `chore: add R62 template/static split`（`templates/`、`static/`、`web.py`）<br>③ `chore: add R61-R67 tests + config updates`（`tests/modern/test_round61-67.py`、`requirements.txt`、`.env.example`、`nginx/dorm.conf`、`tests/modern/test_round35/49/50.py`） |
| 0.4 | 推分支 + 建 PR | `git push -u origin refactor/r69` |
| 0.5 | 新增 `pyproject.toml` | 含 `[project]` 元数据、`[tool.ruff]`（line-length=100，`select = ["E","F","W","I","UP","B"]`）、`[tool.mypy]`（先 `ignore_missing_imports=true`，`app/domain/` 强制严格）、`[tool.pytest.ini_options]`（`testpaths=["tests/unit","tests/integration","tests/regression"]`） |
| 0.6 | 拆分依赖文件 | `requirements.txt`（运行时，保持现有内容）+ `requirements-dev.txt`（`pytest` `pytest-cov` `ruff` `mypy` `import-linter`） |
| 0.7 | 新增 `tests/conftest.py` | fixtures：`tmp_db`（`file:memdb{n}?mode=memory&cache=shared`）、`flask_client`（`create_app(testing=True)`）、`fixed_now`（冻结时间）、`sample_rows` |
| 0.8 | **写 5 类契约快照测试**（放 `tests/regression/`） | ① `test_route_contract.py` — §3.1 的 41 条三元组集合<br>② `test_schema_contract.py` — §3.3 的 10 张表字段名 + 索引<br>③ `test_api_contract.py` — §3.2 的 `/api/data`、`/api/live` 键集合与小数位<br>④ `test_card_contract.py` — §3.5 的卡片键集合<br>⑤ `test_config_contract.py` — §3.6 的 `.env` 键与 `meta` 键常量 |
| 0.9 | 修 18 个测试的硬编码路径 | `PROJ_DIR = Path("D:/MiniMax_Workstation/...")` → `Path(__file__).resolve().parents[2]` |
| 0.10 | 迁移测试目录 | `tests/modern/test_round35-50` → `tests/regression/`（保留原名）；`test_round61-67` 中纯 AST 守卫的移入 `tests/regression/`，其余留 `tests/legacy/` |
| 0.11 | 更新 CI | `.github/workflows/ci.yml` 增加：`pip install -r requirements-dev.txt`、`ruff check .`、`pytest -q`、保留原 AST guard + secret scan |

**验证**
```bash
pytest -q                       # 全绿
ruff check .                    # 0 error
python -m py_compile config.py db.py web.py feishu_bot.py dorm_power.py auth.py
python -c "import ast,glob;[ast.parse(open(f,encoding='utf-8').read()) for f in glob.glob('tests/**/*.py',recursive=True)]"
```

**回滚**：删除 `refactor/r69` 分支，`develop` 不受影响（Phase 0 全部在新分支）

**产出**：`refactor/r69` 分支、`pyproject.toml`、`requirements-dev.txt`、`tests/conftest.py`、5 个契约测试、绿色 CI

**⚠ 注意**：Phase 0 的 0.3 步是本项目**唯一不可逆风险点**。执行前必须确认本地已有完整副本。

---

### Phase 1 — 仓库卫生（0.5 天）

**目标**：让 `git status` 干净，移除干扰项。

**前置**：Phase 0

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 1.1 | 删除项目内临时文件 | `_tmp_admin_rendered.html`、`_tmp_oobe_rendered.html`、`_tmp_check_r48_regex.py`、`_tmp_check_zip.py`、`_tmp_inspect.py`、`_tmp_r62_split.py`、`_tmp_r62_web.py`、`_tmp_render_test.py`、`_tmp_index_old.txt`、`_tmp_r51_new_index.txt`、`_tmp_r48_out*.txt`、`diff_tests.txt` |
| 1.2 | 处理 `preview_v2.html` | 与 `templates/dashboard.html` 重复 → 移入 `docs/design/preview_v2.html` 并加 `<!-- ARCHIVED: superseded by templates/dashboard.html -->` |
| 1.3 | 删除 `records.db`、`tests/modern/records.db` | 含真实数据；`.gitignore` 已含 `records.db`（确认后删除） |
| 1.4 | 归档工作区根目录副本 | `_dorm_patch_scratch/`、`_dorm_zip_scratch/`、`_dorm_zip_verify/`、`_r22_stage/`、`_r22_verify/`、`_stage_zip_round6/`、`.round4_staging/`、`.round5_staging/`、`.round5_verify/`、`.audit-r46-verify/`、`.audit-r47-verify-backup/`、`backups/` → 移到 `../_archive_r68/`（项目目录外，避免污染仓库） |
| 1.5 | 归档 zip | `dorm-power-monitor*.zip*` → 同上 |
| 1.6 | 补 `.gitignore` | 加 `_probe*.txt`、`*.orig`、`.ruff_cache/`、`.mypy_cache/` |
| 1.7 | 归档旧构建脚本 | `scripts/build/r35-r50_build_zip.py`（10 个）→ `scripts/build/archive/`，保留 `build_round68.py` 作为模板 |
| 1.8 | 修正文档 | `docs/TECHNICAL.md` 顶部加 `> ⚠ 本文档描述 R42 状态，已过时；最新架构见 docs/REFACTOR_DESIGN.md`；`R68_RELEASE_NOTES.md` 的 R62 章节修正为"模板已抽取为文件，但仍以 `render_template_string` 渲染" |

**验证**：`git status --short` 输出为空；`pytest -q` 仍全绿

**回滚**：`git revert`（Phase 1 全是删除/移动，无逻辑变更）

**产出**：干净仓库

---

### Phase 2 — 数据层收口（1 天）

**目标**：消除"三份真相"，`db/` 只保留一份实现。

**前置**：Phase 0（契约测试必须已存在）

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 2.1 | 建 `app/db/schema.py` | 从 `db/_legacy.py:170-260` 原样搬 `_SCHEMA` + `META_*` 常量 + `_migrate_*` 函数 |
| 2.2 | 建 `app/db/connection.py` | 从 `db/_legacy.py` 搬 `get_conn()` / `init()` / `DB_PATH` 读取（改从 `app.config` 取） |
| 2.3 | 建 `app/db/coerce.py` | 搬 `_coerce_float` / `_coerce_str`（唯一实现） |
| 2.4 | 建 `app/db/models.py` | 从 `db/models.py` 原样搬（Pydantic v2 实体，内容不变） |
| 2.5 | 建 `app/db/repositories.py` | **合并** `db/_legacy.py` 的 6 个 `record_*`/`recent_*`/`insert`/`query`/`latest` + `db/repo.py` 的 6 个 `*Repo` 类。函数式 API 保持签名，`*Repo` 类保留为面向对象的补充接口 |
| 2.6 | 改写 `db/__init__.py` | 变为 `from app.db.repositories import *` + `from app.db.models import *` 的 re-export，**`__all__` 逐项保持不变** |
| 2.7 | 删除 `db/_legacy.py`、`db/repo.py`、`db/models.py` | 内容已迁移 |
| 2.8 | `db.py`（根 shim）不变 | 保持 35 行 |
| 2.9 | 更新调用方 import | `web.py` / `dorm_power.py` / `feishu_bot.py` / `auth.py` 的 `import db` 保持不变（re-export 兜住） |

**验证**
```bash
pytest -q tests/regression/test_schema_contract.py   # 表/字段/索引不变
pytest -q                                            # 全部 41 个旧测试仍绿
python -c "import db; print(db.RecordRepo, db.insert, db.META_LAST_DAILY_REPORT)"
```

**回滚**：`git revert`（Phase 2 是纯搬迁 + re-export，无行为变更）

**产出**：`app/db/` 5 文件，`db/` 变薄壳

**风险**：`db/__init__.py` 的 `__all__` 漏项 → 由 `test_round61.py` 的符号守卫测试捕获

---

### Phase 3 — 通知层收口（1.5 天）

**目标**：消除双份推送实现与私有函数跨模块调用。

**前置**：Phase 2

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 3.1 | 建 `app/notify/crypto.py` | 搬 `feishu_bot.py:1619-1867`（AES 解密 3 条路径 + SHA256 验签 + Lark 签名） |
| 3.2 | 建 `app/notify/transport.py` | **合并** `dorm_power._post_feishu`（1820）+ `feishu_bot._post_feishu`（2076）→ 一个 `post_card()`，参数取并集：`(card, *, raise_on_error=False, webhook_url=None)`；同时搬 `_sign`（`dorm_power.py:1076`）、`get_tenant_token`、`_upload_image`、`reply_card`、`reply_text`、`send_text_message` |
| 3.3 | 建 `app/notify/card_builder.py` | 搬 `dorm_power.py:1468-1818`（`_build_card` / `_build_l3_card` / `_build_offline_card` / `_kv_cell` / `_template_for_remain` / `_fmt_*`），**移除所有 HTTP 调用** |
| 3.4 | 建 `app/notify/renderer.py` | 搬 `feishu_bot.py:169-795`（字体加载 + 8 个 `_card_*` + `_to_png_bytes` + emoji 分段绘制） |
| 3.5 | 建 `app/notify/policies.py` | 搬 `feishu_bot.py:2034-2110`（`_should_push` / `_in_quiet_hours` / `push_if_enabled`）+ `dorm_power.py:1076-1238`（`_read_push_time` / `_l3_due` / `_stamp_l3` / `_is_due` / `_stamp_now` / `_now_beijing`）→ 统一为 `should_push` / `in_quiet_hours` / `push_if_enabled` / `is_due` / `stamp_now` |
| 3.6 | 建 `app/notify/dispatcher.py` | 搬 `feishu_bot.py:1186-2010`（9 命令 + `_data_*` + `dispatch_text` / `dispatch_menu` / `handle_event` / `verify_token`） |
| 3.7 | 改写 `dorm_power.py` 与 `feishu_bot.py` 为 re-export shim | 保留所有原函数名（含下划线私有名），内部转调 `app.notify.*`。**这是过渡态，Phase 4/5 会进一步收敛** |
| 3.8 | **消除 `web.py:2238` / `web.py:2882` 的 `from dorm_power import _post_feishu`** | 改为 `from app.notify.transport import post_card` |
| 3.9 | 保持 `dorm_power.run_once` 对 `feishu_bot` 的调用不变 | 仍走 shim，确保 Phase 3 可独立部署 |

**验证**
```bash
pytest -q tests/regression/test_card_contract.py
pytest -q tests/regression/
grep -rn "from dorm_power import _" --include=*.py .   # 必须为空
```

**回滚**：`git revert`（shim 保证调用方零改动）

**产出**：`app/notify/` 6 文件；两个旧模块变薄壳

**风险**：两个 `_post_feishu` 的差异（`raise_on_error` vs `webhook_url` 参数、异常处理策略）→ 用参数并集 + 显式测试覆盖两条路径

---

### Phase 4 — 抓取器拆分（1.5 天）

**目标**：把 `dorm_power.py` 的 5 类职责分离。

**前置**：Phase 3

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 4.1 | 建 `app/domain/pricing.py` | **合并** `web.py:338 _read_eqprice` + `dorm_power.py:449 _read_eqprice` → 唯一实现；回退链 `meta → DORM_EQPRICE → 0.5` 保持 |
| 4.2 | 建 `app/domain/metrics.py` | 搬 `dorm_power.py:593-680`（`calc_days_remaining` / `compute_avg_daily_from_cumulative`）+ `dorm_power.py:1239-1467`（`compute_daily/weekly/monthly_summary`）+ `web.py:263 _stats` + `web.py:366 _compute_monthly_projection`。**零 IO 化**：`room_id` 数据由调用方查询后传入 |
| 4.3 | 建 `app/domain/thresholds.py` | 搬 `dorm_power.py:1869 _is_offline` / stale 判定 |
| 4.4 | 建 `app/scraper/client.py` | 搬 `dorm_power.py:122-509`（`retry_school` / `_find_http_cause` / `_find_transport_cause` / `_validate_openid` / `_build_url` / `_safe_path` / `_safe_room_tail` / `_fetch_html` / `_post_form` / UA / Referer） |
| 4.5 | 建 `app/scraper/endpoints.py` | 搬 `dorm_power.py:577-978`（`_parse_input_attrs` / `_discover_room` / `_fetch_data` / F1–F5 全部 fetcher / `_as_list` / `_date_range`） |
| 4.6 | 建 `app/scraper/service.py` | 搬 `dorm_power.py:982-1071`（`_backfill_once`）+ `2126-2394`（`run_once`）+ `2395-2420`（`fetch_once`） |
| 4.7 | 建 `app/scraper/cli.py` | 搬 `dorm_power.py:2421+`（`main` / `--tick` / `--fetch-only` 参数解析） |
| 4.8 | 改写 `dorm_power.py` 为 3 行 shim | `from app.scraper.cli import main` + `if __name__ == "__main__": main()`；同时 re-export `run_once` / `fetch_once` 供 `web.py` 使用 |
| 4.9 | `web.py` 改 import | `from dorm_power import fetch_once` → `from app.scraper.service import fetch_once` |

**验证**
```bash
pytest -q tests/unit/test_metrics.py tests/unit/test_endpoints.py
python dorm_power.py --fetch-only            # 冒烟（不推送）
python -c "from app.scraper.service import run_once; print(run_once.__doc__)"
```

**回滚**：`git revert`；`dorm_power.py` shim 保证 cron 命令行不变

**产出**：`app/domain/` 3 文件、`app/scraper/` 4 文件；`dorm_power.py` → 3 行

**风险**：`run_once` 内部有大量 `db.set_meta` 副作用，拆分时容易漏 → 用 `test_round34a.py` 集成守卫兜住

---

### Phase 5 — Web 蓝图化 + OOBE 合并 + Jinja 修复（2 天）

**目标**：拆掉 2,806 行巨石；合并 OOBE 双后端；修复 §1.4 的模板加载缺陷。

**前置**：Phase 4

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 5.1 | 建 `app/web/factory.py` | `create_app(config=None) -> Flask`；注册 6 个蓝图；迁移 `after_request`（CSRF cookie 传播）、`context_processor`（`csrf_token`）、`secret_key`、cookie 配置、`db.init()` / `auth.ensure_initial_admin()` 引导 |
| 5.2 | 建 `app/web/security.py` | 搬 `_check_internal_token` / `_abort_401` / `_safe_url_for_log` / `_admin_required`（`web.py:1160-1257`） |
| 5.3 | 建 `app/web/forms.py` | 搬 `_parse_school_url` / `_validate_password_strength` / `_webhook_host_allowed` / `_validate_webhook_url` / `_validate_cron_expr`（`web.py:1258-1530`） |
| 5.4 | 建 `app/services/dashboard_service.py` | 搬 `_current_room_id` / `_build_round2_context` 的**装配逻辑**（计算下沉到 `app/domain/`） |
| 5.5 | 建 `app/services/oobe_service.py` | **合并** `/api/oobe/*`（8 端点逻辑）与 `/admin/api/oobe/save`（legacy）。抽出 `save_step(step, data) -> (ok, next_step, error)`，两端点共用。**legacy 端点保持原 URL 与原响应结构（含 `{"ok":..., "next_step":...}`）** |
| 5.6 | 建 `app/services/admin_service.py` | 搬 scrape/push config 读写、用户 CRUD、test-scrape/test-push 触发、audit 查询 |
| 5.7 | 建 6 个蓝图 | `dashboard.py`(4) / `health.py`(1) / `feishu.py`(2) / `auth_api.py`(4) / `oobe.py`(9) / `admin.py`(21)。**每个 handler 只做：解析请求 → 调 service → 序列化** |
| 5.8 | **修复模板渲染** | ① 新建 `templates/base.html`；② 8 个模板改 `{% extends "base.html" %}`；③ 删除 `_load_template()` 与 8 个模块级 `*_HTML` 常量；④ 全部 `render_template_string(X, ...)` → `render_template("x.html", ...)`；⑤ 40 处硬编码 `/static/...` → `{{ url_for('static', filename='...') }}` |
| 5.9 | 改写 `web.py` 为 shim | `from app.web.factory import create_app` + `app = create_app()` + `main()`（保持 `python web.py` 可直接启动） |
| 5.10 | `/oobe` 与 `/admin/oobe` 都渲染 `oobe.html` | 行为不变 |

**验证**
```bash
pytest -q tests/regression/test_route_contract.py   # 41 条路由集合必须完全一致
pytest -q tests/regression/test_api_contract.py
python -c "from app.web.factory import create_app; c=create_app(); print(len(list(c.url_map.iter_rules())))"
```

**回滚**：`git revert`；`web.py` shim 保证 systemd `ExecStart=.../web.py` 不变

**产出**：`app/web/` + `app/services/`；`web.py` → shim；模板改用标准 Jinja 加载

**风险**
- 蓝图前缀冲突 → 由路由集合快照测试捕获
- `_admin_required`（legacy OOBE 用）与 `require_auth`（R63 用）两套装饰器并存 → 本 Phase **只搬家不改语义**，语义统一留到后续版本
- Jinja 转义差异 → `render_template_string` 与 `render_template` 规则相同，但仍需逐页比对 HTML 输出

---

### Phase 6 — 前端令牌统一 + CDN 本地化 + 配置收口（1.5 天）

**目标**：收敛设计系统；去掉公网 CDN 依赖；统一配置读取。

**前置**：Phase 5

**操作步骤**

| 步 | 动作 | 说明 |
|---|---|---|
| 6.1 | 建 `static/css/tokens.css` | 合并 4 份 `:root`：统一命名（`--bg-page` `--bg-card` `--text-primary` `--text-secondary` `--accent` `--border` `--radius-*` `--space-*`）；**取 dashboard 的 `#0a0a0b` 系作为基准**，把 oobe/admin/login 的 `#0f172a` 映射过去 |
| 6.2 | 4 个 CSS 改 import tokens | `@import url("tokens.css");`，删除各自 `:root` |
| 6.3 | Chart.js 本地化 | 下载 `chart.js@4.4.1` UMD → `static/vendor/chart.umd.min.js`（保留 LICENSE 说明）；`templates/dashboard.html:10` 改 `{{ url_for('static', filename='vendor/chart.umd.min.js') }}` |
| 6.4 | 建 `app/config.py` | 统一 Settings（dataclass 或 `pydantic-settings`），字段名映射现有 `.env` 键；`meta` 覆盖通过 `settings_service` 注入（消除 `config.py` 反向 import `db`） |
| 6.5 | 旧 `config.py` 变 shim | 保留全部模块级常量名与 5 个 getter 名（`get_feishu_webhook` 等），内部转调 `app.config` |
| 6.6 | 前端视觉回归 | 逐页截图比对：`/` `/oobe` `/admin/*` `/login`（4 断点 × 5 页面） |

**验证**
```bash
pytest -q tests/regression/     # R67 CSS 守卫（断点 / 选择器 / 44px / prefers-reduced-motion）
grep -rn "cdn.jsdelivr" templates/ static/    # 必须为空
```

**回滚**：`git revert`；`tokens.css` 可单独 revert 而不影响其他 Phase

**产出**：`static/css/tokens.css`、`static/vendor/`、`app/config.py`

**风险**：色板统一会产生视觉变化 → 需你确认"统一到哪一套色板"（见 §8 待决事项）

---

### Phase 7 — 文档与发布（0.5 天）

**目标**：补齐 AGENTS.md 占位的 4 篇文档，发布 `v1.2.0-r69`。

**前置**：Phase 6

**操作步骤**

| 步 | 动作 |
|---|---|
| 7.1 | 写 `docs/ARCHITECTURE.md`（分层图、模块职责、依赖矩阵 —— 可从本文档 §2 派生） |
| 7.2 | 写 `docs/DEPLOY.md`（systemd / nginx / cron / logrotate / 反向代理 SSL / 升级与回滚） |
| 7.3 | 写 `docs/SECURITY.md`（凭据管理、`.env` 权限、`.gitignore` 保护、openid 轮换、**TODO #23 的 6 个已泄露 secrets 轮换清单**） |
| 7.4 | 写 `docs/OPERATIONS.md`（故障排查 SOP、日志位置、备份恢复） |
| 7.5 | 更新 `README.md` / `AGENTS.md` 的模块表与目录树 |
| 7.6 | 重写 `docs/TECHNICAL.md`（对齐 R69 现状） |
| 7.7 | 出 `scripts/deploy/r69_deploy.sh`（沿用 `deploy_round68.sh` 模式：备份 → 校验 sha256 → unzip -o → 恢复 .env → 重启 → healthz 验证） |
| 7.8 | 打 tag `v1.2.0-r69` + 生成 release notes + zip + sha256 |
| 7.9 | 部署到生产 + 走 §5 验收清单 |

---

## 5. 测试策略

### 5.1 测试金字塔（目标）

| 层 | 目录 | 数量目标 | 内容 |
|---|---|---|---|
| 契约快照 | `tests/regression/` | 5 类 × N | §3 的 7 类契约，**每次提交必跑** |
| 单元 | `tests/unit/` | ≥ 60 | `app/domain/*` 纯函数（100% 行覆盖）、`app/db/coerce`、`app/notify/crypto`、`app/notify/card_builder` |
| 集成 | `tests/integration/` | ≥ 25 | Flask test client 打全部 41 路由、`run_once` 编排、OOBE 全流程 |
| 归档守卫 | `tests/legacy/` | 41（现状） | 保留不删，但**不作为 CI 门禁** |

### 5.2 新增 fixture（`tests/conftest.py`）

```python
@pytest.fixture
def tmp_db(monkeypatch):        # 内存 SQLite，隔离每个用例
@pytest.fixture
def flask_client(create_app):   # app.test_client()，TESTING=True
@pytest.fixture
def fixed_now(monkeypatch):     # 冻结 datetime.now() 到 2026-10-06 12:00:00 CST
@pytest.fixture
def sample_rows():              # 24 条 records 快照
@pytest.fixture
def recorded_portal():          # 学校门户 JSON/HTML 录制回放
```

### 5.3 覆盖率门禁

| 范围 | 目标 |
|---|---|
| `app/domain/` | **100%** 行覆盖（强制） |
| `app/db/` | ≥ 85% |
| `app/notify/crypto.py` | **100%**（安全敏感） |
| 全仓 | ≥ 70%（渐进，Phase 5 前不强制） |

### 5.4 CI 门禁（Phase 0 后）

```yaml
- ruff check .
- pytest -q --cov=app --cov-report=term-missing
- python -m py_compile config.py db.py web.py feishu_bot.py dorm_power.py auth.py
- ast.parse 全量守卫（保留现有）
- secret scan（保留现有）
- 契约快照 diff（必须为 0）
```

---

## 6. 风险登记册

| ID | 风险 | 概率 | 影响 | 缓解 | 负责 Phase |
|---|---|---|---|---|---|
| RK1 | **未提交成果丢失** | 中 | 🔴 极高 | Phase 0.1 先备份 + 0.3 立即落库 | P0 |
| RK2 | 重构改坏路由/JSON 契约 | 中 | 🔴 高 | 契约快照测试作为 CI 门禁 | P0 |
| RK3 | `run_once` 副作用漏搬 | 中 | 🟠 中 | 集成守卫 + `--fetch-only` 冒烟 | P4 |
| RK4 | CSS 令牌统一导致视觉回归 | 高 | 🟡 低 | 4 断点 × 5 页面截图比对 | P6 |
| RK5 | 蓝图前缀冲突 / 端点名冲突 | 低 | 🟠 中 | 路由集合快照测试 | P5 |
| RK6 | 两套认证装饰器语义混淆 | 中 | 🟠 中 | 本 Phase 只搬家不改语义 | P5 |
| RK7 | `db/__init__` re-export 漏项 | 中 | 🟠 中 | `test_round61.py` 符号守卫 | P2 |
| RK8 | 生产部署窗口风险 | 中 | 🟠 中 | `r69_deploy.sh` 自动备份 + healthz 校验 + 回滚手册 | P7 |
| RK9 | 工期超预期 | 高 | 🟡 低 | 每个 Phase 独立可交付，可随时停在任一 Phase | 全程 |
| RK10 | 6 个已泄露 secrets 未轮换（TODO #23） | — | 🔴 高 | 与重构解耦，建议并行处理（Phase 7.3 出清单） | P7 |

---

## 7. 验收标准（DoD）

### 7.1 每个 Phase 的 DoD

- [ ] `pytest -q` 全绿
- [ ] `ruff check .` 0 error
- [ ] 契约快照测试 diff = 0
- [ ] `python -m py_compile` 6 个入口文件通过
- [ ] `git status --short` 干净（无临时文件）
- [ ] 打 tag `r69-pN`
- [ ] 更新 `docs/reports/r69_pN_REPORT.md`（记录改动文件清单 + 验证输出）

### 7.2 全量重构完成 DoD

- [ ] 41 个路由的 `{url_rule, methods}` 集合与 §3.1 完全一致
- [ ] `/api/data` / `/api/live` 响应键集合与 §3.2 完全一致
- [ ] 10 张表字段名与 §3.3 完全一致
- [ ] `.env` 键与 `meta` 键与 §3.6 完全一致
- [ ] `web.py` ≤ 10 行、`dorm_power.py` ≤ 10 行、`feishu_bot.py` ≤ 10 行（均为 shim）
- [ ] 单文件最大行数 ≤ 500（`app/**`）
- [ ] 无 `from X import _private` 跨模块调用
- [ ] `app/domain/` 行覆盖率 100%
- [ ] 无公网 CDN 依赖（`grep -r "cdn.jsdelivr\|unpkg" templates/ static/` 为空）
- [ ] `deploy/dorm-cron.txt` 与 `deploy/dorm-web.service` **零改动**
- [ ] 生产部署后 `/healthz` 返回 200
- [ ] 飞书群收到一次常规卡片 + 一次 `/状态` 命令回复正常

### 7.3 生产验证清单（部署后人工）

- [ ] `/healthz` 200 且 `db` / `school` / `feishu` 三段均 ok
- [ ] `/` 渲染正常（hero / 图表 / 4 stat card / 电表面板）
- [ ] `/login` → `/admin` 5 个 tab 均可打开
- [ ] `/oobe` 在 `oobe_completed=1` 时不可进入（保持现状行为）
- [ ] 飞书群：手动 `dorm_power.py --fetch-only` 不推送；正常 cron 推送一次
- [ ] 飞书私聊：`/状态` `/电表` `/今日` `/历史` `/缴费` `/违规` `/帮助` 全部正常
- [ ] cron 两条 entry 均执行成功（`journalctl` / 日志无 ERROR）
- [ ] 回滚演练：`r69_deploy.sh` 的备份目录可完整恢复

---

## 8. 待决事项（需你确认）

| ID | 事项 | 选项 | 影响 Phase |
|---|---|---|---|
| D1 | 色板统一基准 | (a) 用 dashboard 的 `#0a0a0b` 深色系（推荐）<br>(b) 用 oobe/admin 的 `#0f172a` 系<br>(c) 保留两套，只统一变量名 | P6 |
| D2 | 是否保留 `*Repo` 类（Pydantic 风格） | (a) 保留为补充接口<br>(b) 删除，只保留函数式 API | P2 |
| D3 | 旧 round 测试（`tests/legacy/` 23 个） | (a) 保留归档<br>(b) 删除 | P0 |
| D4 | `tests/modern/test_round35-50` 是否重命名为语义名 | (a) 保留 round 命名<br>(b) 重命名为 `test_route_contract.py` 等语义名 | P0 |
| D5 | 根目录 shim 保留时长 | (a) 永久保留（零风险）<br>(b) 下个大版本删除 | P5 |
| D6 | 是否引入 `pydantic-settings` | (a) 引入（配置校验更强）<br>(b) 只用 stdlib dataclass（零新依赖） | P6 |
| D7 | Chart.js 版本 | (a) 固定 4.4.1（与现状一致）<br>(b) 升级到最新 4.x | P6 |
| D8 | 是否引入 `import-linter` 强制 §2.4 依赖矩阵 | (a) 引入（CI 门禁）<br>(b) 只用自定义 AST 守卫（零新依赖） | P0 |

---

## 9. 附录

### 9.1 相关文档

| 文档 | 说明 |
|---|---|
| `AGENTS.md` | 项目硬约束（SSH 禁令、schema 冻结、密钥管理） |
| `docs/TECHNICAL.md` | 技术细节（**已过时，描述 R42 状态**） |
| `R68_RELEASE_NOTES.md` | R60-R68 变更日志 |
| `README.md` | 用户向文档 |
| 本文档 | R69 重构设计 |

### 9.2 基线快照命令（Phase 0.1 使用）

```bash
git rev-parse HEAD > docs/reports/r69_baseline.txt
git status --short >> docs/reports/r69_baseline.txt
git ls-files | wc -l >> docs/reports/r69_baseline.txt
wc -l *.py db/*.py >> docs/reports/r69_baseline.txt
find . -name "*.py" -not -path "./.venv/*" | wc -l >> docs/reports/r69_baseline.txt
```

### 9.3 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | draft-1 | 初稿：现状分析 + 目标架构 + 7 类契约 + 8 个 Phase + 待决事项 |

---

**文档结束** — 本设计不含任何代码改动，待评审通过后按 §4 逐 Phase 执行。







