# 阻断级问题修复方案（BLOCKER FIXES）

> **修复对象**：`PLAN_AUDIT.md` 的 5 个阻断级问题（B1–B5）+ 2 个设计级严重问题（M1 / M4）
> **修复日期**：2026-10-06
> **修复方式**：**只改文档，不写代码**
> **状态**：草案 v1，待你确认 3 个决策点

---

## 0. 结论先行

| # | 问题 | 修复方案 | 需你决策 |
|---|---|---|---|
| **B1** | armv7 与"依赖全保留"矛盾 | **剔除 pydantic + bcrypt→scrypt** → 消除全部 Rust 依赖 → armv7 反而可行 | 🔴 **是**（4 选 1） |
| **B2** | Web 服务器未决（现状是 Flask dev server） | **改用 gunicorn** + 明确参数 + 改 systemd unit | 🟡 建议确认 |
| **B3** | APScheduler 启动时机未设计 | **`post_fork` 启动 + master PID 守卫 + 优雅关闭** | 🟡 建议确认 |
| **B4** | 随机密码 × OOBE 矛盾 | **bootstrap 密码 + OOBE 删掉"建号"步**（6 步 → 5 步） | 🟡 建议确认 |
| **B5** | 配置中心 × OOBE 职责重叠 | **OOBE = 配置中心的引导子集**（共用注册表 + 共用 API） | 🟡 建议确认 |
| **M1** | meta 未分「配置/状态/缓存」 | `Setting` 补 `kind` 字段 | — |
| **M4** | 迁移框架缺失 | 最小迁移框架（约 50 行） | — |

**修复完成后**：5 个阻断级问题全部关闭，可进入 M0。

---

## 1. 🔴 B1 — armv7 与"保留全部依赖"矛盾

### 1.1 问题回顾

| 事实 | 证据 |
|---|---|
| `pydantic-core` 无 armv7 wheel | `pydantic#12194` |
| `bcrypt` 4.x/5.x 是 Rust | `pyca/bcrypt#409` |
| 方案 Dockerfile 用 `python:3.12-slim`（无 Rust 工具链） | → pip 源码编译**必然失败** |

### 1.2 四个选项的详细分析

#### 选项 ① 剔除 Rust 依赖（**推荐**）

**关键发现**：这两个 Rust 依赖**在项目里几乎没被真正使用**。

| 依赖 | 实际使用情况 | 剔除成本 |
|---|---|---|
| **`pydantic`** | ① `auth.py:136-205` **已有完整 fallback**（无 pydantic 时用 `__slots__` 类）<br>② `db/models.py` + `db/repo.py` 是唯一硬依赖 —— 而它们**生产零调用** | 🟢 **几乎为零** |
| **`bcrypt`** | `auth.py:122` 导入，用于 `hash_password` / `verify_password` 两个函数 | 🟡 需换实现 + 一次密码重置 |

**替代方案：`hashlib.scrypt`（Python 标准库）**

```python
# starwatt/auth/password.py —— 零第三方依赖
import hashlib, secrets, base64

def hash_password(plain: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(plain.encode(), salt=salt,
                        n=2**14, r=8, p=1, dklen=32)   # 16MB 内存
    return f"scrypt$16384$8$1${b64(salt)}${b64(dk)}"

def verify_password(plain: str, stored: str) -> bool:
    if not stored.startswith("scrypt$"):
        return False                        # 旧 bcrypt 哈希 → 走重置流程
    _, n, r, p, salt_b64, dk_b64 = stored.split("$")
    dk = hashlib.scrypt(plain.encode(), salt=unb64(salt_b64),
                        n=int(n), r=int(r), p=int(p), dklen=32)
    return secrets.compare_digest(dk, unb64(dk_b64))
```

**安全性对比**：

| 维度 | bcrypt (rounds=12) | scrypt (N=2^14) |
|---|---|---|
| 内存消耗 | ~4 KB | **16 MB** |
| 抗 GPU | 中 | **强**（内存硬） |
| 抗 ASIC | 中 | **强** |
| 耗时 | ~250 ms | ~100 ms（可调） |
| OWASP 推荐 | ✅ | ✅ |
| 第三方依赖 | ❌ 需要 Rust | ✅ **stdlib** |

→ **scrypt 不弱于 bcrypt，且是标准库。**

