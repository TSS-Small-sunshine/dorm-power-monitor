# 需求确认书（REQUIREMENTS）

> **状态**：✅ **已确认（v1.3）** —— **22 项决策**全部锁定，仅 Q8 待你审阅预分类
> **基线**：`develop` @ `bb031f6` + 工作区未提交的 R61–R67 成果
> **确认日期**：2026-10-06
> **配套文档**：
> - `FEATURE_INVENTORY.md` —— 105 项功能现状盘点
> - `SCOPE_DECISION.md` —— 105 项预分类 + 20 项新增（待审阅）
> - `REWRITE_PLAN.md` —— 重写方案 v4
> - `PLAN_AUDIT.md` —— 方案审查报告（5 阻断 / 5 严重 / 7 中等）

---

## 零、决策总览

| Q | 事项 | 决定 | 状态 |
|---|---|---|---|
| Q1 | 驱动力 | 代码/架构难维护 → **重写** | ✅ |
| Q2 | 用户与规模 | **多实例分发**（每个宿舍自建一套） | ✅ |
| Q3 | 部署方式 | **Docker + 裸机脚本**，覆盖 amd64/arm64/armv7 | ✅ |
| Q4 | 依赖预算 | **全保留**，靠 Docker 预编译解决兼容 | ✅ |
| Q5 | 痛点 | **综合**（结构 + 前端 + 测试）→ 全面重做 | ✅ |
| Q6 | 前端 | **全新前端** | ✅ |
| Q7 | 技术栈 | **Flask + SQLite + Pydantic + Vue3/Vite/TS + Tailwind + Chart.js + Docker** | ✅ |
| Q8 | 功能范围 | 预分类已出（**50 必留 / 52 改 / 3 删 + 12 新增**） | 🟡 待审阅 |
| Q9 | Schema/迁移 | **保持 10 表不变** + sessions 存哈希 + users 加 disabled → **零迁移** | ✅ |
| Q10 | 时区 | **方案 B** 显式锁定 Asia/Shanghai → **零迁移** | ✅ |
| Q11 | 访问控制 | **默认需登录** + `public_readonly` 开关 | ✅ |
| Q12 | 工程约束 | **公开 + 中文优先**（补 CONTRIBUTING / 截图 / 部署文档） | ✅ |
| Q13 | 交付节奏 | **并行开发**，随时秒级回滚 | ✅ |
| Q14 | 调度 | **单容器 + APScheduler**（用户不用配 cron） | ✅ |
| **Q15** | **配置中心化** | **所有配置项都能在 WebUI 改**（配置注册表 + 存储迁 DB） | ✅ 🔴 |
| **Q16** | **功能开关** | **飞书机器人做成开关** + 通用开关体系（含命令级） | ✅ |
| **Q17** | **质量属性** | **安全 / 稳定 / 鲁棒** 都要兼顾（写成可验证措施） | ✅ |
| **Q18** | **解耦扩展** | 定义扩展点协议 + `docs/EXTENDING.md` | ✅ |
| **Q19** | **随机密码** | 安装时随机生成 + 强制首登改密 | ✅ |
| **Q20** | **日志分级** | **7 级 + 类别标签 + 模块级覆盖 + JSON 可选** | ✅ |
| **Q21** | **认证页面自主设计** | **禁止浏览器原生 Basic Auth 弹窗**；6 个认证界面统一设计 | ✅ 🎨 |
| **Q22** | **分支与文件策略** | **全新分支 `rewrite/r69` + 干净文件树**（先落库 → 生成快照 → 再清空 legacy） | ✅ 🌿 |

### 三项"红利"

| 决定组合 | 红利 |
|---|---|
| Q9 + Q10 | **零数据迁移** —— 现有 `records.db` 直接可用，历史数据零风险 |
| Q9 + Q10 + Q13 | **新旧代码读写同一 DB** —— 并行开发 + 秒级回滚成为可能 |
| Q14 | **用户不再需要配 cron** —— 部署从 ~15 步降到 1–2 步 |

---

## 一、已确认的决策

### Q1 驱动力 ✅

**答案**：代码/架构难维护 —— 技术栈想换

**含义**：进入**重写（Rewrite）**路径，不是重构。契约可重新设计。

---

### Q2 用户与规模 ✅

**答案**：我自己用 + 舍友用这个网页 + **隔壁宿舍的也想用（查自己的宿舍）**
**补充确认**：**每个宿舍都是自己搭建的**（多实例分发，非多租户）

**架构含义 —— 关键结论**：

| 项 | 判断 |
|---|---|
| 形态 | **多实例分发（multi-instance）**，不是多租户 |
| 数据模型 | ✅ **不用加 room 维度**（每实例单宿舍） |
| `meta` 33 个全局键 | ✅ **不用改成 per-room** |
| 抓取器 | ✅ **不用改成循环抓 N 个房间** |
| 权限模型 | ✅ **不需要 user↔room 多对多** |
| 真实新增需求 | 部署门槛 / 自助首次配置 / 多用户（舍友）/ 可交付文档 |

> **判定**：Q2 顺带回答了"功能是否等价" —— **不等价**。但新增的是**交付体验**类需求，不是**数据架构**类需求。这是好消息。

---

### Q3 部署方式 ✅

**答案**：**Docker 和裸机脚本都要**；**要保证 Linux 所有架构都能用**

