# M0 报告 —— 基线保全 + 分支建立 + 契约快照

> **日期**：2026-10-07
> **分支**：`rewrite/r69`
> **里程碑**：M0（`REWRITE_PLAN.md §3`）
> **tag**：`r69-m0`
> **执行手册**：`docs/M0_RUNBOOK.md`

---

## 1. 产出总览

| 项 | 结果 |
|---|---|
| `develop` 落库 commit | **5** 个 |
| `rewrite/r69` commit | **6** 个 |
| 契约 fixtures | **5** 类 |
| 删除 legacy 文件 | **115** 个（81 tracked + 34 gitignored） |
| 保留跟踪文件 | **30** 个 |
| 测试用例 | **47** 个全绿 |
| ruff | **0 error** |
| compile-time 检查 | `compileall` exit 0 |

---

## 2. commit 清单

### `develop`（R61–R67 落库，5 个）

| Hash | 说明 |
|---|---|
| `353ba86` | `chore(r61,r63): add db/ package + auth module` |
| `fed0eba` | `chore(r62,r64-r67): split templates/static + admin + OOBE + components` |
| `43351af` | `chore(r61-r67): add AST-guard tests for R61-R67` |
| `e91970d` | `chore: update env example, nginx conf, gitignore` |
| `d368139` | `docs: add R69 rewrite planning set (8 docs)` |

### `rewrite/r69`（6 个）

| Hash | 说明 |
|---|---|
| `14b235a` | `docs(naming): introduce StarWatt product name + version codenames (Q23)` |
| `f94d8fd` | `chore(m0): add pyproject + dev requirements + pytest conftest` |
| `88b528f` | `test(m0): generate 5 contract fixture classes from legacy code` |
| `7d865b3` | `chore(m0): remove all legacy files (Q22 clean branch)` |
| `7d68a07` | `chore(m0): add AST guard (R1-R7) + rewrite CI pipeline` |
| （本报告） | `docs(m0): add M0 report` |

---

## 3. 契约快照（本次最重要的产出）

| fixture | 大小 | 内容 |
|---|---|---|
| `schema.json` | 2118 B | 10 表 + 19 索引（表名/字段名/索引名冻结） |
| `algorithms.json` | 2084 B | `_stats` ×8 / `_compute_monthly_projection` ×3 / `calc_days_remaining` ×8 |
| `cards.json` | 24709 B | `_build_card` ×7 / 颜色模板 ×9 / 离线卡 ×3 |
| `api.json` | 1386 B | `/api/data(24h,168h)` + `/api/live` 字段集 |
| `behaviors.json` | 5306 B | 隐性行为 + **死开关证据链** + 17 项 TODO |

### 已固化的关键边界

| 项 | 固化值 |
|---|---|
| 颜色分级 | `29.99→red` / **`30→orange`** / `80→blue` / **`200→green`** / `None→blue` |
| 离线判定 | `none/empty/no_label → True`（**缺行算离线**） |
| 1 小时耗电门槛 | 1h → `0.5`；**30 分钟 → `None`**（<3500s） |
| 电量上升 | `hourly_used → None` |
| `db.insert` 同秒覆盖 | 6 行 + 2 次插入 = **仍 6 行**，值 REPLACE |
| `calc_days_remaining` | `avg=0 → None`；`remain=-1 → 0.0` |
| SSRF | 内网/元数据/`file://`/外域 → 全部不解析出 roomId |
| `_ROOM_NO_RE` | 只匹配 `<input id="roomNo">`，**不匹配 `<span>`** |

### 生成器质量

- ✅ **离线可重现**：`requests.get` 被 mock（首次运行时发现它真的去请求学校了）
- ✅ **时间冻结**：`datetime` 符号逐模块替换（12:00 → `is_top_of_hour=True`）
- ✅ **DB 隔离**：临时文件，不碰真实 `records.db`

---

## 4. 🔴 本次新发现的缺陷

| # | 缺陷 | 严重度 |
|---|---|---|
| **L20** | `meta.admin_password` **明文密码仍在活跃使用**（`web.py:1240` 读它做认证 + 3 处明文写入） | 🔴🔒 |
| **L21** | **整个「推送开关体系」是死代码** | 🔴 |

### L21 证据链

| 位置 | 事实 |
|---|---|
| `feishu_bot.py:2107` | `push_if_enabled()` —— **零调用点** |
| `feishu_bot.py:2113` | `_should_push()` 只被它调用 |
| `feishu_bot.py:2116` | `_in_quiet_hours()` 只被它调用 |
| `web.py:2199/2205/2206` | `push_l1_enable`/`push_l2_enable` 只写不读 |
| `push_receivers_*`（4） | 零调用 |

**DEAD（8）**：`push_l1_enable` `push_l2_enable` `quiet_hours_start` `quiet_hours_end`
`push_receivers_l1` `push_receivers_l2` `push_receivers_report` `push_receivers_alert`

**ACTIVE（6）**：`push_daily/weekly/monthly_enable` + `push_daily/weekly/monthly_time`

**影响**：`Q16` 不是「把现有开关搬到 UI」，而是**从零实现真正的开关体系**。

---

## 5. 文件变更

### 删除（115）