**剔除后的依赖树**：

| 依赖 | 类型 | armv7 可行性 |
|---|---|---|
| `flask` / `requests` / `python-dotenv` / `gunicorn` / `APScheduler` | 纯 Python | ✅ 无编译 |
| `pycryptodome` | C | ✅ 需 `gcc`（普通 C，编译容易） |
| `pillow` | C | ✅ 需 `libjpeg-dev zlib1g-dev libfreetype6-dev`（普通 C） |
| ~~`pydantic`~~ | ~~Rust~~ | ✅ **已剔除** |
| ~~`bcrypt`~~ | ~~Rust~~ | ✅ **已剔除** |

**结论：剔除后 armv7 只需编译两个普通 C 扩展 —— 完全可行。**

**唯一代价：密码哈希格式变更 → 需要一次密码重置**

| 场景 | 影响 |
|---|---|
| 你的生产实例 | 升级后管理员需用**新随机密码**登录一次（与 Q19 一致） |
| 新部署（每宿舍自建） | 无历史密码，**零影响** |

#### 选项 ② 放弃 armv7

- 只支持 amd64 + arm64
- **评估**：目标用户是中国高校学生，用的是**云 VPS**（阿里云/腾讯云/雨云）—— 绝大多数是 amd64，少数 arm64
- armv7 的真实设备：树莓派 2/3、旧开发板、老路由器 —— 这些机器**跑得动**（512MB 就够）
- **代价**：明确放弃一类设备，且 Q3 的"全架构"承诺缩水

#### 选项 ③ 加 Rust 工具链 + QEMU

- Dockerfile build 阶段装 `rustup`
- QEMU 模拟下编译 pydantic-core + bcrypt：**每个 15–30 分钟**，总计 30–60 分钟
- GitHub Actions 有 7GB RAM 能扛，但要跑 30–60 分钟
- **风险**：Rust + cross-compile 的坑很多（linker / target triple / OpenSSL 链接）
- **代价**：构建脆弱、CI 慢、**仍可能失败**

#### 选项 ④ 自建 wheel 源

- 在能编译的机器上为 armv7 编译 wheel → 上传 GitHub Releases
- 用户 `pip install --find-links <url>`
- **代价**：每次依赖升级都要重新编译维护

### 1.3 推荐：**选项 ①**

**理由**：
1. **剔除的成本几乎为零**（pydantic 本来就没用；bcrypt→scrypt 是等价替换）
2. **armv7 反而变成"可支持"**（只剩普通 C 扩展）
3. 同时**简化了依赖树**（少两个重量级依赖）
4. 与 Q3（全架构）**不再冲突**

**对已确认决策的影响**：

| 决策 | 原内容 | 修订后 |
|---|---|---|
| **Q4** | 依赖**全保留** | **功能性依赖全保留**；剔除未使用的 `pydantic`；`bcrypt` → stdlib `scrypt` |

> Q4 的**本意**是"不为了兼容性而砍功能"。剔除 pydantic（未使用）和换 bcrypt（等价安全）**不损失任何功能**，符合本意。

---

## 2. 🟡 B2 — Web 服务器选型

### 2.1 决定

| 项 | 现状 | 修复后 |
|---|---|---|
| 生产启动 | `python web.py` → `app.run()`（**Flask 开发服务器**） | **gunicorn** |
| `gunicorn` 引用 | requirements 里有，代码/部署**零引用** | 真正使用 |
| systemd `ExecStart` | `.venv/bin/python web.py` | `.venv/bin/gunicorn -c gunicorn.conf.py web:app` |

### 2.2 `gunicorn.conf.py`（新增文件）

```python
# gunicorn.conf.py —— 生产与裸机共用
import os

bind            = os.environ.get("GUNICORN_BIND", "127.0.0.1:5000")
workers         = 1          # 🔴 APScheduler 必须单 worker
preload_app     = False      # 🔴 必须 False（否则调度器在 master 启动后丢失）
worker_class    = "sync"
timeout         = 120        # 抓取最慢约 30s，留足余量
graceful_timeout= 30
keepalive       = 5
accesslog       = "-"        # 交给 logging_setup 统一处理
errorlog        = "-"
loglevel        = "info"

def post_fork(server, worker):
    """在每个 worker 内启动调度器（见 B3）。"""
    from web import start_scheduler_once
    start_scheduler_once()

def worker_int(worker):
    """SIGINT —— 优雅停止调度器（Q17 稳定性）。"""
    from web import stop_scheduler
    stop_scheduler()

def worker_abort(worker):
    from web import stop_scheduler
    stop_scheduler()
```