**含义**：
- Docker Compose 作为主路径（解决架构兼容）
- 裸机 bash 脚本作为兜底路径
- 目标架构需收敛为：`amd64` + `arm64` + `armv7l`（riscv64 尽力而为）

---

### Q4 依赖预算 ✅（**2026-10-06 修订**）

**原答案**：都不砍 —— 靠 Docker 镜像预编译解决兼容性；裸机脚本只支持 `amd64` / `arm64`

**修订后答案**（因 B1 修复）：

> **功能性依赖全保留；剔除未使用的 `pydantic`；`bcrypt` → Python 标准库 `hashlib.scrypt`**

**修订原因**：原答案在 armv7 上**不可行**（`pydantic-core` / `bcrypt` 无 armv7 wheel，`python:3.12-slim` 无 Rust 工具链 → 必然编译失败）。详见 `BLOCKER_FIXES.md §1`。

**修订后的依赖清单（6 个，零 Rust）**：

| 依赖 | 类型 | 用途 | armv7 |
|---|---|---|---|
| `flask` | 纯 Python | Web 框架 | ✅ |
| `requests` | 纯 Python | HTTP | ✅ |
| `python-dotenv` | 纯 Python | `.env` 加载 | ✅ |
| `gunicorn` | 纯 Python | WSGI 服务器（**新增用途**） | ✅ |
| `APScheduler` | 纯 Python | 进程内调度（**新增**） | ✅ |
| `pycryptodome` | C | 飞书 AES | ✅ 需 `gcc` |
| `pillow` | C | PNG 卡片渲染 | ✅ 需 `libjpeg-dev zlib1g-dev` |
| ~~`pydantic`~~ | ~~Rust~~ | ~~数据模型~~ | ✅ **已剔除**（未使用） |
| ~~`bcrypt`~~ | ~~Rust~~ | ~~密码散列~~ | ✅ **已替换**为 stdlib `scrypt` |

**关键收益**：
- ✅ **armv7 从"必然失败"变为"可行"**（只剩普通 C 扩展）
- ✅ 依赖树从 8 个减到 **6 个**（含 2 个可编译的 C 扩展）
- ✅ **Q3 的"全架构"承诺得以兑现**

**唯一代价**：密码哈希格式变更 → 升级时管理员用新随机密码登录一次（与 Q19 一致；新部署零影响）

**含义**：
- Docker 镜像仍**预编译**并发布到 registry（用户 `docker pull`，不本地编译）
- 裸机脚本支持 **amd64 / arm64**；armv7 走 Docker（或自行编译 2 个 C 扩展）

---

### Q5 痛点定位 ✅

**答案**：**综合** —— 结构、前端、测试都不满意，希望**全面重做**

**含义**：范围锁定为「**全面重写**」（结构 + 前端 + 测试），不是局部重构。

---

### Q6 前端不满意的点 ✅

**答案**：**都有** —— 要一个全新的前端（不追问具体原因）

**含义**：前端整体重做，采用组件化框架，视觉重新设计。

---

### Q7 技术栈选型 ✅

**答案**：**整套同意**

| 层 | 最终选择 |
|---|---|
| 后端框架 | **Flask 3 + 蓝图 + 服务层** |
| 数据库 | **SQLite（WAL）** |
| 数据访问 | **薄 SQL 层 + Repository**（不用 ORM） |
| 数据模型 | **Pydantic v2**（且要修掉"三份真相"，成为唯一模型层） |
| 前端 | **Vue 3 + Vite + TypeScript（纯静态 SPA）** |
| 前端样式 | **Tailwind CSS** |
| 图表 | **Chart.js（本地打包，去 CDN）** |
| 打包 | **Docker（buildx 多架构）+ 裸机脚本** |
| 测试 | **pytest + 契约快照** |

**附带决定**：
- ✅ 字体目录去重（`assets/fonts/` 与 `static/fonts/` 合并为一份，省 17.3MB）
- ✅ 后端转为纯 API 服务，页面路由交给前端 SPA（URL 会变）
- ⏳ 数据迁移策略待定（见 Q9）

---

## 二、待确认项

| ID | 事项 | 状态 |
|---|---|---|
| Q8 | 功能范围决策（`SCOPE_DECISION.md` 的 105 项预分类已出，待你审阅） | 🟡 已产出待审 |
| Q9 | 数据/Schema 迁移策略 | ⏳ **当前** |
| Q10 | 时区存储策略 | ✅ **方案 B** |
| Q11 | 多用户方案（舍友怎么拿账号） | ⏳ |
| Q12 | 工程约束（是否开源 / CI 要求） | ⏳ |
| Q13 | 交付节奏（停服重写 vs 并行） | ⏳ |
| Q14 | 调度方式（host cron vs 容器内 APScheduler） | ⏳ |

---

### Q10 时区策略 ✅

**答案**：**方案 B —— 显式锁定 `Asia/Shanghai`（零迁移）**

**实现方式**：

```python
from zoneinfo import ZoneInfo          # Python 3.9+ 标准库

CST = ZoneInfo("Asia/Shanghai")

def now_cst() -> datetime:
    """返回朴素的北京时间，与系统时区无关。"""
    return datetime.now(CST).replace(tzinfo=None)
```

**要求**：
- 全项目**禁止**直接调用 `datetime.now()` / `datetime.today()` / `date.today()`
- 统一走 `starwatt.timeutil.now_cst()`（新增模块）
- 用 ruff 自定义规则或 AST 守卫在 CI 中强制检查
- Docker 镜像中同时设置 `TZ=Asia/Shanghai` 作为双保险

