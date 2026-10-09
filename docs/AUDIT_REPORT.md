# StarWatt 2.0 全面审计报告

> 日期：**2026-10-09** · 审计对象：`2.0.1-starcore` 及此前 `main`
> 产出：**7 个代码缺陷 + 3 个流程/文档缺陷**，全部已修并发布为 **`2.0.2-starcore`**

---

## 0. 一句话结论

`2.0.1` 里有一个**会让三个数据源静默失败**的缺陷（用户看到的是「这几段永远是空的」，
而系统报告「抓取成功」），外加一处**日志明文泄露凭据**的安全缺口，以及若干
「同一逻辑在 A 处修了、B 处漏了」的成对缺陷。**全部已修复、验证并发布**；
切换依赖的每一项假设都做了**实测**（而非推断）。

---

## 1. 审计方法（为什么这次的结论可信）

这次没有只读代码 —— 每个结论都有**可复现的证据**：

| 手段 | 做了什么 | 抓到了什么 |
|---|---|---|
| **真实响应对账** | 用真实 openid / roomId 复刻 App 的完整请求（含 openid query、微信 UA、Referer/Origin），把 F1–F5 的真实响应与解析器逐字段比对 | F2/F3/F5 返回**数组**而客户端只收对象（P0）；F4 只给 `dt` 而代码读 `updateDt` |
| **本地切换预演** | 造 legacy 库（去掉 2 列 + 删 `schema_version`）→ 走真实启动路径迁移 → 真实抓取 → **遍历全部 17 条 GET 路由** | 日均在充值后变**负数**；`live()` 的窗口副作用；迁移「一行未丢」的正面证据 |
| **程序化不变量检查** | 对 94 项配置注册表跑 7 条不变量（默认值自洽、规则串可识别、`depends_on` 指向真实布尔项…） | 全部通过 ✓（比逐行读 554 行声明更可靠）|
| **标准测试向量** | 纯 Python Ed25519 用 **RFC 8032 §7.1** 向量钉住 | 实现正确 ✓ |
| **上游状态核验** | GitHub API 查 CI / Release 状态与 GHCR 标签 | 确认发布真的可用（含 `latest`）|

**反面教材**：`test_json_array_raises` 这条测试**把错误行为当成了规范** ——
如果只信测试，这个 P0 会被判为「符合预期」。

---

## 2. 缺陷清单

### 2.1 代码缺陷（7 项）

| # | 级别 | 缺陷 | 用户可见症状 | 提交 |
|---|---|---|---|---|
| 1 | **P0** | `scraper/client.py` 的 `post_form` 拒绝 JSON **数组** | F2/F3/F5 **每次抓取都失败** → 每日用电 / 违规 / 缴费恒空，而 `run_once` 报 `ok=True` | `26644d5` |
| 2 | P1 | `RunStatusRepo` 只从 `updateDt` 取 `update_dt`，而学校 F4 只返回 `dt` | 电表「最后上报」/ 飞书离线卡 / `/dorm status` 三处恒为 `—` | `26644d5` |
| 3 | P1 | `live()` 用 24h 窗口算 `stats.daily_avg`，而该指标要求跨度 ≥ 1 天 | 概览「日均用量」恒为 `—` | `26644d5` |
| 4 | P1 | `daily_avg` 未防充值（`最旧 − 最新` 直接相减）| 充值后「日均用量」变**负数** | `2c17851` |
| 5 | P1 | `hourly_used` 的窗口副作用（**修复 #3 时引入**）| 「近一小时」可能取 6 天前的差值 | `2c17851` |
| 6 | P2 | `reports.daily_summary` 的「今日违规」按**月份前缀**过滤 | 日报把**昨天**的违规算成今天 | `f15fd9e` |
| 7 | **安全** | `RedactingFilter` 只脱敏 `record.msg`，**不覆盖 `exc_info`** | 抓取失败时 **openid 明文进日志** | `6f0de54` |

#### #1 细节（P0）

**根因**：

```python
if not isinstance(payload, dict):
    raise ScrapeError(f"{path} 返回的 JSON 不是对象")   # ← 学校 F2/F3/F5 返回数组
```

**证据**（复刻 App 的完整请求后，学校返回的首字符就是 `[`）：

```
HTTP 200  CT=application/json
[{"dt":"2026-10-08","roomNo":"6号楼-1-119","eebm":8245.35,"zongEq":9.29,"esbm":8236.06,...}]
```