### 2.3 `deploy/dorm-web.service` 改动

```diff
- ExecStart=/opt/dorm-power-monitor/.venv/bin/python /opt/dorm-power-monitor/web.py
+ WorkingDirectory=/opt/dorm-power-monitor
+ ExecStart=/opt/dorm-power-monitor/.venv/bin/gunicorn \
+             -c /opt/dorm-power-monitor/gunicorn.conf.py web:app
```

### 2.4 开发环境

**彻底消除 dev/prod 差异**：dev 也用 gunicorn。

```bash
# 开发
gunicorn -c gunicorn.conf.py --reload web:app
```

**`app.run()` 完全不再使用** —— 从根上消除"reloader 导致调度器启动两次"的风险。

### 2.5 新增启动守卫

```python
# web.py
import os

_MASTER_PID = os.getpid()          # 模块导入时的 PID

def start_scheduler_once():
    """幂等启动；并防御 preload 误配。"""
    if os.getpid() == _MASTER_PID:
        raise RuntimeError(
            "调度器不得在 master 进程启动：请保持 preload_app=False，"
            "并且不要使用 app.run()。"
        )
    ...
```

**这个守卫让误配变成"启动即失败"，而不是"静默重复抓取"。**

---

## 3. 🟡 B3 — APScheduler 启动时机

### 3.1 问题回顾

方案原来只有一句"启动时断言单 worker"，**不足以覆盖三种失败场景**：

| 场景 | 后果 |
|---|---|
| 模块导入时 `scheduler.start()` + `--preload` | master 启动 → fork 后线程丢失 → **调度器不工作** |
| 多 worker | 每个 worker 各启动一次 → **重复抓取 N 倍** |
| `app.run(debug=True)` reloader | 父子进程各启动一次 → **重复抓取** |

### 3.2 修复设计：三道防线

```
防线 1  gunicorn.conf.py: workers=1 + preload_app=False
防线 2  post_fork 钩子里启动（保证在 worker 内、只启动一次）
防线 3  master PID 守卫（误配 → 启动即崩，不静默出错）
        + 幂等守卫（重复调用 → 无副作用）
```

### 3.3 `web.py` 完整骨架

```python
"""web.py —— gunicorn 入口（web:app）。"""
from __future__ import annotations
import os
import logging

from starwatt.config import settings
from starwatt.logging_setup import setup_logging
from starwatt.web.factory import create_app

setup_logging(settings)
logger = logging.getLogger("web")

app = create_app()                 # 只建 app，不启动调度器

_MASTER_PID = os.getpid()          # 导入时的 PID（= master）
_scheduler = None                  # 模块级单例


def start_scheduler_once() -> None:
    """在 worker 内幂等启动调度器。由 gunicorn post_fork 调用。"""
    global _scheduler
    if _scheduler is not None:
        return                                  # 幂等
    if os.getpid() == _MASTER_PID:
        raise RuntimeError(                     # 防线 3
            "调度器不得在 master 进程启动：preload_app 必须为 False，"
            "且不要使用 app.run()。"
        )
    from starwatt.scheduler import build_scheduler
    _scheduler = build_scheduler()
    _scheduler.start()
    logger.info("scheduler started (pid=%s, jobs=%s)",
                os.getpid(), [j.id for j in _scheduler.get_jobs()])


def stop_scheduler() -> None:
    """优雅停止（SIGINT / SIGTERM）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        logger.info("scheduler stopped")
        _scheduler = None
```

### 3.4 `starwatt/scheduler.py`

```python
def build_scheduler() -> BackgroundScheduler:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BackgroundScheduler(
        timezone="Asia/Shanghai",           # Q10 一致
        job_defaults={
            "max_instances": 1,             # 防重入（替代旧 flock）
            "coalesce": True,               # 积压任务合并为一次
            "misfire_grace_time": 300,      # ⚠️ 仅覆盖"运行中阻塞"，不补跑停机期
        },
    )
    sched.add_job(scrape_job, CronTrigger(minute="*/10"), id="scrape")
    sched.add_job(l3_gate_job, CronTrigger(minute="*"), id="l3_gate")
    return sched


def scrape_job() -> None:
    try:
        from starwatt.scraper.service import run_once
        run_once(fetch_only=False)
    except Exception:
        logger.exception("scheduler: scrape_job crashed")   # 线程级兜底
```