**收益**：
| 项 | 结果 |
|---|---|
| 数据迁移 | ✅ **零迁移**（存储格式 `YYYY-MM-DD HH:MM:SS` 不变） |
| 现有 `records.db` | ✅ 直接可用 |
| 33 个 meta 键 | ✅ 语义不变 |
| 所有去重/节流/比较逻辑 | ✅ 继续有效 |
| 服务器时区 | ✅ 任意时区结果一致 |

**风险**：无（唯一要求是"禁止裸用 `datetime.now()`"，用 CI 守卫保证）

---

### Q9 Schema 与迁移策略 ✅

**答案**：**保持 10 张表不变 + 两处增量改动（零迁移）**

| 改动 | 方式 | 代价 | 收益 |
|---|---|---|---|
| `sessions` 改存 `sha256(token)` | 改代码（列名不变） | 现有 session 失效 → 用户重登一次 | 🔒 消除明文 token 风险（L17） |
| `users` **新增** `disabled` 列 | `ALTER TABLE ADD COLUMN` | 无 | 支持禁用用户（L18） |
| `meta` 33 个键名 | **保持不变** | 无 | 零迁移 |
| 新增表 | 允许 | 无 | 为将来留余地 |

**结果**：
- ✅ **零数据迁移**，现有 `records.db` 直接可用
- ✅ 历史电量数据零风险
- ✅ 表名 / 字段名全部保持（`records` / `daily_elec` / `violations` / `pay_history` / `run_status` / `meta` / `users` / `sessions` / `failed_attempts` / `audit_log`）

**顺带修正的文档错误**（原 `REFACTOR_DESIGN.md` 契约表）：
- 表名是 `failed_attempts`，**不是** `login_attempts`
- `sessions` 主键是 `id`（`token` 仅 UNIQUE），**不是** `token_hash`
- `users` **没有** `disabled` 字段（R68 文档声称有）
- `sessions.token` 存**明文**（R68 文档声称存 SHA-256）
- session 是**滑动过期**（可无限续期，文档未提）

---

### Q11 访问控制模型 ✅

**答案**：**默认需登录 + 提供 `public_readonly` 开关**；舍友由管理员建号

**背景（新发现的问题）**：现状 `GET /`、`GET /api/data`、`GET /api/live` **完全无认证**
（`web.py:671/708/730` 三个路由无装饰器）—— 任何知道 URL 的人都能看到全部电量数据。
这在"个人自用"时合理，在"每宿舍自建 + 舍友使用"时成为问题。

**方案**：

| 项 | 设计 |
|---|---|
| 默认 | `/`、`/api/live`、`/api/data` 全部要求登录（复用现有 session） |
| 开关 | `meta.public_readonly = "1"` 时回到公开行为（适合"就我自己用"的部署） |
| 账号来源 | 管理员建号（一个宿舍 4–6 人，建号成本极低） |
| 角色 | `admin`（可改配置）/ `viewer`（只读）；配合 Q9 新增的 `disabled` 列 |
| 审计 | 复用现有 `audit_log`，补记"数据访问"事件（可选） |

**收益**：舍友可看自己宿舍数据 ✅ / 可一键公开 ✅ / 审计完整 ✅ / 开发成本极低 ✅

---

### Q14 调度方式 ✅

**答案**：**方案 ① —— 单容器 + APScheduler 进程内调度**

**设计**：

```python
# starwatt/scheduler.py
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler(timezone="Asia/Shanghai")
scheduler.add_job(scrape_job,  "cron", minute="*/10",
                  max_instances=1, coalesce=True, misfire_grace_time=300)
scheduler.add_job(l3_gate_job, "cron", minute="*",
                  max_instances=1, coalesce=True, misfire_grace_time=120)
scheduler.start()
```

**约束**：gunicorn 只跑 **1 个 worker**（启动时断言；多 worker 会重复调度）

**收益**：

| 项 | 结果 |
|---|---|
| Docker / 裸机两条路径 | ✅ **统一**（不再需要用户配 cron） |
| 进程数 | ✅ 1 个（内存最省，512MB 友好） |
| `flock` 重入防护 | ✅ 由 `max_instances=1` 替代 |
| 容器停机错过的抓取 | ✅ 由 `coalesce=True` + `misfire_grace_time` 处理 |
| 现有 `run_once()` / `_l3_due()` | ✅ **零改动**，原样调用 |
| armv7 兼容 | ✅ 纯 Python，无 cron 依赖 |

**已知代价**：

| 代价 | 严重度 | 缓解 |
|---|---|---|
| web 重启打断正在跑的抓取（~10s） | 🟢 低 | 抓取幂等（`INSERT OR REPLACE`），下次 tick 补上 |
| 必须锁定 1 个 worker | 🟢 低 | 写进配置 + 启动断言 |
| 抓取线程异常影响 Flask | 🟡 中 | 现有代码已全 `try/except`（K7）+ 加线程级兜底 |

**连带影响**：
- ❌ **J1 双 cron + flock 设计废弃**
- ❌ **`deploy/dorm-cron.txt` 废弃**
- ⚠️ 新增依赖 `APScheduler`（纯 Python，~1MB）
- ⚠️ 新增 `starwatt/scheduler.py` 模块

---

### Q13 交付节奏 ✅

**答案**：**方案 B —— 并行开发，随时可回滚**

**为什么可行**（Q9 + Q10 换来的红利）：schema 不变 + 时间戳格式不变 + meta 键不变
→ **新旧代码可读写同一个 `records.db`**。