**内部自相矛盾**：同文件的 `as_list()` 第一个分支就是 `isinstance(payload, list)`
—— 它**本来就是为数组写的**（docstring 写着「逐字保留 legacy `_as_list` 的行为」），
是客户端这一层把它挡死了。**这是重写引入的回归，1.x 是好的。**

**为什么静默**：`report.ok = "F1" in report.ran` —— 只要 F1 成功就报 `ok`，
3/5 接口挂掉也显示「刷新成功」。

**修复后实测**：`{'F1':1,'F4':1}` → **`{'F1':1,'F4':1,'F2':30,'F5':14}`**。

#### #7 细节（安全）

**根因**：格式化器渲染 traceback 走的是 `record.exc_info`，而脱敏只处理了 `record.msg`。
**这条链路是真实存在的**：`SchoolClient` 把请求路径构造成 `path?openid=...`，
抓取异常自带凭据，`scheduler.scrape_job` 的兜底 `logger.exception(...)` 会把它写进日志。

**修复过程中还抓到修复本身的脆弱点**：第一版写成
`if record.exc_info and not record.exc_text` —— 而 `exc_text` 会被**先跑的 handler**
缓存（pytest 插件、stdout+file 双 handler 都会），后跑的 handler 会因此**跳过脱敏**。
新测试立刻把明文打了出来，守卫已去掉。

### 2.2 流程 / 文档缺陷（3 项）

| # | 问题 | 影响 | 提交 |
|---|---|---|---|
| 8 | runbook 用 `cp` 备份 **WAL** 库（且旧服务仍在运行）| 回滚安全网可能缺最近几行；Phase 3 影子副本同理 | `a1fdd9a` |
| 9 | runbook 指向 `2.0.1` | 照做就装到有缺陷的版本 | `a1fdd9a` |
| 10 | `docker-compose.yml` 默认 tag 是 `2.0.1` | 新用户 `docker compose up -d` 装到有缺陷的版本 | `24bb5aa` |

> #8 的正确做法已写进 runbook：`sqlite3 <源库> ".backup '<目标>'"`（WAL 感知的在线
> 一致性快照）+ **立即比对行数**。

---

## 3. 为什么测试全绿却漏了这些

**三条机制**，每条都对应上面至少一个缺陷：

| 机制 | 实例 |
|---|---|
| **测试把错误行为写成了规范** | `test_json_array_raises` 断言「数组必须抛错」（#1）；`test_violations_today_counts_current_month` 只种今天的数据，两种实现都能过（#6）|
| **测试用「我以为的上游形状」造数据** | 所有 fake 都返回 dict / 都传 `updateDt`（#1 #2）；`remain_increase` 向量跨度不足 1 天，`null` 来自「跨度规则」而非「充值规则」（#4）|
| **只测函数本身，没测调用方传了什么** | `test_daily_avg_needs_a_full_day` 测的是 `stats()` ✓，没测 `live()` 传了 24h 窗口（#3）|

## 4. 那条规律

> **7 个代码缺陷里 6 个是同一个形状**：修复存在于一处，它的**孪生兄弟**被漏掉了。

| # | 修好的那一处 | 被漏掉的孪生兄弟 |
|---|---|---|
| 1 | `as_list()` 为数组而写 | `post_form` 拒绝数组 |
| 2 | 学校只给 `dt` | 代码只读 `updateDt` |
| 3 | `days_remaining()` 用 7 天窗口 | `live()` 用 24h |
| 4 | `hourly_used` 防充值 | `daily_avg` 没防 |
| 5 | `offline_card_payload` 修了键名 | **值根本没写进库** |
| 6 | 卡片写「今日违规」 | 按**月份**过滤 |

第 7 个（脱敏）是变体但同源：覆盖了 `msg`，漏了 `exc_info`。

### 建议的检查清单（已写入 `CONTRIBUTING.md`）

1. **改一处逻辑时，先搜它的孪生兄弟** —— 同一个概念往往在「后端/前端」「卡片/页面」「飞书/QQ」「live/报表」各有一份。
2. **新增测试时问一句：这条测试能不能区分「对」与「错」两种实现？**
   只种「正确路径」的数据、名字却断言了某个口径，等于把错误写成规范。
3. **涉及上游数据时，用真实响应造测试数据** —— 不是「我以为的形状」。

---

## 5. 逐层结论（读完后判定为扎实）