### 3.5 三种部署方式下的行为验证

| 部署方式 | 命令 | 调度器实例数 |
|---|---|---|
| Docker（推荐） | `gunicorn -c gunicorn.conf.py web:app` | **1** ✅ |
| 裸机 systemd | 同上 | **1** ✅ |
| 开发 | `gunicorn -c gunicorn.conf.py --reload web:app` | **1** ✅ |
| ❌ 误配 `--preload` | `gunicorn --preload ...` | **启动即崩**（防线 3）✅ |
| ❌ 误配 `python web.py` | 已删除 `app.run()` 分支 | **不存在此路径** ✅ |

### 3.6 对 M3（"容器停机补跑"表述）的连带修正

原方案 §Q14 声称 `coalesce` + `misfire_grace_time` 能"处理容器停机错过的抓取" —— **这是错的**（审计 M3）。

**修正**：APScheduler 的 misfire 机制**只处理"scheduler 在运行但任务被阻塞"**。进程不在时**不补跑**。

**是否要补跑？** 两个选择：

| 选择 | 说明 |
|---|---|
| **不补跑**（推荐） | 与现有 host cron 行为一致；停机期间的数据缺口用"抓取器长时间无响应"卡（B7）提示用户；缺失的 records 行不影响图表趋势 |
| 补跑 | 启动时检查 `last_scrape_at` 缺口 → 主动抓一次。**收益低**（补一次也只补一个点），**成本中**（要处理并发与幂等） |

**决定：不补跑**，并在验收标准里把"会补跑"改为"**缺口会在下次 tick 后恢复**"。

---

## 4. 🟡 B4 — 随机密码 × OOBE 矛盾

### 4.1 问题回顾

| 决策 | 内容 | 冲突 |
|---|---|---|
| **Q19** | 安装时**随机生成**管理员密码 + 强制首登改密 | ↔ |
| **OOBE 第 6 步** | **用户自己创建管理员账号**（username + password） | 两套流程指向同一个 `users` 表 |

### 4.2 推荐方案：**Bootstrap 密码 + OOBE 删掉"建号"步**

**核心思路**：把"建号"从 OOBE 里**移出去**，放到"首次启动"；OOBE 退化为**纯配置向导**。

```
① install.sh / Docker entrypoint
   → 生成随机密码：secrets.token_urlsafe(16)
   → 写入 .env: BOOTSTRAP_ADMIN_PASSWORD=<随机值>
   → 打印到安装输出（用户必须复制）
   → 同时写 /opt/dorm-power-monitor/.initial-admin-password（chmod 600）

② 首次启动
   → 检测到 users 表为空 且 BOOTSTRAP_ADMIN_PASSWORD 存在
   → 创建 admin 账号（username=admin, password_hash=scrypt(随机值)）
   → 设 users.must_change_password = 1
   → 从 .env 中清除 BOOTSTRAP_ADMIN_PASSWORD（用后即焚）

③ 用户首次访问
   → 任意路由 → 重定向到 /login（Q21 的新登录页）
   → 登录成功 → 因 must_change_password=1 → 强制跳 /change-password
   → 改密成功 → 清除标志 → 进入 /oobe

④ OOBE（5 步，纯配置）
   → 配置完成后进入 /admin
```

### 4.3 OOBE 步骤变化

| 原（6 步） | 新（5 步） |
|---|---|
| 1 欢迎 | 1 欢迎 |
| 2 飞书 App 凭据 | 2 **学校接入**（openid → 自动解析 roomId） |
| 3 飞书 Webhook | 3 **飞书群推送**（webhook + 开关） |
| 4 cron + 时区 | 4 **飞书机器人**（App 凭据 + 开关） |
| 5 数据保留 | 5 **推送偏好 + 完成**（L1/L2/L3 开关与时间、静默时段、数据保留） |
| **6 管理员账号** | ❌ **删除**（移到安装阶段） |

### 4.4 为什么这样最好