**开发期布局**：

```
/opt/dorm-power-monitor/          ← 旧服务继续跑（systemd + host cron，写 records.db）
/opt/dorm-power-monitor-r69/      ← 新版本开发（不动生产）
```

**切换 SOP（几分钟）**：

```bash
# 1) 停旧
sudo systemctl stop dorm-web
# 2) 起新（挂载同一份 records.db）
cd /opt/dorm-power-monitor-r69 && sudo docker compose up -d
# 3) 验证
curl -fsS http://127.0.0.1:5000/healthz && echo OK
# 4) 若失败 → 秒级回滚
sudo docker compose down && sudo systemctl start dorm-web
```

**约束**：
- ⚠️ **切换期间避免新旧同时写**（SQLite WAL 支持并发读，但双写会有锁竞争）→ 必须"停旧 → 起新"
- ✅ 数据零断档（旧服务在开发期持续采集）
- ✅ 回滚是"秒级"的（旧服务从未被卸载）

---

### Q12 工程约束 ✅

**答案**：**公开 + 中文优先**（补齐 CONTRIBUTING / 截图 / 部署文档，不做英文 README）

**现状核实**：
- 仓库 `TSS-Small-sunshine/dorm-power-monitor` **已是公开仓库**
- 许可证 **MIT**（`LICENSE:1`）

**待补**：

| 项 | 现状 | 动作 |
|---|---|---|
| 许可证 | ✅ MIT | 保持 |
| 部署文档 | ⚠️ 假设读者是原作者 | **必须重写**（N7） |
| `CONTRIBUTING.md` | ❌ 无 | 补 |
| 截图 / 演示 | ❌ 无（README 写"待补"） | 补 3 张 |
| Issue / PR 模板 | ❌ 无 | 可选 |
| 英文 README | ❌ 无 | **不做**（目标用户是中国高校学生） |
| CI | ✅ 有 | 增强（加 pytest + ruff + 多架构构建） |

---

### Q15 配置中心化 ✅ 🔴

**答案**：**所有配置项都必须能在 WebUI 中修改**

**现状差距**（这是个大缺口）：

| 配置层 | 现状 | WebUI 可改？ |
|---|---|---|
| `.env` 20 个键 | `DORM_OPENID` / `FEISHU_*` / `FLASK_*` / `AUTH_*` / `API_INTERNAL_TOKEN` … | ❌ 只能改文件后重启 |
| `meta` 表 33 个键 | 抓取配置 / 推送开关 / 时间 / 接收人 / 静默时段 | 🟡 部分可改（仅 scrape + push 两组） |
| **代码硬编码常量** | `_REMAIN_RED_BELOW=30` / `_F2_INTERVAL_SEC=86400` / `_STALE_SCRAPE_GAP_SEC=7200` / `_VIOLATION_ALERT_COOLDOWN_SEC=1800` / `_ONLINE_LABELS` / 重试次数 / 超时 / bcrypt rounds / 锁定 5次15分 / session 24h / 学期锚定 9/7 | ❌ **完全不可改** |

**设计要求**：

1. **配置注册表（Config Registry）** —— 声明式定义**每一个**配置项
2. **运行时存储迁到 DB** —— `meta` 表成为唯一运行时配置源
3. **`.env` 只保留启动必需项**（DB 打开前就要用的）
4. **UI 自动生成** —— 前端根据注册表元数据渲染表单（分组 / 类型 / 校验 / 帮助文本）
5. **导出 / 导入** —— JSON 配置备份（对"每宿舍自建"的迁移很关键）
6. **`requires_restart` 标记** —— 需要重启的项在 UI 上显式提示

**键名兼容**（尊重 Q9）：现有 33 个 meta 键名**保持不变**，新键沿用同样的 snake_case 风格。

---

### Q16 功能开关 ✅

**答案**：**飞书机器人做成开关**（并且推广为通用的功能开关体系）

