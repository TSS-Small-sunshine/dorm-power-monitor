# 版本号与命名体系（NAMING）

> **需求来源**：Q23 —— 引入版本号；「星」主题（**不用星座**）；中英双语
> **产品名**：**StarWatt / 星瓦**（用户定）
> **编写日期**：2026-10-07
> **状态**：草案 v2，待确认剩余 2 点

---

## 0. 命名总览

| 层 | 命名 | 说明 |
|---|---|---|
| **产品名** | **StarWatt / 星瓦** | star（宇宙）× watt（电功率） |
| **Python 包** | `starwatt` | 替代原方案的 `app/` |
| **CLI 命令** | `starwatt` | 替代 `python dorm_power.py` |
| **Docker 镜像** | `starwatt` | `ghcr.io/<owner>/starwatt` |
| **systemd 服务** | `starwatt.service` | 替代 `dorm-web.service` |
| **仓库名** | `dorm-power-monitor` | ⚠️ 待定（见 §6 待确认 N1） |
| **版本号** | `MAJOR.MINOR.PATCH` | SemVer |
| **版本代号** | 「中文 / English」 | **星 × 电** 主题 |
| **Git tag** | `v2.0.0-starcore` | ASCII 安全 |

---

## 1. 为什么 StarWatt 这个名字成立

| 维度 | 评价 |
|---|---|
| **双关精准** | `star`（恒星/宇宙）+ `watt`（瓦特，电功率单位）—— 同时命中「太空感」与「电量监控」 |
| **中英对应** | 星瓦 ↔ StarWatt，发音与语义都对齐 |
| **足够短** | 2 汉字 / 2 音节 / 8 个 ASCII 字符（vs `dorm-power-monitor` 19 字符） |
| **标识符友好** | 小写 `starwatt` 直接可用作 Python 包名 / CLI 命令 / 镜像名 / 服务名 |
| **可扩展** | 将来做「多宿舍聚合版」可叫 `starwatt-hub`；「云版」`starwatt-cloud` |
| **待核对** | PyPI / GitHub 上是否有同名知名项目（发布前需查一次） |

---

## 2. 版本号规则（SemVer）

```
v<MAJOR>.<MINOR>.<PATCH>  「<中文代号> / <English Codename>」
```

| 段 | 递增条件 | 本项目示例 |
|---|---|---|
| **MAJOR** | 破坏性变更（schema / API / 部署方式不兼容） | R69 重写 → **2.0.0** |
| **MINOR** | 向后兼容的新功能 | 加告警层 / 加通知渠道 |
| **PATCH** | 向后兼容的修复 | 修 bug / 调阈值 |

### 版本对照表（含历史回溯）

| 版本 | 代号 | 轮次 | 说明 |
|---|---|---|---|
| `1.0.0` | 初尘 / First Dust | R51c | baseline（已发布 `v1.0.0-r51c-baseline`） |
| `1.1.0` | 微光 / Glimmer | R60–R68 | bundle（已发布 `v1.1.0-r68`） |
| **`2.0.0`** | **星核 / Star Core** | **R69** | **本次重写** ← 当前目标 |

> **为什么 `2.0.0` 而不是 `1.2.0`**：R69 是**破坏性重写** ——
> 删兼容 shim（Q22）· 换调度（Q14）· 删 Basic Auth（Q21）· 换密码哈希（B1）· 换启动方式（B2）
> 按 SemVer 必须进 MAJOR。

---

## 3. 版本代号序列（星 × 电 主题）

### 3.1 主题叙事

> **一粒尘埃，如何照亮一间宿舍。**
>
> 星际尘埃（初尘）→ 微弱的光（微光）→ **恒星核心聚变点火（星核）** →
> 稳定运行（星轨）→ 电火花迸发（星火）→ 尘与光交融（尘光）→
> 电弧强化（电弧）→ 光的流动（流光）→ 汇聚成辉（凝辉）→
> 汇流成河（星河）→ 长明不灭（长明）→ 黎明（破晓）

**为什么这个主题贴切**：恒星本质上是宇宙的**聚变反应堆** —— 光与电同源。
「星 + 瓦」正好把「宇宙能量」与「电功率」缝进一个词。

### 3.2 完整代号表

| # | 中文 | English | 意象 | 适配版本 |
|---|---|---|---|---|
| 1 | **初尘** | First Dust | 星际尘埃 —— 一切之始 | `1.0.0` |
| 2 | **微光** | Glimmer | 尘埃初亮 | `1.1.0` |
| 3 | **星核** | **Star Core** | **恒星核心聚变点火 —— 重写重燃** | **`2.0.0`** ← 当前 |
| 4 | 星轨 | Star Trail | 稳定绕行 | `2.1.0` |
| 5 | 星火 | Spark | 电火花 / 星火 | `2.2.0` |
| 6 | 尘光 | Dustlight | 尘与光交融 | `2.3.0` |
| 7 | 电弧 | Arc | 电弧 / 光弧 | `2.4.0` |
| 8 | 流光 | Streamer | 光的流动 | `2.5.0` |
| 9 | 凝辉 | Coalesce | 汇聚成辉 | `2.6.0` |
| 10 | 星河 | Star River | 汇流成河 | `3.0.0` |
| 11 | 长明 | Everglow | 长明不灭 | `3.1.0` |
| 12 | 破晓 | Daybreak | 新纪元 | `4.0.0` |