| 收益 | 说明 |
|---|---|
| ✅ **消除 B4 矛盾** | 只有一条建号路径（安装时） |
| ✅ **有 bootstrap 保护** | 必须持有随机密码才能进入，防"部署后被人抢先建号" |
| ✅ **与 Q19 一致** | 随机密码 + 强制改密 |
| ✅ **与 Q21 一致** | 复用 `/change-password` 认证页 |
| ✅ **OOBE 变纯** | 只做配置，与 Q15 配置中心职责不重叠（见 B5） |
| ✅ **.env 用后即焚** | `BOOTSTRAP_ADMIN_PASSWORD` 用完即删，不留后门 |

### 4.5 边界情况

| 情况 | 处理 |
|---|---|
| 用户忘了随机密码 | 删除 `records.db` 重装；或手工执行 `python -m starwatt.auth.reset-admin` |
| `users` 表非空（升级场景） | 跳过 bootstrap；但**因 bcrypt→scrypt 变更，旧密码无法验证** → 走"升级时重置密码"流程（与 Q19 一致） |
| Docker 环境下用户看不到 stdout | `docker compose logs` 可查；同时写卷内 `.initial-admin-password` |

---

## 5. 🟡 B5 — 配置中心 × OOBE 职责重叠

### 5.1 问题回顾

5 项配置在 OOBE 和配置中心**两边都有**（学校 URL / 飞书 Webhook / 推送偏好 / 数据保留 / 管理员账号），用户会被两套 UI 搞混，且容易分叉（**现状就已经因为同样原因产生了新旧两套 OOBE 后端 —— L6**）。

### 5.2 推荐方案：**OOBE = 配置中心的引导子集**

**核心思路**：OOBE **不拥有任何配置逻辑**，它只是**同一批配置的分步引导视图**。

```
        ┌─────────────────────────────────────────┐
        │  config_registry（唯一配置定义）         │
        │  8 分组 / 全部配置项 / 校验规则          │
        └───────────────┬─────────────────────────┘
                        │
        ┌───────────────┴───────────────┐
        ▼                               ▼
┌───────────────────┐        ┌──────────────────────┐
│ 配置中心（全量）    │        │ OOBE（引导子集）      │
│ /admin/config      │        │ /oobe                │
│ 按 8 分组渲染全部   │        │ 按 5 步渲染指定键     │
└─────────┬─────────┘        └──────────┬───────────┘
          │                             │
          └──────────┬──────────────────┘
                     ▼
        PUT /api/admin/config  ← 同一个端点、同一套校验、同一套审计
```

### 5.3 OOBE 步骤 ↔ 注册表键映射

| OOBE 步 | 复用的注册表键 |
|---|---|
| 1 欢迎 | —（无配置） |
| 2 学校接入 | `dorm_openid` `dorm_base_url` `last_room_id` `eqprice` |
| 3 飞书群推送 | `feishu_webhook_url` `feishu_secret` `push_group_enabled` |
| 4 飞书机器人 | `feishu_app_id` `feishu_app_secret` `feishu_verification_token` `feishu_encrypt_key` `push_bot_enabled` |
| 5 推送偏好 + 完成 | `push_l1_enable` `push_l2_enable` `push_daily_enable` `push_daily_time` `push_weekly_time` `push_monthly_time` `quiet_hours_start` `quiet_hours_end` |

### 5.4 端点收敛：从 **10 个降到 2 个**

| 现状 | 重写后 |
|---|---|
| `/api/oobe/save-state` | `GET /api/oobe/state` |
| `/api/oobe/next` | `POST /api/oobe/advance` |
| `/api/oobe/prev` | ↑（同一个，带 `direction` 参数） |
| `/api/oobe/skip-step` | ↑ |
| `/api/oobe/validate-feishu` | → 合并进 `POST /api/admin/config/test` |
| `/api/oobe/validate-webhook` | → 合并进 `POST /api/admin/config/test` |
| `/api/oobe/complete` | → 合并进 `POST /api/oobe/advance` |
| `/admin/api/oobe/save`（legacy） | ❌ **删除**（消除 L6） |
| — | **合计 10 → 2** ✅ |

### 5.5 收益