**现状**：飞书有 6 个凭据（`FEISHU_WEBHOOK` / `FEISHU_SECRET` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` / `FEISHU_VERIFICATION_TOKEN` / `FEISHU_ENCRYPT_KEY`），但**没有任何总开关** —— 只能靠"清空凭据"来停用，且停了之后端点行为不确定。

**设计要求**：

| 开关 | 作用 |
|---|---|
| **群推送总开关** | 关闭后所有群卡片静默（不报错） |
| **私聊机器人总开关** | 关闭后 `/feishu/event` 返回 200 但不处理（飞书侧不报错） |
| **L1 低电告警** | 已有（`push_l1_enable`） |
| **L2 整点播报** | 已有（`push_l2_enable`） |
| **L3 日报 / 周报 / 月报** | 已有（3 个独立开关） |
| **L4 电表离线** | 🆕 新增 |
| **违规告警** | 🆕 新增 |
| **stale 数据陈旧** | 🆕 新增 |
| **命令级开关** | 🆕 9 个 slash 命令可逐个禁用 |

**关键约束**：**关闭 ≠ 报错**。所有被关闭的功能必须"静默降级"，且在日志中留 `NOTICE` 级记录（见 Q20）。

---

### Q17 质量属性 ✅

**答案**：**安全性 / 稳定性 / 鲁棒性都要兼顾**

**要求写成可验证的设计原则**，而非口号：

| 属性 | 可验证的措施 |
|---|---|
| **安全** | secrets 加密存储 + 脱敏返回 / session token 哈希 / CSRF / 登录锁定 / SSRF 4 层 / fail-closed / 输入校验 / 审计日志 / 容器非 root + 只读根 FS |
| **稳定** | 单点失败隔离 / 幂等写 / 重试+退避 / 健康检查 / 优雅关闭 / 定时备份 / 磁盘空间检查 / 单 worker 断言 |
| **鲁棒** | 边界处理（负数 delta / 空数据 / NULL）/ 降级路径（stale 兜底）/ 时区锁定 / 配置校验 / 崩溃自恢复 |

---

### Q18 解耦与可扩展 ✅

**答案**：**解耦架构，便于维护也便于加新功能**

**要求定义明确的"扩展点协议"**（加新功能时改几个文件、写几行，而不是改 5 处）：

| 扩展场景 | 预期成本 |
|---|---|
| 加一个配置项 | **1 行声明**（registry） |
| 加一个告警层 | ~20 行（policies + 开关） |
| 加一个通知渠道（微信/邮件/Telegram） | **新增 1 个文件**（实现 `Notifier` 协议） |
| 加一个数据源（适配其他学校） | **新增 1 个文件**（实现 `Endpoint` 协议） |
| 加一个飞书命令 | ~10 行 |
| 加一个前端页面 | 2 个文件（view + route） |

**产出**：`docs/EXTENDING.md`（扩展指南，含每类扩展的分步 SOP）

---

### Q19 安装时随机密码 ✅

**答案**：**默认密码在安装时随机生成**

**设计**：

1. `install.sh` / Docker entrypoint 用 `secrets.token_urlsafe(16)` 生成随机管理员密码
2. 密码**打印一次到安装输出**（用户必须复制）
3. 同时写入 `/opt/dorm-power-monitor/.initial-admin-password`（`chmod 600`，首次登录后自动删除）
4. **强制首次登录改密**：`users.must_change_password = 1` 时，除改密接口外所有接口返回 403
5. **移除** `AUTH_INITIAL_ADMIN_PASSWORD` 环境变量（弱密码来源）

**收益**：消除"默认密码 `admin/admin`"这类部署事故。

---

### Q20 日志分级 ✅

**答案**：**日志要全，严重等级细分**

**现状**：`logging.basicConfig(level=INFO)`，散落的 `logger.warning` / `logger.error`，无级别策略、无模块级控制、无结构化输出。

**设计要求**：

**① 自定义 7 级**（Python 标准只有 5 级，不够用）：

| 级别 | 值 | 用途 | 示例 |
|---|---|---|---|
| `TRACE` | 5 | HTTP 请求/响应细节、解析中间值 | 请求 URL 路径、响应字节数 |
| `DEBUG` | 10 | 函数入口出口、决策分支 | `_is_due(F2)=False` |
| `INFO` | 20 | 正常业务事件 | 抓取成功、推送成功、登录成功 |
| `NOTICE` | 25 | 需要留意但正常 | 降级到 stale、因静默时段跳过推送、功能已关闭 |
| `WARNING` | 30 | 可恢复异常 | 单次抓取失败、重试后成功 |
| `ERROR` | 40 | 功能失败（需人工关注） | 抓取全失败、推送失败、DB 写失败 |
| `CRITICAL` | 50 | 系统不可用 | DB 打不开、配置损坏、调度器崩溃 |

**② 业务类别标签**（便于过滤）：
`scrape` / `push` / `auth` / `config` / `scheduler` / `db` / `web` / `notify`

**③ 模块级级别覆盖**（WebUI 可配）：
```
log.level            = INFO        # 全局
log.overrides        = {"scrape": "DEBUG", "db": "WARNING"}
```

**④ 输出目标**（WebUI 可配）：
- 控制台（`docker logs` / `journalctl`）—— 始终开启
- 文件（按天轮转 + 保留 N 天）—— 可选
- 格式：文本（默认）/ **JSON**（可选，便于采集）

**⑤ 与审计日志分离**：
- **运行日志** —— 可轮转、可降级、面向排查
- **审计日志**（`audit_log` 表）—— 不可篡改、不轮转、面向合规

**⑥ 敏感信息铁律**（沿用现有 K2）：
`openid` / `token` / `secret` / `password` **任何级别都不得出现在日志中** —— 用 AST 守卫 + 单元测试双重保证。

---

### Q21 认证页面必须自主设计 ✅ 🎨

**答案**：**所有登录/认证相关页面都必须自己写，禁止使用浏览器原生的认证弹窗**

---

#### 🔴 实证核查：你抱怨的"原生的那个"确实还在

我追查了根因 —— **HTTP Basic Auth 从未被删除**，`R68_RELEASE_NOTES.md` 的声明是**错的**（这是本次发现的**第 5 处**文档/代码不符）。

**证据**：

```python
# web.py:1218-1255  ——  _admin_required 装饰器（仍在生效）
def _admin_required(fn):
    """Decorator — HTTP Basic Auth gate for /admin/* routes."""
    ...
    auth = request.authorization                     # ← 解析 Basic Auth 头
    if not auth or auth.username != ADMIN_USER or auth.password != stored:
        resp = Response(
            "Auth required",
            401,
            {"WWW-Authenticate": 'Basic realm="dorm-power-monitor"'},   # ← 触发浏览器原生弹窗
        )