| 层 | 亮点 |
|---|---|
| `auth/` | 会话只存 sha256（L17）· **30 天绝对上限**（L19）· 锁定按 `(username, ip)` · bootstrap 用后即焚 · fail-closed |
| `db/` | WAL + 外键 + `connect()` 保证关闭 · 迁移幂等可重入 · **「零迁移」由预演证明** |
| `scraper/` | SSRF 四层（含逐跳复检）· 响应体积上限 · 重试策略区分传输层/状态码 · 修复后端点全部工作 |
| `notify/policies` | 幂等铁律「**发送成功才打点**」· 充值不算耗电 · stale 同一段只推一次 |
| `notify/transport` | 单一发送实现 · 签名按规范 · 失败默认不抛 · **总开关在出口二次兜底**（L21 教训）|
| `notify/crypto` | **fail-closed**（堵掉 legacy 的 fail-open）· 常量时间比较 · 16 字节盐硬切（记着「盐里 6% 概率含 `{`」）|
| `notify/ed25519` | 纯 Python（为 armv7）· **被 RFC 8032 §7.1 向量钉住** |
| `notify/dispatcher` | 图片→文本卡→纯文本三级降级 · 保底文案 |
| `scheduler` | B3 三道防线（`workers=1` / `post_fork` / PID 守卫）|
| `flags` | 17 个开关**真生效** · 跨午夜静默 · 起止相同视为关闭 |
| `config_registry` | `Kind` 是**权限边界** · secret 默认脱敏且**导入跳过掩码** · 导出只含 CONFIG · 94 项 7 条不变量全过 |
| `logging_setup` | 7 级 × 8 类 · 三级脱敏防线 · 类别级热更新 · 修复后覆盖异常栈 |
| `web/` | `read_access`/`admin_access`/`internal_token` 三层 · `create_app()` 自己跑幂等 `db.init()` · SPA 兜底只接管 GET |
| 前端 | 改密优先于放行 · 「后端才是安全边界」 |
| 部署 | 多阶段 · 非 root · 只读根 · `cap_drop: ALL` · entrypoint 把密钥持久化到数据卷 · `install.sh` 三条安全铁律 |

---

## 6. 切换相关的事实（预演实测，非推断）

| 事实 | 结果 |
|---|---|
| legacy 库原地迁移 | 2 列补齐 ✓ `schema_version` 0 → 3 ✓ **一行未丢** ✓ |
| 旧库的**明文** openid | 28 位原样读出 ✓（2.0 在下次写入时自动加密 ✓）|
| 旧配置键 | `last_room_id` / `eqprice` / `site_name` 全部继承 ✓ |
| 旧用户行 | 保留 ✓（但 bcrypt 密码需按 runbook §4.5 建 `admin2` ✓）|
| 17 条 GET 路由 | 全部 200 ✓ 无 500 ✓ |
| 真实抓取 | `{'F1':1,'F4':1,'F2':30,'F5':14}` ✓ |

**已知局限（非缺陷，属设计）**：`records` 历史**不回溯**（`selectRecord` 是**有意删除**的），
所以全新安装的「剩余电量趋势」图要从零开始积累；而**升级**场景下旧 `records` 会完整保留 ✓。

---

## 7. 遗留建议（未采纳，需产品决策）

| 项 | 现状 | 建议 | 为什么没直接改 |
|---|---|---|---|
| **`report.ok` 的语义** | `ok = "F1" in ran` → F2/F3/F5 全挂仍报成功 | 改成「F1 成功**且本轮无端点失败**」 | 会改变 `last_scrape_status`（注册表冻结取值）→ 影响 UI 与**通知**；单次抖动可能触发告警，需先定策略 |
| **日志文件体积** | `TimedRotatingFileHandler` 只按天轮转，**无单文件体积上限** | 加体积上限，或在文档注明 TRACE 会放大日志 | 默认 `log_file_enabled=False`，风险低 |
| **飞书事件重放** | 签名含时间戳，但**不校验时效** | 加时间窗（如 ±5 分钟） | 命令均为只读，重放仅产生重复回复；影响低 |

---

## 8. 覆盖范围

**已逐行读或程序化验证**：`starwatt/` 全部模块（scraper · domain · db · auth · config_registry ·
notify · services · web · scheduler · flags · logging_setup · timeutil）· 前端 `views/sections`
与 `router` · `Dockerfile` / `docker-compose.yml` / `gunicorn.conf.py` / `docker-entrypoint.sh` /
`install.sh`（结构）· CI 与 Release 工作流。

**未逐行读**：`config_registry/registry.py` 的 554 行声明（改用不变量检查器验证）·
`notify/renderer.py` 的绘制细节（接口级已读，且策略是「永不抛、降级到默认字体」）·
`install.sh` 的逐行实现（已核对三条安全铁律与关键变量）。