| 收益 | 说明 |
|---|---|
| ✅ **消除 B5 重叠** | 一份配置定义，零分叉 |
| ✅ **消除 L6** | OOBE 双后端合并 |
| ✅ **校验一致** | OOBE 和配置中心走同一套 `validate` |
| ✅ **审计一致** | 两条路都写 `audit_log` |
| ✅ **可跳过** | 用户可跳过 OOBE 直接去配置中心，结果等价 |
| ✅ **可重入** | 配置中心随时能改 OOBE 设过的项 |

---

## 6. 🟠 M1 — meta 三类划分

### 6.1 修复：`Setting` 补 `kind` 字段

```python
class Kind(Enum):
    CONFIG = "config"    # 用户可改 → 进注册表、进 UI
    STATE  = "state"     # 运行时状态 → 不进注册表，UI 只读展示（诊断用）
    CACHE  = "cache"     # 内部缓存 → 完全隐藏（image_key 等）

@dataclass(frozen=True)
class Setting:
    key: str
    kind: Kind = Kind.CONFIG       # ★ 新增
    ...
```

### 6.2 现有 33 个 meta 键的归类

| kind | 键（共 31 个） |
|---|---|
| **CONFIG**（16） | `dorm_openid` `dorm_base_url` `eqprice` `feishu_webhook_url` `api_internal_token` `push_l1_enable` `push_l2_enable` `push_daily_enable` `push_weekly_enable` `push_monthly_enable` `push_daily_time` `push_weekly_time` `push_monthly_time` `push_receivers_l1` `push_receivers_l2` `push_receivers_report` `push_receivers_alert` `quiet_hours_start` `quiet_hours_end` |
| **STATE**（14） | `last_scrape_at` `last_scrape_status` `backfill_done` `last_daily_elect_at` `last_violation_check_at` `last_pay_at` `last_violation_alert_at` `last_low_battery_alert_at` `last_stale_alert_at` `last_daily_report_date` `last_weekly_report_iso` `last_monthly_report_mo` `oobe_step` `oobe_completed` |
| **CACHE**（2） | `img_cache_*`（image_key 缓存）、`admin_password`（**将随 Q21 删除**） |
| **特殊**（1） | `last_room_id` —— 抓取器**写**、配置层**读** → `kind=CONFIG` + `readonly_after_setup=True` |

### 6.3 UI 呈现规则

| kind | 配置中心 | OOBE |
|---|---|---|
| `CONFIG` | ✅ 可编辑 | ✅ 可编辑（若在该步映射内） |
| `STATE` | 👁 **只读**（放在"诊断"折叠区） | ❌ 不显示 |
| `CACHE` | ❌ 不显示 | ❌ 不显示 |

---

## 7. 🟠 M4 — 最小迁移框架

### 7.1 修复：版本化迁移（约 50 行）

```python
# starwatt/db/migrations.py
from __future__ import annotations
import logging
import sqlite3
from typing import Callable

logger = logging.getLogger("db")

def _has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == col for r in rows)

def _m1_baseline(conn): pass      # 现有 10 张表（init 已建）

def _m2_users_disabled(conn):
    if not _has_column(conn, "users", "disabled"):
        conn.execute("ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0")

def _m3_users_must_change_password(conn):
    if not _has_column(conn, "users", "must_change_password"):
        conn.execute(
            "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
        )

MIGRATIONS: list[tuple[int, str, Callable]] = [
    (1, "baseline",                    _m1_baseline),
    (2, "users.disabled",              _m2_users_disabled),
    (3, "users.must_change_password",  _m3_users_must_change_password),
]


def migrate(conn: sqlite3.Connection) -> None:
    """幂等迁移：按 schema_version 逐条应用，每条自身也要幂等。"""
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    current = int(row["value"]) if row and row["value"] else 0
    for ver, name, fn in MIGRATIONS:
        if ver <= current:
            continue
        logger.info("db: applying migration %d (%s)", ver, name)
        fn(conn)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(ver),),
        )
    if current < MIGRATIONS[-1][0]:
        logger.info("db: migrated %d → %d", current, MIGRATIONS[-1][0])
```

### 7.2 设计要点

| 要点 | 说明 |
|---|---|
| **幂等** | 每条迁移自身检查 `PRAGMA table_info`；`schema_version` 记录进度 |
| **可重入** | 中断后重跑从断点继续 |
| **不删列** | 只加列 / 加表 / 加索引（SQLite 的 `DROP COLUMN` 限制多） |
| **日志** | 每次迁移记 `INFO`（Q20 的 `db` 类别） |
| **测试** | 单元测试：空库 → 迁移到最新；旧库 → 增量迁移；重复跑 → 无副作用 |