**速查**：初尘 First Dust · 微光 Glimmer · **星核 Star Core** · 星轨 Star Trail ·
星火 Spark · 尘光 Dustlight · 电弧 Arc · 流光 Streamer · 凝辉 Coalesce ·
星河 Star River · 长明 Everglow · 破晓 Daybreak

### 3.3 明确禁止的命名类型

| 禁止 | 例子 | 原因 |
|---|---|---|
| ❌ **星座名** | 猎户座 / 天琴座 / 仙女座 / **Orion / Lyra / Andromeda** | **用户明确要求** |
| ❌ 具体星名 | 织女 / 牛郎 / **Vega / Sirius / Polaris** | 同属星座体系 |
| ❌ 天体编号 | NGC-224 / M31 | 太技术化 |
| ❌ 神话人名 | 阿波罗 / **Apollo** / 嫦娥 | 跑题 |
| ❌ 行星名 | 火星 / 木星 / **Mars / Jupiter** | 是行星不是星尘 |

### 3.4 命名规则

| 规则 | 说明 |
|---|---|
| **中英双语** | 中文 2 字；英文 1–2 词，首字母大写 |
| **意象可读** | 中文一眼懂；英文不做生僻词 |
| **不重复** | 一个代号只用一个版本 |
| **顺序感** | 整体呈现「尘埃 → 光」的上升叙事 |
| **ASCII 安全** | tag 用英文小写（`v2.0.0-starcore`） |

---

## 4. 落点清单（12 处）

| # | 位置 | 内容 | 何时 |
|---|---|---|---|
| 1 | `pyproject.toml` | `name = "starwatt"`<br>`version = "2.0.0"`<br>`description = "星瓦 · StarWatt —— 宿舍电量监控"` | M0.3 |
| 2 | `starwatt/__init__.py` | `__version__ = "2.0.0"`<br>`__codename__ = ("星核", "Star Core")`<br>`__product__ = ("星瓦", "StarWatt")` | M1 |
| 3 | `GET /healthz` | `{"version": "2.0.0", "codename": "星核 / Star Core", "product": "星瓦 / StarWatt"}` | M4 |
| 4 | 前端页脚 | `星瓦 StarWatt · v2.0.0 星核` | M5 |
| 5 | SPA `<title>` | `星瓦 · 宿舍电量` | M5 |
| 6 | `CHANGELOG.md` | `## [2.0.0] 星核 / Star Core — 2026-XX-XX` | M6 |
| 7 | Git tag | `v2.0.0-starcore` | M7 |
| 8 | GitHub Release 标题 | `v2.0.0 星核 / Star Core` | M7 |
| 9 | `README.md` 头部 | `> **v2.0.0 星核 / Star Core** ← current` | M6 |
| 10 | Docker 镜像 tag | `ghcr.io/<owner>/starwatt:2.0.0-starcore` | M6 |
| 11 | systemd 服务名 | `starwatt.service` | M2 |
| 12 | CLI 入口 | `starwatt --version` → `starwatt 2.0.0 (星核 / Star Core)` | M2 |

---

## 5. 目录结构调整（因包名改为 `starwatt`）

```diff
  dorm-power-monitor/
- ├── app/                          ← 原方案
+ ├── starwatt/                     ← 新（产品名做包名）
    ├── __init__.py                 版本 + 代号
    ├── config.py
    ├── timeutil.py
    ├── scheduler.py
    ├── logging_setup.py
    ├── flags.py
    ├── protocols.py
    ├── config_registry/
    ├── db/
    ├── auth/
    ├── domain/
    ├── scraper/
    ├── notify/
    ├── services/
    └── web/
```

**连带影响**（需同步更新）：

| 项 | 原 | 新 |
|---|---|---|
| AST 守卫扫描目录 | `app/**` | `starwatt/**` |
| ruff / mypy 配置 | `files = ["app"]` | `files = ["starwatt"]` |
| CI 覆盖率 | `--cov=app` | `--cov=starwatt` |
| 分层规则 R1–R7 | `app/domain/` … | `starwatt/domain/` … |
| `web.py` 入口 | `from app.web.factory import create_app` | `from starwatt.web.factory import create_app` |
| gunicorn 目标 | `web:app` | 不变（入口模块仍是 `web.py`） |

> **成本**：方案文档里的 `app/` 需全部替换为 `starwatt/`（约 40 处）。
> **现在改很便宜**（还没写代码）；**M1 之后再改会很贵**。

---

## 6. 待你确认的 2 点

| # | 事项 | 我的建议 | 备选 |
|---|---|---|---|
| **N1** | **仓库名是否也改成 `starwatt`** | **暂不改** —— GitHub 改名会重定向旧链接，但会打断 README 徽章 / 已发布 release 链接 / 别人的 fork。可后续单独做 | 现在就改 / 永久不改 |
| **N2** | **Python 包名用 `starwatt/`** | **采用** —— 与产品名统一，`import starwatt` 比 `import app` 更好读 | 保留 `app/`（避免文档改动） |

---

## 7. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-07 | draft-1 | 初稿：星尘主题 + 10 代号 |
| 2026-10-07 | draft-2 | **产品名改为 StarWatt / 星瓦**（用户定）；代号序列重设计为「星 × 电」12 个；新增包名 / CLI / 镜像 / 服务命名；落点扩到 12 处；目录调整方案 |

---

**文档结束**