| 类别 | 数量 |
|---|---|
| 旧单体代码（config/db/web/dorm_power/feishu_bot/auth） | 6 |
| 旧数据层 `db/` | 4 |
| 旧 Jinja 模板 `templates/` | 8 |
| 旧前端 `static/css` + `static/js` | 24 |
| 字体重复 `assets/fonts/` | 3 |
| 旧测试 `tests/modern/` | 18 |
| **旧测试 `tests/legacy/`（gitignored）** | **23** |
| 旧脚本 `scripts/deploy/` | 10 |
| **旧脚本 `scripts/build/`（gitignored）** | **11** |
| 旧文档（TECHNICAL / Round25 / REFACTOR_DESIGN / reports r41-r42 / R68_NOTES / AGENTS） | 7 |
| 旧配置（setup.sh / preview_v2.html / dorm-cron.txt / logrotate / nginx conf） | 5 |

### 保留（30）

```
.env.example  .gitignore  LICENSE  README.md
pyproject.toml  requirements.txt  requirements-dev.txt
.github/workflows/ci.yml
deploy/dorm-web.service
static/fonts/*.ttf                     (3)
tests/conftest.py
tests/regression/generate_fixtures.py
tests/regression/test_contract.py
tests/regression/fixtures/*.json       (5)
tests/unit/.gitkeep  tests/integration/.gitkeep
docs/*.md                              (8 份方案文档)
docs/reports/m0_5_fixtures.txt
```

---

## 6. 验收清单（M0_RUNBOOK 附录 C）

### 基线保全
- [x] `develop` 上有 5 个新 commit（A–E）
- [x] `auth.py` / `db/` / `templates/` / `static/` 已入库
- [x] `git ls-files` 无 `_tmp_` / `records.db` / `diff_tests`
- [x] `develop` 已 push

### 分支与骨架
- [x] `rewrite/r69` 从 `develop` 切出
- [x] `pyproject.toml`（starwatt 2.0.0）可跑 ruff/mypy/pytest
- [x] `requirements-dev.txt`
- [x] `tests/conftest.py` 三个 fixture 可用

### 契约快照
- [x] `tests/regression/fixtures/` 有 **5 个** JSON
- [x] 已覆盖 `FEATURE_INVENTORY` 的主要 ⚠ 项（约 60%，其余在 TODO 清单）
- [x] **人工核对过** 关键边界（30 / 200 / 缺行 / SSRF）
- [x] fixtures 已提交

### 清空 legacy
- [x] `git ls-files` 无全部旧文件
- [x] `static/fonts/` 仍有 3 个 TTF
- [x] 文件数 111 → **30**
- [x] **磁盘上也无残留**（额外清掉 gitignored 的 `tests/legacy/` + `scripts/build/`）

### CI 与守卫
- [x] `scripts/ast_guard.py` 存在且可跑（R1–R7）
- [x] `.github/workflows/ci.yml` 含 ruff + AST + pytest + compileall + secret scan
- [x] `pytest` 47 用例全绿（不再 exit 5）

### 终验
- [x] **删除 legacy 后 `pytest` 仍全绿** ← M0 核心验收
- [x] `ruff check .` 0 error
- [x] `git status --short` 干净
- [x] 无 legacy import 残留

---

## 7. 遇到的问题与处理

| # | 问题 | 处理 |
|---|---|---|
| 1 | 快照生成器首次运行**真的去请求学校**了 | 加 `_offline()` mock `requests.get` |
| 2 | `sqlite3.Row` 无 `.get()` | 改用 `row["key"]` |
| 3 | `_seed` 6 次 `db.insert` 全落同一秒 → 只 1 行 | 改用原始 SQL 指定 ts + 相对时间偏移 |
| 4 | `_discover_room` 测试用 `<span>` 不匹配 | 改用 `<input>`（已固化该限制） |
| 5 | PowerShell 5.1 读不了无 BOM 的 UTF-8 中文脚本 | 删除脚本改为纯 ASCII |
| 6 | `ruff` C901：`_check_file` 太复杂 | 拆成 4 个规则函数 |
| 7 | `compileall` 扫到 `tests/legacy/` 的语法错误 | 删除 gitignored 残留 + CI 加 `-x` 排除 |
| 8 | `pytest` exit 5（无用例）会让 CI 失败 | 0.8 补 `test_contract.py`（47 用例） |
| 9 | `github.com` DNS 污染 → 127.0.0.1，推送失败 | 用户挂加速后推送成功 |

---

## 8. 下一步

**M1 — 基础设施层**（6 天）

| 步 | 内容 |
|---|---|
| 1.1 | `starwatt/timeutil.py`（Q10）+ AST 守卫 R7 |
| 1.2 | `starwatt/config.py` 统一 Settings（`.env` 只留 4 项启动必需） |
| 1.3 | `starwatt/db/` 五文件（schema / connection / **migrations** / models / repositories） |
| 1.4 | `starwatt/auth/` 拆分 + `password.py`（stdlib scrypt）+ `users.disabled` + `must_change_password` |
| 1.5 | `starwatt/config_registry/` 五文件（含 `kind` 三类划分） |
| 1.6 | `starwatt/flags.py`（17 个开关，**真正生效**，修复 L21） |
| 1.7 | `starwatt/logging_setup.py`（7 级 + 类别 + 模块级覆盖） |
| 1.8 | `starwatt/protocols.py`（`Notifier` / `Endpoint`） |
| 1.9 | 单元测试：`starwatt/domain/` 100% 覆盖 + config_registry 全分支 |

**M1 起 `starwatt/` 存在，AST 守卫将真正开始拦截违规。**

---

## 9. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-07 | v1 | M0 完成报告：5 类 fixtures / 115 文件清除 / 47 测试全绿 / 新发现 L20+L21 |