```

而 `R68_RELEASE_NOTES.md` 声称：
> "Pre-R63 HTTP Basic Auth against `meta.admin_password` is **fully removed**"

→ **与代码不符**。`/admin/api/oobe/save` 路由至今仍挂 `@_admin_required`。

**并且代码注释自己承认了这一点**（`web.py:2398-2399`）：
> "after setting a password, redirect back to /admin so the browser session can authenticate normally.
> **The basic-auth prompt will fire** because we did NOT send a header here."

---

#### 现状盘点：6 处认证界面，4 套互不相干的样式

| # | 界面 | 位置 | 样式来源 | 问题 |
|---|---|---|---|---|
| **1** | **浏览器原生 Basic Auth 弹窗** | `web.py:1251` `WWW-Authenticate: Basic` | **浏览器内置** | ❌ **完全无法定制** —— 这就是"丑"的根源 |
| **2** | `/admin/set-password` 恢复页 | `web.py:2355-2383` | **内联 HTML + 内联 `<style>`** | ❌ 硬编码，与设计系统脱节 |
| **3** | 登录失败页（curl 路径） | `web.py:2496-2508` | **零 CSS** | ❌ 最丑 —— 白底黑字裸 HTML |
| **4** | `/login` 登录页 | `templates/login.html` + `static/css/login.css` | 独立 CSS 文件 | 🟡 有设计（玻璃拟态），但色板是 `#0f172a` 系，与 dashboard 的 `#0a0a0b` 系不一致 |
| **5** | OOBE 密码步骤 | `templates/oobe.html` + `static/css/oobe.css` | 第三套 CSS | 🟡 又一套色板 |
| **6** | **首登强制改密页**（Q19 新增） | 尚未存在 | — | ⚠️ 需要新建 |

**根因**：认证流程被 4 种不同的实现方式切碎（Basic Auth / 内联 HTML / 独立 CSS / 无 CSS），**没有统一的设计系统**。

---

#### 设计要求

**① 绝对禁止**

| 禁止项 | 原因 |
|---|---|
| `WWW-Authenticate: Basic` 响应头 | 会触发浏览器原生弹窗，无法定制 |
| `request.authorization` 解析 | 旧 Basic Auth 遗留 |
| 内联 `<style>` 硬编码认证页面 | 无法复用设计令牌 |
| 无样式的裸 HTML 错误页 | 体验断裂 |

**② 必须统一设计的页面清单（6 个）**

| # | 页面 | 说明 |
|---|---|---|
| 1 | **登录页** `/login` | 用户名 + 密码 + 可见性切换 + 错误内联提示 + 加载态 + 锁定倒计时 |
| 2 | **首登强制改密页** | Q19 新增：显示随机密码 + 强制设置新密码 + 强度计 |
| 3 | **会话过期/未授权** | 不跳裸页，SPA 内展示 + 引导回登录（保留原目标路径） |
| 4 | **OOBE 各步（含密码步骤）** | 与登录页共用组件与令牌 |
| 5 | **登录失败（无 JS / curl 降级）** | 复用同一套设计，不再是无样式裸页 |
| 6 | **密码重置**（若保留 F6；**建议删**） | 见审计分歧点 1 |

**③ 统一交互规范**

| 项 | 要求 |
|---|---|
| 密码可见性切换 | 👁 按钮，键盘可达 |
| 强度计 | 实时反馈（复用 OOBE 现有实现） |
| 错误提示 | **内联**（不弹原生 alert、不跳页） |
| 加载态 | 按钮 spinner + 禁用，防重复提交 |
| 锁定提示 | 倒计时（"还需等待 12:34"） |
| 自动聚焦 | 首屏聚焦用户名 |
| `autocomplete` | `username` / `current-password` / `new-password` 正确标注 |
| 键盘可达 | 全程 Tab 可达 + Enter 提交 |
| ARIA | `aria-live` 播报错误；`aria-invalid` 标记字段 |
| 响应式 | 4 断点（≤767 / 768–1023 / 1024–1439 / ≥1440） |
| 深色/浅色 | 跟随设计令牌，两套都要好看 |

**④ 设计基准**

- 全部使用 **Tailwind + 统一设计令牌**（Q15/Q17 的产物）
- 复用同一个 `AuthLayout` 组件，登录 / 改密 / OOBE 密码步骤**共用骨架**
- 视觉基调与 dashboard 一致（**解决 L10 的色板分裂**）

---

### Q22 分支与文件策略 ✅ 🌿

**答案**：**从头开始做，拉全新分支，保证该分支的文件干净**

**关键顺序约束（不可颠倒）**：

```
① 落库 R61–R67 的 24 项到 develop        ← 否则永久丢失
        ↓
② 创建 rewrite/r69（从 develop 切出）
        ↓
③ commit #1：加入契约快照生成器 + 产出 fixtures   ← 旧代码还在，才能运行
        ↓
④ commit #2：删除全部 legacy 文件（约 60+ 个）
        ↓
⑤ 从 M1 开始建设
```

**为什么顺序不能反**：契约快照的 fixtures 需要**旧代码真实运行**才能产出。先删旧代码 = 失去"行为参照物"。

**分支策略**：

| 项 | 决定 |
|---|---|
| 分支名 | `rewrite/r69` |
| 基点 | **从 `develop` 切出**（非 orphan —— 保留 git 历史，旧代码可 `git show develop:web.py` 查阅） |
| "干净"的实现 | 用"第二个 commit 清空 legacy"实现干净工作树 |
| 不保留 | `legacy/` 目录 —— 不把旧代码塞进新分支 |