### 7.3 对"零迁移"表述的修正

| 原表述 | 修正后 |
|---|---|
| "零迁移" | **"零数据迁移"** —— 现有 `records.db` 的**数据行**不需要搬动；但 **schema 有 2 个加列操作**，由上述框架自动完成 |

---

## 8. 对已确认决策的修订

| 决策 | 原内容 | 修订后 | 原因 |
|---|---|---|---|
| **Q4** | 依赖全保留 | **功能性依赖全保留**；剔除未使用的 `pydantic`；`bcrypt` → stdlib `scrypt` | B1 |
| **Q9** | "零迁移" | **"零数据迁移"**（schema 有 2 处加列，由迁移框架自动完成） | M4 |
| **Q14** | 调度器可"处理停机错过的抓取" | **不补跑**；缺口在下次 tick 后恢复 | M3 |
| **Q19** | 安装时随机密码 + 强制改密 | 明确为 **bootstrap 密码 + OOBE 删掉建号步** | B4 |
| **Q15/Q21** | 配置中心 / OOBE 各自设计 | **OOBE = 配置中心引导子集**（共用注册表 + 共用 API） | B5 |
| **Q22** | `web.py` 为新内容 | 明确为 **gunicorn 入口 `web:app`** | B2 |

---

## 9. 修复后的动工前置清单

| # | 原审计项 | 状态 |
|---|---|---|
| **P1** | B1 armv7 矛盾 | ✅ **已出方案**（推荐选项 ①）→ **待你确认** |
| **P2** | B2 gunicorn + B3 调度启动 | ✅ **已出方案**（`post_fork` + 三道防线）→ 待确认 |
| **P3** | B4 随机密码 × OOBE | ✅ **已出方案**（bootstrap + OOBE 5 步）→ 待确认 |
| **P4** | B5 配置中心 × OOBE | ✅ **已出方案**（OOBE = 引导子集，端点 10→2）→ 待确认 |
| **P5** | M1 meta 三类划分 | ✅ **已出方案**（`kind` 字段 + 33 键归类） |
| **P6** | M2 明文密码清理 | ✅ **已并入 Q21** |
| **P7** | M4 迁移框架 | ✅ **已出方案**（约 50 行） |
| **P8** | M5 重估工期 | ⚠️ **待处理**（见下） |
| **P9** | 冻结需求 | ⚠️ **已破**（Q21、Q22 已追加） |

### P8 — 工期重估

| 阶段 | 方案原估 | 审计复核 | **修复后** |
|---|---|---|---|
| M0 | 2d | 4–5d | **4d**（含 Q22 的两阶段） |
| M1 | 5d | 6–7d | **6d** |
| M2 | 3d | — | **3d** |
| M3 | 3d | — | **3d** |
| M4 | 5d | — | **5d** |
| M5 | 7d | 10–12d | **10d** |
| M6 | 4d | 6–8d | **6d** |
| M7 | 1d | — | **1d** |
| **合计** | 30d | 40–50d | **38d** |

**建议对外口径：38 天（乐观）/ 45 天（现实）/ 55 天（含返工）**

---

## 10. 待你确认的 3 个决策点

| # | 决策 | 我的推荐 | 备选 |
|---|---|---|---|
| **D1** | **B1 的修复选项** | **① 剔除 Rust 依赖**（pydantic 删 + bcrypt→scrypt） | ② 放弃 armv7 / ③ Rust+QEMU / ④ wheel 源 |
| **D2** | **B4 的建号流程** | **bootstrap 密码 + OOBE 删掉建号步**（5 步） | ① OOBE 里设密码（则 Q19 的随机密码取消） |
| **D3** | **B5 的 OOBE 定位** | **OOBE = 配置中心引导子集** | ② 两个独立入口 |

**D2/D3 若无异议，我将按推荐写入方案；D1 需要你明确选择。**

---

## 11. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | v1 | 初稿：B1–B5 修复方案 + M1/M4 设计 + 决策修订表 + 工期重估（38d） |

---

**修复方案结束** —— 本文档不含任何代码改动。