**保留 18 项 / 删除约 60+ 项**：详见 `REWRITE_PLAN.md §0.1 ④⑤`

**连带影响**：

| 原方案 | Q22 后 |
|---|---|
| 根目录 `web.py` / `dorm_power.py` / `db.py` / `config.py` 保留为兼容 shim | ❌ **全部取消** —— `web.py` 是新内容（gunicorn 入口），其余 3 个**删除** |
| `deploy/dorm-cron.txt` 保留 | ❌ **删除**（Q14 改 APScheduler） |
| `deploy/dorm-power-monitor.logrotate` 保留 | ❌ **删除**（Docker 用 `json-file` 轮转） |
| `setup.sh` 保留 | ❌ **删除**（由 `install.sh` 替代） |
| `assets/fonts/` 与 `static/fonts/` 去重 | ✅ 保留 `static/fonts/`，删 `assets/fonts/` |

---

## 二·补 阻断级问题修复决策（2026-10-06 晚确认）

> 详见 `BLOCKER_FIXES.md`。以下 3 项决策已确认，并据此修订了 6 项原决策。

### D1 — B1 修复选项 ✅

**答案**：**选项 ① —— 剔除 Rust 依赖**

| 动作 | 内容 |
|---|---|
| 删除 `pydantic` | 生产零调用；`auth.py` 已有 fallback |
| `bcrypt` → `hashlib.scrypt` | Python 标准库，安全等价（内存硬，16MB） |
| 结果 | **零 Rust 依赖** → armv7 只需编译 2 个普通 C 扩展 → **可行** |

### D2 — 服务器与建号流程 ✅

| 项 | 决定 |
|---|---|
| **Web 服务器** | 改用 **gunicorn**（`workers=1` + `preload_app=False`） |
| **调度器启动** | **`post_fork` + master PID 守卫 + 幂等守卫 + 优雅停止**（三道防线） |
| **建号流程** | **bootstrap 随机密码**（安装时生成 → 首启用 → 用后即焚）**+ OOBE 删掉"建号"步**（6 步 → **5 步**） |

### D3 — OOBE 定位 ✅

**答案**：**OOBE = 配置中心的引导子集**

- OOBE **不拥有**任何配置逻辑
- 复用同一个 `config_registry` + 同一个 `PUT /api/admin/config`
- OOBE 端点从 **10 个收敛到 2 个**
- 同时消灭 **L6**（OOBE 双后端）

### 由此产生的 6 项决策修订

| 决策 | 原内容 | 修订后 | 原因 |
|---|---|---|---|
| **Q4** | 依赖全保留 | **功能性依赖全保留**；删 pydantic；bcrypt→scrypt | D1 |
| **Q9** | "零迁移" | **"零数据迁移"**（schema 有 2 处加列，由迁移框架自动完成） | M4 |
| **Q14** | 可"处理停机错过的抓取" | **不补跑**；缺口在下次 tick 后恢复 | M3 |
| **Q19** | 随机密码 + 强制改密 | 明确为 **bootstrap 密码 + OOBE 删建号步** | D2 |
| **Q15/Q21** | 各自设计 | **OOBE = 配置中心引导子集**（共用注册表 + 共用 API） | D3 |
| **Q22** | `web.py` 为新内容 | 明确为 **gunicorn 入口 `web:app`** | D2 |

### 工期修订

| 阶段 | 原估 | 修复后 |
|---|---|---|
| M0 | 2d | **4d** |
| M1 | 5d | **6d** |
| M2 | 3d | 3d |
| M3 | 3d | 3d |
| M4 | 5d | 5d |
| M5 | 7d | **10d** |
| M6 | 4d | **6d** |
| M7 | 1d | 1d |
| **合计** | 30d | **38d** |

**对外口径：38 天（乐观）/ 45 天（现实）/ 55 天（含返工）**

---

### Q23 版本号与命名体系 ✅

**答案**：引入**语义化版本号** + 「**星 × 电**」主题的**中英双语版本代号**（**不用星座**）

**产品名（用户定）**：**StarWatt / 星瓦**
> star（宇宙）× watt（电功率）—— 同时命中「太空感」与「电量监控」

**关键设计**：

| 层 | 命名 |
|---|---|
| **产品名** | **StarWatt / 星瓦** |
| **Python 包** | `starwatt`（替代原方案的 `starwatt/`） |
| **CLI 命令** | `starwatt` |
| **Docker 镜像** | `ghcr.io/<owner>/starwatt` |
| **systemd 服务** | `starwatt.service` |
| 仓库名 | `dorm-power-monitor`（⚠️ 是否改待定，见 N1） |
| 版本号 | `MAJOR.MINOR.PATCH`（SemVer） |
| 版本代号 | 「中文 / English」 |
| Git tag | `v2.0.0-starcore`（ASCII 安全） |

**R69 重写 = `v2.0.0`「星核 / Star Core」**
> 因为它是**破坏性重写**（Q22 删兼容 shim、Q14 换调度、Q21 删 Basic Auth、B1 换密码哈希、B2 换启动方式）→ 按 SemVer 必须进 MAJOR

**代号序列（星 × 电 主题，非星座）—— 叙事线：一粒尘埃如何照亮一间宿舍**

| 版本 | 代号 | 意象 | 轮次 |
|---|---|---|---|
| `1.0.0` | **初尘** / First Dust | 星际尘埃 · 一切之始 | R51c |
| `1.1.0` | **微光** / Glimmer | 尘埃初亮 | R60–R68 |
| **`2.0.0`** | **星核** / **Star Core** | **恒星核心聚变点火 —— 重写重燃** | **R69** ← 当前 |
| `2.1.0` | 星轨 / Star Trail | 稳定绕行 | 预留 |
| `2.2.0` | 星火 / Spark | 电火花 / 星火 | 预留 |
| `2.3.0` | 尘光 / Dustlight | 尘与光交融 | 预留 |
| `2.4.0` | 电弧 / Arc | 电弧 / 光弧 | 预留 |
| `2.5.0` | 流光 / Streamer | 光的流动 | 预留 |
| `2.6.0` | 凝辉 / Coalesce | 汇聚成辉 | 预留 |
| `3.0.0` | 星河 / Star River | 汇流成河 | 预留 |
| `3.1.0` | 长明 / Everglow | 长明不灭 | 预留 |
| `4.0.0` | 破晓 / Daybreak | 新纪元 | 预留 |

**主题合理性**：恒星本质上是宇宙的**聚变反应堆** —— 光与电同源。
「星 + 瓦」正好把「宇宙能量」与「电功率」缝进一个词。

**明确禁止**：星座名（猎户/天琴/Orion/Lyra）· 具体星名（织女/Vega）· 天体编号 · 神话人名 · 行星名

**12 处落点**：`pyproject.toml` / `starwatt/__init__.py` / `/healthz` / 前端页脚 / SPA title / `CHANGELOG.md` / git tag / Release 标题 / README 头部 / 镜像 tag / systemd 服务 / CLI `--version`

**连带影响**：Python 包名 `starwatt/` → `starwatt/`（约 40 处文档引用需同步；现在改便宜，M1 后改贵）

**详见**：`NAMING.md`

---

## 三、🔴 已解决：时区地雷（原 C5）

见上方 Q10。方案 B 在**零数据迁移**的前提下消除了对服务器时区的隐式依赖。

---

## 四、约束冲突（更新）

| # | 冲突 | 状态 |
|---|---|---|
| C1 | "所有架构" vs "依赖全保留" | ✅ 已解（Docker 预编译） |
| C2 | "降低部署门槛" vs "镜像体积" | ⚠️ 待解（建议加离线镜像包 N5） |
| C3 | "每宿舍自建" vs "34.6MB 字体" | ✅ 已解（去重，省 17.3MB） |
| C4 | "512MB VPS" vs 技术栈升级 | ✅ 已解（保持 Flask + SQLite） |
| C5 | "每宿舍自建" vs "隐式 CST 时区依赖" | ✅ **已解**（Q10 方案 B） |

---

## 五、变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | draft-1 | Q1–Q4 确认；记录 4 项约束冲突 |
| 2026-10-06 | draft-2 | Q5–Q7 确认；技术栈锁定；新增时区风险（C5）+ 5 项待确认 |
| 2026-10-06 | draft-3 | Q10 确认（方案 B 零迁移）；C5 关闭；待确认项扩展为 Q8–Q14 |

---

## 三、🔴 新发现的部署地雷：时区

**问题**：全栈时间戳是「朴素本地时间」（`datetime.now()`，无时区）。当前生产服务器时区是 CST，所以能跑。**但代码里没有任何地方显式设置时区**：

| 检查项 | 结果 |
|---|---|
| `deploy/dorm-web.service` 是否设 `Environment=TZ=` | ❌ **没有** |
| `deploy/dorm-cron.txt` 是否设 TZ | ❌ **没有** |
| 代码里是否用 `zoneinfo` / `pytz` | ❌ **没有** |
| 依赖 `datetime.now()`（跟随系统时区） | ✅ 到处都在用 |

**对"每宿舍自己搭建"的后果**：

如果隔壁宿舍同学的 VPS 时区是 **UTC**（阿里云/腾讯云默认经常是 UTC），那么：

| 功能 | 后果 |
|---|---|
| L3 日报（BJ 09:00） | 会在**北京时间 17:00** 推送 |
| L2 整点播报 | 会在 UTC 整点推 = 北京 :00 也对得上（巧合），但卡片上的时间戳全错 8 小时 |
| 前端显示时间 | JS 补 `+08:00` 解析 → **全站时间错 8 小时** |
| 30 分钟违规冷却 / 2 小时 stale 判定 | 用相对差计算，**不受影响** |
| 首页「离线判定」（2h 窗口） | 用相对差计算，**不受影响** |

**这是"多实例分发"路线下的必解问题** —— 不能依赖"对方服务器时区刚好是 CST"。

---

## 四、约束冲突（更新）

| # | 冲突 | 状态 |
|---|---|---|
| C1 | "所有架构" vs "依赖全保留" | ✅ 已解（Docker 预编译） |
| C2 | "降低部署门槛" vs "镜像体积" | ⚠️ 待解（建议加离线镜像包） |
| C3 | "每宿舍自建" vs "34.6MB 字体" | ✅ 已解（去重，省 17.3MB） |
| C4 | "512MB VPS" vs 技术栈升级 | ✅ 已解（保持 Flask + SQLite） |
| **C5** | **"每宿舍自建" vs "隐式 CST 时区依赖"** | 🔴 **待解（见 Q10）** |

---

## 五、变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | draft-1 | Q1–Q4 确认；记录 4 项约束冲突 |
| 2026-10-06 | draft-2 | Q5–Q7 确认；技术栈锁定；新增时区风险（C5）+ 5 项待确认 |

