# StarWatt 星瓦 · 可用性（UX / 交互）审计

- 审计对象：`D:\MiniMax_Workstation\Creative_Workstation\dorm-power-monitor`（Flask 后端 `starwatt/**` + Vue 3 SPA `frontend/src/**`，产物 `static/**`）
- 审计方式：**只读静态审计**（逐文件读源码 + 全仓 grep 核对）。未运行测试、未启动服务、未对学校站点发请求。
- 使用者画像：校网宿舍电量监控的真实使用者，部署在阿里云 ECS，刚做完首次配置（OOBE）。
- 本文每条发现固定五段：标题 / 严重度 / 位置 / 证据 / 用户可见后果 / 修复建议。位置一律是**可核对的 `文件:行`**。
- 严重度定义：`blocker` = 功能不可用或误导性结论；`high` = 关键流程卡住 / 信息错误；`medium` = 明显影响效率或一致性；`low` = 打磨项。

---

## 一、页面清单（穷举）

### 1. 前端路由页面（`frontend/src/router/index.ts:27-108`）

| 路径 | 名称 | 组件文件 | 作用 | meta |
| --- | --- | --- | --- | --- |
| `/` | `dashboard` | `views/DashboardView.vue`（137 行） | 总览外壳：吸顶头部 + 四段切换 + 刷新 | 非 public |
| `/login` | `login` | `views/LoginView.vue` | 登录；`?redirect=` 回跳、`?expired=1` 过期提示 | `public: true` |
| `/password` | `password` | `views/PasswordView.vue` | 修改密码；**同时承担首次登录强制改密** | 非 public |
| `/oobe` | `oobe` | `views/OobeView.vue`（418 行） | 首次配置向导（5 步） | `admin: true` |
| `/forgot` | `forgot` | `views/ForgotPasswordView.vue` | 忘记密码：**无表单**，只给一段 CLI 命令 + 复制按钮 | `public: true` |
| `/admin` | — | `views/admin/AdminShell.vue` | 管理后台外壳；`children: [{ path: '', redirect: admin-config }]`（`index.ts:63`） | `admin: true` |
| `/admin/config` | `admin-config` | `views/admin/AdminConfigView.vue`（16.5 KB） | 配置中心：schema 驱动、分组、保存/导出/导入/恢复默认 | `admin: true` |
| `/admin/switches` | `admin-switches` | `views/admin/AdminSwitchesView.vue` | 功能开关：开关 + 安静时段 | `admin: true` |
| `/admin/logging` | `admin-logging` | `views/admin/AdminLoggingView.vue` | 日志设置 | `admin: true` |
| `/admin/users` | `admin-users` | `views/admin/AdminUsersView.vue` | 用户管理：新建/角色/禁用/重置密码（模态）/删除 | `admin: true` |
| `/admin/test` | `admin-test` | `views/admin/AdminTestView.vue` | 连通测试：飞书群机器人 / QQ 机器人 | `admin: true` |
| `/admin/audit` | `admin-audit` | `views/admin/AdminAuditView.vue` | 审计日志：筛选 + 表格 | `admin: true` |
| 其余任意路径 | `not-found` | `views/NotFoundView.vue` | 404 页 | `public: true` |

### 2. 仪表盘内的四个「段」（同一路由内切换，`useSection.ts` 记到 `localStorage['dorm-power-monitor.section']`）

| 段 | key | 组件文件 | 作用 |
| --- | --- | --- | --- |
| 概览 | `overview` | `views/sections/OverviewSection.vue` | 剩余电量 hero、连接状态、4 张统计卡、趋势图、24/72/168/720 小时区间 |
| 历史 | `history` | `views/sections/HistorySection.vue` | 每日用电图、采集记录表（快速区间 + 自定义区间）、`TABLE_LIMIT=100` |
| 违规 | `violations` | `views/sections/ViolationsSection.vue` | 近 30 天违规 + 近 90 天缴费两张表 |
| 电表 | `meter` | `views/sections/MeterSection.vue` | 电压/电流/有功功率/上报时间 + 离线提示 |

### 3. 非路由但用户会看到的界面

| 界面 | 位置 | 作用 |
| --- | --- | --- |
| 启动态 | `App.vue`（`!auth.ready` → `<p role="status">正在加载…</p>`） | 首屏探身份期间 |
| 无 JS 降级页 | `static/index.html` 的 `<noscript>` 块 | 禁用 JS / curl 访问时的说明页 |
| 后端渲染模板 | **不存在** | 无 `starwatt/web/templates`；`starwatt/web/factory.py` 只把 `static/index.html` 作为 SPA 壳返回，全部界面由前端渲染 |

> 计数：13 条路由（12 个视图 + 1 个纯外壳）/ 4 个内嵌段 / 2 个非路由界面。

---

## 二、发现

### B1 · OOBE 完成后被守卫弹回向导（前端缓存永不失效）

**严重度：blocker**

**位置**
- `frontend/src/stores/auth.ts:88`（早退条件）
- `frontend/src/stores/auth.ts:90`（缓存写入）
- `frontend/src/router/index.ts:146-151`（守卫弹回）
- `frontend/src/views/OobeView.vue:129-133`（完成路径不清缓存）

**证据**

```ts
// frontend/src/stores/auth.ts:87-92
async checkOobe(): Promise<void> {
  if (!this.isAdmin || this.oobeCompleted !== null) return
  try {
    this.oobeCompleted = (await api.oobeState()).completed
  } catch {
    this.oobeCompleted = null
  }
}
```

```ts
// frontend/src/router/index.ts:146-151
if (auth.isAdmin && to.name !== 'oobe') {
  await auth.checkOobe()
  if (auth.oobeCompleted === false) {
    return { name: 'oobe', query: { redirect: to.fullPath } }
  }
}
```

```ts
// frontend/src/views/OobeView.vue:129-133
if (isLast.value && direction === 'next' && result.completed) {
  notice.value = '配置完成！正在进入仪表盘…'
  const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
  await router.replace(target)
}
```

（后端此时已置完成：`starwatt/services/oobe_service.py:272-274` → `set_state(STATE_OOBE_COMPLETED, True)`，并在响应里回 `completed: true`。）

**用户可见后果**
点完最后一步「完成」后 URL 又回到 `/oobe`，向导重新出现，永远进不去仪表盘。同理 `/admin/*` 一切入口也被重定向回向导。唯一出路是**整页刷新 F5**——因为 `frontend/src/stores/auth.ts:75` 的 `loadMe()` 会把 `oobeCompleted` 重置为 `null`，守卫下一轮才会重新查询而得 `true`。用户感知为「配置完了，但系统说我还没配」，且随机按 F5 又好了，行为像 bug 随机发生。

触发条件（精确）：管理员登录 → 首次访问任意非 public 页面（守卫查到 `completed=false` 并缓存）→ 在向导里走到最后一步成功 → `router.replace('/')` → 守卫再跑 → `checkOobe()` 因 `oobeCompleted !== null` 直接 return → `oobeCompleted === false` 仍成立 → 再弹回。**缓存永不过期，所以这个循环在本次会话里不会自行解开。**

**修复建议**
1. 最小改动：`frontend/src/views/OobeView.vue:132` 之前加一行 `auth.oobeCompleted = true`（`auth` 在该文件已实例化）。加在 `notice.value = ...` 之后、`router.replace(target)` 之前。
2. 推荐做法：在 `frontend/src/stores/auth.ts` 增加 `markOobeCompleted(): void { this.oobeCompleted = true }`，在 `OobeView.vue:132` 调用，语义清晰且避免视图直接写 store 状态。
3. `checkOobe()` 的 `oobeCompleted !== null` 早退本身是对的（省一次请求），**不要**去掉；错的是完成路径没有回写缓存。若担心状态漂移，可在 `OobeView.vue:117` 拿到 `result.completed === true` 时无条件回写。

---

### B2 · OOBE 不校验必填项，可以「一路留空」走到完成

**严重度：blocker**

**位置**
- `starwatt/services/oobe_service.py:231-280`（`advance()`：只做逐键校验，从不检查 `step.required`）
- `starwatt/services/oobe_service.py:177-182`（`_is_done` 只喂给展示用的 `steps[].done`）
- `starwatt/services/oobe_service.py:124`（学校步 `required=("dorm_openid",)`）
- `frontend/src/views/OobeView.vue:108-133`（前端 `move()` 无前置校验）

**证据**

```python
# starwatt/services/oobe_service.py:177-182
def _is_done(step: Step, values: dict[str, Any]) -> bool:
    """某步是否已填完（``required`` 全部非空）。"""
    return all(str(values.get(key) or "").strip() for key in step.required)
```

`_is_done` 的唯一调用点是 `state()`（`:222`）与 `step_payload()`（`:200`）——**只用来给步骤列表打「已填」标记**，不参与 `advance()` 的任何判断。

```python
# starwatt/services/oobe_service.py:258-275（节选）
saved: dict[str, Any] = {"applied": [], "errors": {}}
if values and not skip:
    allowed = set(step.writable)
    payload = {key: value for key, value in values.items() if key in allowed}
    if payload:
        saved = admin_service.config_update(payload)   # 只做「逐键」校验
...
if direction != "prev" and not saved["errors"] and target == LAST_INDEX:
    set_state(STATE_OOBE_COMPLETED, True)
```

同文件 `:247-251` 的文档字符串已经自己写明了风险（「最典型的后果是 `dorm_openid` 从来没写进去，用户以为配好了、抓取却永远起不来」），但**代码里没有对应守卫**：`advance()` 里没有任何一处读 `step.required`。

**用户可见后果**
新用户不填 openid / 房间号，直接连点五步「下一步」→ 后端把 `oobe_completed=True` 落库 → 跳进仪表盘，而仪表盘永远显示 `—`：概览四张卡全空、违规页说「用电一切正常，继续保持」（见 H4）、历史页说「等下一次抓取」。用户没有任何线索知道「你少了 openid」，唯一的补救是管理员自己想到去配置中心翻。这是「看起来配好了、其实一步都没生效」的静默失败。

**修复建议**
1. 后端：`starwatt/services/oobe_service.py:272` 的完成条件补上必填校验，例如
   ```python
   missing = [k for k in step.required if not str(state_values.get(k) or "").strip()]
   ```
   更贴近现有机制的做法是在 `:259` 的 `if values and not skip:` 分支里，对 `step.required` 中缺失的键写入 `saved["errors"][key] = "这一步需要先填写"`。前端 `OobeView.vue:122-126` 已经会把这个错误渲染成字段级错误 + `role="alert"` 提示（`OobeView.vue:378` 的 `:error="errors[setting.key]"`），无需改前端。
2. 内容上建议把学校步的 required 从 `("dorm_openid",)` 扩到 `("dorm_openid", "dorm_room_id")`——只有这两个都到位才可能看到自己的数据（roomId 自动发现当前不可用，见 H1）。
3. 前端可选加一道保险：`OobeView.vue` 的 `move()` 在最后一步前检查 `state.current.required`，未满足则本地提示并聚焦该字段。

---

### H1 · roomId 自动发现必然失败，界面却承诺「自动发现」，且手填入口没有任何引导

**严重度：high**

**位置**
- `starwatt/scraper/client.py:76`（`FINDUSER_PATH` 不带 `?openid=`）
- `starwatt/services/setup_service.py:213-221`（失败文案）
- `starwatt/config_registry/registry.py:176`（配置项 help 与事实相反）
- `starwatt/scraper/service.py:526-539`（抓取期的兜底发现同样失败）
- `frontend/src/views/OobeView.vue:331-370`（学校接入块：「从学校页面自动填写（可选）」`:337`；粘贴框 hint `:346`；「解析地址」+「测试连通」）

**证据**

```python
# starwatt/scraper/client.py:76
FINDUSER_PATH = "/campus/webchat/dormEmRealRead/finduser"   # 裸请求：roomId 的 value 是空字符串
```

```python
# starwatt/services/setup_service.py:213-221（节选）
if not room_id:
    ...discover_room(...)...
    return {"ok": False, ..., "error": "凭据无效，或学校页面里找不到房间号（可手工填写房间 ID）"}
```

```python
# starwatt/config_registry/registry.py:176
"dorm_room_id", "宿舍房间号", ..., help="留空则每次抓取时自动发现"
```

```python
# starwatt/scraper/service.py:536-539（节选）
raise ScrapeError("无法从 finduser 页面解析 roomId（请检查 openid，或在配置里直接填写 dorm_room_id）")
```

**用户可见后果**
新用户在向导第 2 步粘贴学校地址后点「测试连通」，**必然**收到「凭据无效，或学校页面里找不到房间号（可手工填写房间 ID）」。这条文案有三重问题：① 先说「凭据无效」，用户会去怀疑自己复制错的 openid 并反复重贴；②「可手工填写房间 ID」没说填哪个字段、也没给出可复制的值——而正确的值恰恰要靠带 `openid` 的 finduser 请求才能拿到，用户在界面上根本看不到；③ 页面上的「宿舍房间号」输入框的说明文字还是「留空则每次抓取时自动发现」，等于在劝阻用户手填。最终结果：用户卡在第 2 步，或者按提示留空跳过 → 落到 B2/H4 的「配完了什么都没有」。

**修复建议**
1. `starwatt/config_registry/registry.py:176` 的 help 改为「自动发现依赖学校页面；失败时请把学校电费页地址里的 `roomId=` 数字填到这里」。
2. `starwatt/services/oobe_service.py` 学校步的 `description`（现为「粘贴学校 H5 页面地址，自动解析凭据与房间号，并立即验证连通。」）补一句「解析不到房间号时，请手工填写下面的『宿舍房间号』」。
3. 前端把失败文案换成可操作引导：在 `frontend/src/views/OobeView.vue` 的 `verify()` 里，若 `result.room_id === ''`，改写提示并给出字段级错误，例如
   `errors.value = { ...errors.value, dorm_room_id: '未取到房间号：请把地址栏 roomId= 后面的数字填到这里，再点『测试连通』' }`（`OobeView.vue:378` 会把它显示在字段下方），并把焦点移到该字段。
4. 若技术上允许，`starwatt/scraper/client.py:76` 侧带上 `openid` 参数（需后端确认学校接口行为），才能让「自动发现」名副其实；在此之前，文案必须承认它会失败。

---

### H2 · 「稍后再配」按钮在任何情况下都无效

**严重度：high**

**位置** `frontend/src/views/OobeView.vue:224`（配合 `frontend/src/router/index.ts:146-151`）

**证据**

```html
<!-- frontend/src/views/OobeView.vue:224-227 -->
<BaseButton variant="ghost" size="sm" @click="router.push('/')">
  <template #icon><Icon name="x" :size="14" /></template>
  稍后再配
</BaseButton>
```

**用户可见后果**
点了「稍后再配」→ 跳 `/` → 守卫立刻弹回 `/oobe`，画面闪一下又回到向导，用户判断为「按钮坏了」。即使用户此刻只是想先进去看看，也没有任何合法出口（除非 B1 那套 F5 操作）。这与按钮文案承诺的语义完全相反，是最容易被当成「界面故障」的一处。

**修复建议**
守卫需要一个「用户已明确拒绝向导」的持久豁免，而不只是「已完成」：
1. `OobeView.vue:224` 改为先落标记再跳转：`localStorage.setItem('starwatt.oobe.dismissed', '1')` 然后 `router.push('/')`。
2. `router/index.ts:148` 的条件改成 `if (auth.oobeCompleted === false && localStorage.getItem('starwatt.oobe.dismissed') !== '1')`。
3. 用户主动从管理菜单进入 `/oobe` 时清除该标记（在 `OobeView.vue` 的 `onMounted` 里 `localStorage.removeItem('starwatt.oobe.dismissed')`），保证下次还能被引导回来。

---

### H3 · 房间名（`room_label`）永远拿不到，推送标题与配置项说明都不成立

**严重度：high**

**位置**
- `starwatt/services/setup_service.py:270-295`（`commit()` 不写 `room_label`）
- `starwatt/services/setup_service.py:144-146`（`parse_url()` 明明解析出了 `room_label`）
- `starwatt/scraper/service.py:543`（抓到后丢弃）
- `starwatt/notify/policies.py:450-452`（从配置键读 `room_label` → 永远是空）
- `starwatt/notify/card_builder.py:162`（空则回退通用标题）
- `starwatt/config_registry/registry.py:148`（help「留空则用学校返回的房间名」）

**证据**

```python
# starwatt/notify/policies.py:450-452（节选）
def room_label() -> str | None:
    return get_str("room_label", "").strip() or None
```

```python
# starwatt/notify/card_builder.py:162
title_room = room_label if room_label else "宿舍电量监控"
```

`setup_service.commit()` 只写 `dorm_openid`/`dorm_room_id`/`eqprice`/`dorm_base_url` 并 `set_state("last_room_id", room_id)`（`:294`）；`starwatt/config_registry/registry.py:148` 的 `room_label` 配置项**没有任何代码写过**。

**用户可见后果**
1. 飞书推送标题永远是「宿舍电量监控」，多宿舍/多房间场景无法分辨是谁的数据。
2. 配置中心「房间显示名」的说明是「留空则用学校返回的房间名」——用户按说明留空，结果**永远**是空，且页面上（见 M1）也不能确认当前监控的是哪个房间。
3. `parse_url()` 能把 `room_label` 解析出来（`:144-146`），却在 `commit()` 时被丢掉，属于「解析结果未落库」的信息损失。

**修复建议**
1. 最小："`starwatt/services/setup_service.py` 的 `commit()` 在 `room_label` 非空时一并写入 `room_label` 配置键"（与 `:294` 的 `set_state("last_room_id", ...)` 相邻）。
2. 补一条抓取期回写：`starwatt/scraper/service.py:543` 处，当抓取得到 `room_label` 且配置里的 `room_label` 为空时写回配置，保证老用户也能自动补上。
3. 若不做 1/2，则必须把 `starwatt/config_registry/registry.py:148` 的 help 改成「留空则推送标题显示为『宿舍电量监控』」，不要承诺不会发生的事。

---

### H4 · 「没有任何数据」被渲染成「一切正常」（空房间与「无违规」无法区分）

**严重度：high**

**位置**
- `starwatt/services/dashboard_service.py:419`（违规）
- `starwatt/services/dashboard_service.py:441`（缴费）与 `daily_usage()` 的同类写法
- `frontend/src/views/sections/ViolationsSection.vue:109-113`
- `frontend/src/views/sections/HistorySection.vue:282`

**证据**

```python
# starwatt/services/dashboard_service.py:419（节选）
rows = ViolationRepo.recent(room, days=days) if room else []
```

```html
<!-- frontend/src/views/sections/ViolationsSection.vue:109-113 -->
<EmptyState
  title="近 30 天没有违规记录"
  description="用电一切正常，继续保持。系统每 10 分钟检查一次，出现违规会第一时间推送提醒。"
```

```html
<!-- frontend/src/views/sections/HistorySection.vue:282 -->
description="抓取器默认每 10 分钟抄表一次；等下一次抓取成功后，记录就会出现在这里。"
```

后端在 `room_id` 为空（即从未成功发现/填写房间号）时**静默返回 `rows: []`**，前端把它当成「查到 0 条」。

**用户可见后果**
这是最危险的一类误导：用户被告知「用电一切正常，继续保持。系统每 10 分钟检查一次」，因此认为系统正在守护，而事实上系统连房间都没连上、一条数据都没抓过。真实的违规/欠费会被完全漏掉。

**修复建议**
后端载荷里已经带了 `room_id`（`starwatt/services/dashboard_service.py:398-408` 的 `room_id()`），前端直接用它分支：
1. `frontend/src/views/sections/ViolationsSection.vue:109` 的 `EmptyState` 前加条件分支：`v-if="!payload.room_id"` 时改为提示「还没有拿到房间号，抓取尚未开始 —— 请到『首次配置』或配置中心填好 openid 与房间号」，仅在 `room_id` 存在时显示「近 30 天没有违规记录」。
2. `frontend/src/views/sections/HistorySection.vue:275-283` 的记录空状态同样处理；每日用电图的 empty-text 也一起改。
3. 更彻底：后端在 `room_id` 为空时于载荷里加一个 `reason: "no_room"` 字段，避免前端靠 `room_id` 是否为空来推断。

---

### H5 · `/api/live` 的失败原因从不显示

**严重度：high**

**位置**
- `frontend/src/composables/useLive.ts`（失败时写 `error`，但无人读取）
- `frontend/src/views/DashboardView.vue:105-107`（只渲染 `refreshNotice`/`refreshError`/`payload?.stale`）

**证据**

```html
<!-- frontend/src/views/DashboardView.vue:105-107 -->
<Notice v-if="refreshNotice" tone="success">{{ refreshNotice }}</Notice>
<Notice v-if="refreshError" tone="danger">{{ refreshError }}</Notice>
<Notice v-if="payload?.stale" tone="warning">
```

`useLive` 在轮询失败时累加 `failures`（`:43`）并设置 `error`（`:45`，`payload` 不清空），`error` 也已经在 `:106` 被 return 出来，但全站没有任何模板绑定它。

```ts
// frontend/src/composables/useLive.ts:43-45
failures.value += 1
...
error.value = err instanceof Error ? err.message : '加载失败'
```

**用户可见后果**
抓取全线失败（openid 过期、学校站点改版、网络不通）时，用户只看到头部一行小字「连续失败 3 次」和一片空白/—，**完全不知道失败原因**，也没有「去哪里修」的指引。对宿舍电量这种「不响就是好」的场景，用户会误以为只是没数据，而不是系统已经坏了。

**修复建议**
在 `frontend/src/views/DashboardView.vue` 的 `payload?.stale` 那条 Notice 旁加一条：

```html
<Notice v-if="liveError" tone="danger">
  最近一次读取失败：{{ liveError }}。请到「管理后台 → 连通测试」检查配置。
</Notice>
```

`useLive` 已经返回 `error`，绑定即可（注意：管理员看得到「管理后台」入口，普通用户应换成「请联系管理员」）。

---

### M1 · 页面从不显示「当前监控的房间」

**严重度：medium**

**位置**
- `frontend/src/api/types.ts:54-63`（`LivePayload` 不含 `room_id`）
- `frontend/src/views/DashboardView.vue`（全页无 room 字样）
- `frontend/src/views/sections/MeterSection.vue:10-15`（注释确认 `LivePayload` 无 `room_id`）

**证据**
全仓 grep `frontend/src`：`room_id` / `room_label` 只出现在 `api/types.ts:102,116,128,136,305,307,314,315,323`、`api/client.ts:269-274`、`views/OobeView.vue:142-155,177,183,345`，以及 `views/sections/MeterSection.vue` 的注释里——**没有任何视图渲染它们**。违例/缴费/每日三个载荷虽然带 `room_id`，也没有一处显示。

**用户可见后果**
无法确认页面上的数字是不是自己宿舍的。手填 `roomId` 填错（例如填成隔壁宿舍）、或 `last_room_id` 是上一次测试留下的旧房间时，页面照常显示一切正常，没有任何可核对的标识；同一账号管多个房间时更无从分辨。

**修复建议**
1. 后端 `starwatt/services/dashboard_service.py:338-356` 的 live 载荷里补 `room_id` / `room_label`（`room_id()` 已在同文件可取，`room_label` 见 H3）。
2. 前端在 `frontend/src/views/DashboardView.vue` 头部（`auth.siteName` 旁）加一个小胶囊：`房间 {{ room_label || room_id || '未配置' }}`，未配置时用 `text-warning`。

---

### M2 · 前端硬编码了后端可配置的阈值（在线状态、违规功率）

**严重度：medium**

**位置**
- `frontend/src/views/sections/OverviewSection.vue:61-62`
- `frontend/src/views/sections/MeterSection.vue:45-46`
- `frontend/src/views/sections/ViolationsSection.vue:39`
- 后端来源：`starwatt/notify/policies.py:140-146`、`starwatt/config_registry/registry.py:250`、`:264`

**证据**

```ts
// frontend/src/views/sections/OverviewSection.vue:61-62（MeterSection.vue:45-46 完全相同）
/** 电表认为「在线」的三种取值（与后端 `thresholds.ONLINE_LABELS` 同义） */
const ONLINE = ['在线', '正常', '通讯正常']
```

```python
# starwatt/notify/policies.py:140-146（节选）
values = get_json("offline_status_values", None)
online = tuple(str(v) for v in values) if isinstance(values, list) and values else ("在线", "正常", "通讯正常")
return label not in online
```

```python
# starwatt/config_registry/registry.py:250 / :264
"offline_status_values", "视为在线的状态值", THRESHOLD, T.JSON, ...
"violation_power_threshold", "违规标红功率（kW）", THRESHOLD, T.FLOAT, 1.0,
```

两处注释引用的 `thresholds.ONLINE_LABELS` **在仓库里不存在**（`starwatt/domain/thresholds.py:39` 叫 `DEFAULT_ONLINE_VALUES`），真实来源是可配置键 `offline_status_values`。

**用户可见后果**
管理员在配置中心把「视为在线的状态值」改成学校实际返回的文案后，飞书侧判定正确、**网页仍按三字面值判定**：正常状态被渲染成红色「电表XXX」；反过来，把「违规标红功率」从 1 kW 改成 0.5 kW 后，飞书告警了、网页不标红。两端口径不一致，用户无法信任页面颜色。

**修复建议**
1. 后端在 `/api/live`（`starwatt/services/dashboard_service.py:338-356`）里回传生效的 `offline_status_values` 与 `violation_power_threshold`（普通用户读不到 `/api/admin/config`，只能靠载荷下发）。
2. 前端把 `OverviewSection.vue:62`、`MeterSection.vue:46`、`ViolationsSection.vue:39` 改成读接口值，保留现有常量作为兜底默认。
3. 至少先修正两处注释里的错误常量名，避免后来者以为两边「同义」。

---

### M3 · 低电阈值（红/橙/蓝档）在网页上完全没有任何体现

**严重度：medium**

**位置**
- `starwatt/config_registry/registry.py:238-246`（`threshold_red` 30 / `threshold_orange` 80 / `threshold_blue` 200）
- `frontend/src/views/sections/OverviewSection.vue:127`（hero 只显示数字与单位）

**证据**

```html
<!-- frontend/src/views/sections/OverviewSection.vue:127 附近 -->
{{ num(stats?.remain) }}
```

全仓 grep `frontend/src` 无 `threshold_red`/`threshold_orange`/`threshold_blue` 的任何读取。

**用户可见后果**
「低电红档」是这套系统最核心的告警概念（飞书卡片会按档位变色），但网页上剩余电量永远是同一个颜色。用户改了档位数值页面上没有任何变化；更重要的是，用户打开页面看不出「现在电量已经很低」，必须自己记住 30 度这条线。

**修复建议**
在 `frontend/src/views/sections/OverviewSection.vue` 的 hero 数字上按档位加颜色与文案（「电量偏低」/「电量紧张」），阈值由接口下发（同 M2 的载荷扩展）。若暂时不下发，可先只做前端默认值 + 注明「阈值见配置中心」。

---

### M4 · 「0 天有数据」在加载中与失败时都会显示

**严重度：medium**

**位置** `frontend/src/views/sections/HistorySection.vue:173`

**证据**

```html
<!-- frontend/src/views/sections/HistorySection.vue:173 -->
{{ daily?.rows.length ?? 0 }} 天有数据
```

**用户可见后果**
打开历史页的瞬间（以及 `/api/daily` 失败时）这个计数胶囊显示「0 天有数据」，与同屏的加载骨架或错误 Notice 直接矛盾；用户据此判断「真的一天数据都没有」，从而放弃等待或误报故障。

**修复建议**
改成三态显示：`dailyLoading ? '统计中…' : dailyError ? '未能统计' : \`${daily.rows.length} 天有数据\``。组件里已有 `dailyError` ref（`:177` 的 Notice 在用），只需把 loading 状态也纳入。

---

### M5 · 自定义日期区间缺乏约束，且「只显示最近 100 条」无任何说明

**严重度：medium**

**位置**
- `frontend/src/views/sections/HistorySection.vue:277`（错误 Notice）与其 `applyFilter()`
- 表格上限 `TABLE_LIMIT = 100`（同文件内定义）

**证据**
`applyFilter()` 的唯一校验是「缺一个就 `recordsError = '请同时填写起始与截止时间'`」；两个 `datetime-local` 没有 `min`/`max`；表格数据来自「反转后 `slice(0, TERM = 100)`」。

**用户可见后果**
1. 可以选 1970 年 → 今天，触发全表查询，而表格只显示 100 行，用户不知道自己看到的是哪一段，也无法判断数据是否完整。
2. 起始晚于截止时前端没有任何提示（行为依赖后端）。
3. 「共 N 条」与表格实际 100 行之间的差异没有任何说明，用户会以为记录丢了。

**修复建议**
在 `applyFilter()` 里补三条：`start > end` → 提示「起始时间不能晚于截止时间」；区间跨度过大（如 > 31 天）→ 提示并说明原因；表格上方固定加一句「最多显示最近 100 条，缩小时间范围可查看更早的记录」。

---

### M6 · 重置密码弹窗不支持键盘关闭（无 Esc、无焦点陷阱、无焦点回收）

**严重度：medium**

**位置** `frontend/src/views/admin/AdminUsersView.vue:344-372`（`role="dialog" aria-modal="true"` 的模态）

**证据**
全仓 grep `frontend/src` 的 `keydown|Escape|keyup|focus-trap|.focus()` 只命中三处：`components/SectionTabs.vue:45`（方向键切标签）、`views/admin/AdminAuditView.vue:78`（回车查询）、`views/OobeView.vue:347`（回车解析）——**没有任何一处处理 Esc 或把焦点移入弹窗**。

**用户可见后果**
键盘用户打开「改密码」弹窗后，Tab 会跳到背后的页面上（与 `aria-modal="true"` 的承诺相反），也无法按 Esc 退出，只能用鼠标点「取消」。纯键盘/读屏用户很难可靠地完成这个操作。

**修复建议**
弹窗打开时 `nextTick` 聚焦第一个输入框（`AdminUsersView.vue:361` 的新密码字段），容器加 `@keydown.esc="closeModal"`，关闭后把焦点还给「改密码」按钮；进阶做 `inert` 或焦点循环。

---

### M7 · 「禁用用户」与「角色切换」即时生效且没有二次确认

**严重度：medium**

**位置**
- `frontend/src/views/admin/AdminUsersView.vue:310-323`（禁用/启用按钮）
- `frontend/src/views/admin/AdminUsersView.vue:252-270`（角色 select 的 `@change` 直接提交）
- 对照：`frontend/src/views/admin/AdminUsersView.vue:118`（删除有 `window.confirm`）

**证据**

```html
<!-- frontend/src/views/admin/AdminUsersView.vue:313-316（节选） -->
@click="() => run(user, () => api.updateUser(user.id, { disabled: !user.disabled }), ...)"
```

```html
<!-- frontend/src/views/admin/AdminUsersView.vue:258-266（节选） -->
@change="(e) => run(user, () => api.updateUser(user.id, { role: (e.target as HTMLSelectElement).value }), `已更新 ${user.username} 的角色`)"
```

全站只有 3 处 `window.confirm`（删用户 `AdminUsersView.vue:118`、恢复默认配置 `AdminConfigView.vue:160`、导出含凭据 `AdminConfigView.vue:173`，另有导入含凭据 `:201`），「禁用」和「改角色」都不在其中。

**用户可见后果**
1. 误点一下就把舍友（或另一位管理员）禁用，对方立刻无法登录，只有一条事后 success 文案、没有撤销按钮。
2. 把「普通」误选成「管理员」等于当场授权，没有任何确认；这类误操作在触屏/小手机上概率不低（表格行很密）。

**修复建议**
`AdminUsersView.vue:315` 的禁用调用前加 `window.confirm(\`禁用 ${user.username}？该账号将立即无法登录。\`)`（启用不需要确认）；角色 select 要么改为「选择 + 保存」两步，要么在 `@change` 里先 `confirm(\`把 ${user.username} 的角色改成「${value}」？\`)`，取消时把 select 的值回滚。

---

### M8 · 开关页的并发点击被静默吞掉，开关视觉可能停在错误位置

**严重度：medium**

**位置**
- `frontend/src/views/admin/AdminSwitchesView.vue:125-127`（`if (busy.value) return`）
- `frontend/src/views/admin/AdminSwitchesView.vue:261-270`（只有当前行的复选框被 `:disabled`）

**证据**

```ts
// frontend/src/views/admin/AdminSwitchesView.vue:125-127
async function toggle(flag: FlagInfo): Promise<void> {
  if (busy.value) return      // 无任何提示
  busy.value = flag.key
```

```html
<!-- frontend/src/views/admin/AdminSwitchesView.vue:261-270（节选） -->
:class="[ busy && busy !== row.key ? 'opacity-60' : '', busy === row.key ? 'cursor-wait' : 'cursor-pointer' ]"
:disabled="busy === row.key"
```

**用户可见后果**
一个开关正在保存时点另一个开关，点击被丢弃且没有任何反馈（用户以为没点中）。更麻烦的是复选框用的是单向绑定 `:checked="row.enabled"`：浏览器已经把框翻过去了，而 Vue 因为没有状态变化不会重新 patch，用户可能看到一个「已开启」的开关其实是关闭的。

**修复建议**
把 `:disabled="busy === row.key"` 改成 `:disabled="busy !== ''"`（真正全局串行），或在 `if (busy.value)` 分支里 `fatal.value = '上一个开关正在保存，请稍候再试'`；同时把 `:checked` 换成受控写法（`@change` 后无论成功失败都基于服务端结果重新渲染）。

---

### M9 · 「跳过这步」的可见性依赖 `required`，与页脚承诺不符

**严重度：medium**

**位置**
- `frontend/src/views/OobeView.vue` 页脚三按钮（上一步 / 下一步 / 跳过这步），`skippable` 由本步 `required` 为空推导
- 定义来源：`starwatt/services/oobe_service.py:124`（学校步 `required=("dorm_openid",)`）

**证据**
学校步 `required` 非空 → 该步**不显示**「跳过这步」；而第 1 步（欢迎步）`required` 为空 → 欢迎页反而显示「跳过这步」。页脚文案承诺每步都可跳过。

**用户可见后果**
恰恰在用户最需要跳过的这一步（学校接入——roomId 走不通，见 H1）反而没有跳过按钮，用户会以为必须解决它才能继续，于是卡死在第 2 步。

**修复建议**
不要用 `required` 反推可跳过性：让后端在 `step_payload()`（`starwatt/services/oobe_service.py:192-202`）里显式返回 `skippable`，学校步设 `True`；或保留按钮但在有 `required` 时先弹确认「跳过会导致抓取无法启动，确定跳过吗？」。

---

### M10 · 图表对屏幕阅读器完全不可用

**严重度：medium**

**位置** `frontend/src/components/ChartLine.vue:187-188`

**证据**

```html
<!-- frontend/src/components/ChartLine.vue:187-188 -->
<EmptyState v-if="!values.length" title="还没有数据" :description="emptyText" icon="chart" />
<canvas v-show="values.length" ref="canvas" />
```

canvas 上没有 `role="img"`、没有 `aria-label`，也没有相邻的可读数据表。

**用户可见后果**
读屏用户只能听到同屏的「共 N 条抄表记录」这类计数，趋势、峰值、突变一概拿不到——而趋势图是概览页的核心信息。同理，任何依赖读屏的用户无法使用这个页面的主要价值。

**修复建议**
给 canvas 加 `role="img"` 与动态 `aria-label`（例如「近 24 小时剩余电量趋势：从 X kW·h 到 Y kW·h，共 N 个点」），或在图下方提供一个 `<details>` 折叠的数据表。

---

### M11 · 内部接口编号泄漏到界面（「F4 实时读数」）

**严重度：medium**

**位置** `frontend/src/views/sections/MeterSection.vue:110`（辅助说明在 `:131`）

**证据**

```html
<!-- frontend/src/views/sections/MeterSection.vue:110 -->
F4 实时读数
```

```html
<!-- frontend/src/views/sections/MeterSection.vue:131 -->
来自学校门户的 F4 接口，随抓取周期更新（默认 10 分钟一次）；后端没返回的字段显示「—」。
```

**用户可见后果**
「F4」是学校端接口编号，用户无从理解；它出现在电表状态旁边，会让用户以为这是某种「刷新模式」或版本号，进而怀疑数据是否实时。`AdminUsersView.vue:3` 的 `F5` 只在注释里，无用户影响。

**修复建议**
`MeterSection.vue:110` 改成「电表实时读数」；`:131` 的说明去掉编号，改为「来自学校门户的实时读数接口」。

---

### L1 · 浅色主题下 warning 文字对比度约 3.6:1，低于 WCAG AA 4.5:1

**严重度：low**

**位置**
- `frontend/src/styles/tokens.css:48`（`--color-warning: 190 110 6`）
- 使用处：`frontend/src/views/DashboardView.vue:69`（「连续失败 N 次」）、`frontend/src/views/sections/OverviewSection.vue:78`（「数据陈旧」胶囊）、`frontend/src/views/admin/AdminSwitchesView.vue:153`
- 同文件 `:22-24` 的注释声称两类语义色都已压深到达标

**证据**
按 WCAG 相对亮度公式手算（未用浏览器工具复核）：`#BE6E06` 在 `--color-surface #F7F8FA` 上 ≈ **3.64:1**，在 `--color-warning-soft #FEF5E9` 上 ≈ **3.59:1**；同批次的 `--color-success #15803D` ≈ 4.72:1、`--color-danger #CD2D3C` ≈ 4.89:1 均达标。`tokens.css:22-24` 的注释写「正文/次要文字与语义色在浅色底上都要过 WCAG AA（≥4.5:1）」。

**用户可见后果**
小字号（`text-xs`/`text-sm`）的「数据陈旧」「连续失败 N 次」在浅色主题下偏浅，弱视用户或在低亮度/强光环境下不易读——而这几处恰恰是需要用户注意的信息。

**修复建议**
把 `--color-warning` 压深到约 `166 95 5`（≈4.6:1），或为 warning 提供更深的文字色变量并在 chip/Notice 上使用；顺手修正 `tokens.css:22-24` 的注释（目前只有 success/danger 真的达标）。

---

### L2 · 时间戳无相对时间、无时区标注

**严重度：low**

**位置**
- `frontend/src/views/DashboardView.vue:68`（`最后更新 ${payload.latest_ts}`）
- `frontend/src/views/sections/HistorySection.vue:135`（注释「不做时区转换 —— 全栈都是 CST 朴素时间」）
- `starwatt/timeutil.py:48`（`STAMP_FMT = "%Y-%m-%d %H:%M:%S"`）
- `starwatt/config_registry/registry.py:139`（`site_timezone` 是可配置项，默认 `Asia/Shanghai`）

**证据**
全仓 grep `frontend/src` 的 `时区|北京时间|CST|Asia/Shanghai`：只命中 `HistorySection.vue:135` 的**代码注释**，界面上没有任何一处出现时区字样；所有时间都是 `YYYY-MM-DD HH:MM:SS` 原样输出。

**用户可见后果**
1. 用户要自己心算「这是多久以前的数」——而「数据是否新鲜」是这类监控页的第一诉求。
2. 部署在阿里云 ECS 上，若服务器时区/zoneinfo 配错（或管理员改了 `site_timezone`），页面时间会静静偏移而没有任何提示，用户看到的时间与实际不符却无从察觉。

**修复建议**
后端载荷补一个时区字段（或直接用 `site_timezone`），前端把头部改成「最后更新 3 分钟前（CST）」这种相对 + 绝对组合；至少在表格表头补「（北京时间）」。

---

### L3 · 术语混用：房间号 / 房间 ID / roomId / dorm_room_id / 宿舍房间号 / 房间显示名

**严重度：low**

**位置**
- `starwatt/services/setup_service.py:221`（用户可见错误里写「房间 ID」）
- `starwatt/config_registry/registry.py:148`（「房间显示名」）、`:176`（「宿舍房间号」）、`:229`（「上次发现的房间 ID」）
- `frontend/src/views/OobeView.vue`（占位符里是 `roomId=`，解析提示是「房间 未发现」）
- `starwatt/scraper/service.py:536-539`（用户可见错误里直接写配置键名 `dorm_room_id`）

**证据**
同一概念在用户可见文案里有 6 种写法：`房间 ID`（setup_service.py:221）、`宿舍房间号`（registry.py:176）、`上次发现的房间 ID`（registry.py:229）、`房间显示名`（registry.py:148）、`房间号`（oobe_service.py 学校步描述）、`roomId`（OobeView 占位符与 `roomId=` 参数名）。

**用户可见后果**
用户看到「可手工填写房间 ID」时，无法确定它指页面上那个叫做「宿舍房间号」的字段；看到「或在配置里直接填写 dorm_room_id」时（`service.py:537`），非技术用户完全无法把 `dorm_room_id` 对应到界面上的任何一个输入框。

**修复建议**
统一成「房间号」一个词；确需指界面字段时写成「房间号（配置里的『宿舍房间号』）」。`starwatt/scraper/service.py:536-539` 的错误文案改成「…请到配置中心把『宿舍房间号』填成学校电费页地址里的房间号」，不要出现内部键名。

---

### L4 · 审计日志页改「条数」后不自动查询

**严重度：low**

**位置** `frontend/src/views/admin/AdminAuditView.vue:78`（回车查询）与其 `limit` 输入框

**证据**
只有 `@keyup.enter="load"` 与「查询」按钮触发加载，`limit` 变化不会重新请求。

**用户可见后果**
用户把条数从 50 改成 200 后以为已生效，表格其实还是旧结果，会误判「就是只有这几条」。

**修复建议**
给 `limit` 加 `@change="load"`。

---

### L5 · 连通测试页的 `fatal` 分支是死代码

**严重度：low**

**位置** `frontend/src/views/admin/AdminTestView.vue:79`

**证据**

```html
<!-- frontend/src/views/admin/AdminTestView.vue:79 -->
<Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>
```

`fatal` 这个 ref 在整个文件中从未被赋值（失败结果通过 400 响应体 + 结果 Notice 展示，见 `:110`）。

**用户可见后果**
当前没有可见后果（失败仍能正确展示）；风险在于后续有人依赖这个分支做错误展示时会静默失效，且读代码的人会误以为已覆盖错误态。

**修复建议**
删除该 Notice，或在 `catch` 分支里补 `fatal.value = err instanceof Error ? err.message : '请求失败'`。

---

### L6 · 配完之后没有「第一次抓取何时到来」的预期管理

**严重度：low**

**位置**
- `frontend/src/views/OobeView.vue:130`（完成文案「配置完成！正在进入仪表盘…」）
- `starwatt/config_registry/registry.py:184`（`scrape_interval_sec` 默认 600 秒，且用户可改）

**证据**
向导完成文案没有任何时间预期；仪表盘首屏在无数据时只有 `正在加载…`（`DashboardView.vue` 头部）与段内空状态。

**用户可见后果**
用户配完立刻看到空白页面，不知道「要等最多 10 分钟才会出现第一条数据」，于是反复点「刷新」（会真的触发一次抓取，可能触发限流/风控）或直接判定失败。

**修复建议**
`OobeView.vue:130` 的文案补一句「首次抓取可能需要最多 10 分钟，稍后回到总览即可看到数据」；或在仪表盘空状态里用 `last_scrape_at` 显示「距下次抓取约 N 分钟」。

---

## 三、已检查未发现问题

以下项逐条读过对应代码，未发现可用性缺陷（记录以便复审查漏）：

1. **键盘焦点可见**：`frontend/src/styles/main.css:22-24` 全局 `:focus-visible { outline: 2px solid rgb(var(--color-brand)); outline-offset: 2px }`，全站未被覆盖。
2. **动效降级**：`frontend/src/styles/main.css:37` 有 `@media (prefers-reduced-motion: reduce)`。
3. **表单错误关联**：`frontend/src/components/BaseField.vue` 用 `aria-describedby` + `aria-invalid` 关联错误文本；`hint` 在出错时让位给错误信息，不会同时堆两行。
4. **按钮默认类型**：`frontend/src/components/BaseButton.vue` 的 `type` 默认 `'button'`，不会在表单里误触发提交；`loading` 时 `aria-busy`。
5. **密码可见性开关**：`BaseField.vue` 的密码明暗切换用 `type="button"` + `aria-pressed` + 动态 `aria-label`。
6. **骨架屏不冒充数据**：`frontend/src/components/StatCard.vue` 的 `loading` 用骨架块而不是 `'—'`；`ViolationsSection.vue` / `HistorySection.vue` 有 3 行骨架 + `:aria-busy`。
7. **空值的统一呈现**：`OverviewSection.vue` 的 `num()`/`text()` 把 `null`/`undefined`/空串统一成 `'—'`，表格用 `{{ row.read_time ?? '—' }}`，`ChartLine` 的 tooltip 对 `null` 显示 `'—'`。
8. **标签页键盘操作**：`frontend/src/components/SectionTabs.vue:45` 有 `@keydown`，`role="tab"` + `:aria-selected` + roving `tabindex` 正确。
9. **危险操作确认**（已覆盖的 4 处）：删除用户 `AdminUsersView.vue:118`；恢复默认配置 `AdminConfigView.vue:160`；导出含凭据 `:173`；导入接受含凭据 `:201`。删除类操作文案都写明了不可逆/外传风险。
10. **表格可用性**：`HistorySection.vue` 的表格有 `<caption class="sr-only">`、sticky 表头、`min-w-[34rem]` + `overflow-auto`（窄屏可横向滚动而不挤压）；`AdminAuditView.vue` 的详情用 `title` 保留全文并截断 120 字符加省略号。
11. **密钥字段的脱敏与回写保护**：`frontend/src/components/config/ConfigField.vue` 里 secret 只有 `secretDirty` 为真才进 payload，不会把界面上显示的 `••••1234` 写回服务器；`OobeView.vue:119-120` 提交后清空本地 `edited`。
12. **部分成功的表达**：`AdminConfigView.vue:236-237` 同时给出 success 提示与 warning 的「N 项未通过校验」，错误通过 `:error="errors[setting.key]"`（`:374`）落到具体字段；`AdminLoggingView.vue:293` 同构。
13. **会话过期与强制改密的提示**：401 时 `router/index.ts:127-129` 带 `?expired=1`，`LoginView.vue:75` 用 warning Notice 解释「原目标页面会在登录后自动打开」；`must_change_password` 有专门的 403 文案与「换掉它才能继续使用其它功能」的说明。
14. **单位标注的一致性**：电量 `kW·h`、金额 `元`、功率 `kW`（违规页）/`W`（电表页即时读数）都在行内或表头标注；`ViolationsSection.vue:39` 的 `HIGH_POWER_KW = 1` 与后端 `wg_power` 的 kW 口径一致（legacy 的 `> 1000` 是 W 口径的历史 bug，已在重写中修掉，见该文件 `:8-10` 的注释）。
15. **主题切换后图表重绘**：`ChartLine.vue` 用 `MutationObserver` 监听 `<html>` 的 class 变化重画，避免切深浅色后图表颜色不同步。
16. **主题色可读性**：`frontend/src/theme.ts:60-62` 按相对亮度在品牌底色上选黑/白前景（`--color-brand-ink`），修掉了「用户改成浅色主题色 → 按钮白字看不见」的问题。
17. **轮询的省电行为**：`frontend/src/composables/useLive.ts` 用 `document.visibilityState` 判定，页面隐藏时停止轮询、恢复时立即拉取一次。
18. **离线的显式告警**：`MeterSection.vue:114-119` 对「电表不在线」「还没读到状态」「等待首次抓取」三种情况分别给了 danger/warning/info 三档提示，故障时不会静默。

---

## 四、未能验证的部分

以下内容本次审计**没有**验证，结论以静态代码为准，落地修改前建议实测：

1. **浏览器实测完全缺席**：任务要求只读且不联网安装依赖，因此没有启动前后端、没有用 axe / Lighthouse / 真机复核。所有布局、移动端换行、字体渲染、对比度（含 L1 的 3.6:1）都是按源码与 WCAG 公式推算的结果，未经浏览器测量。
2. **B1 的端到端复现未做**：复现路径（登录 → 触发守卫缓存 → 走完向导 → 被弹回）由 `router/index.ts:146-151` + `stores/auth.ts:88` + `OobeView.vue:129-133` 的代码推导，未实际点击验证。触发条件对「用户刚做完首次配置」这一报告场景成立，但若用户在向导期间刷新过页面（`loadMe()` 会重置缓存），表现会不同——这也是用户会觉得「时好时坏」的原因。
3. **B2 的运行时证据未取**：未运行 Python 测试、未向 `/api/oobe/advance` 发请求（会写 `records.db` 的 state 表，超出只读边界）。「全部留空也能标记完成」是从 `oobe_service.py:258-275` 中不存在任何 `required` 分支推出的，建议用一次独立实例实测确认。
4. **`roomId` 自动发现失败沿用任务书给定的事实**：未对真实学校站点发请求，也未验证「拼接 `openid` 后能得到真房间号」的具体返回结构；`H1` 的修复建议里「给 `FINDUSER_PATH` 带 `openid`」需要后端确认接口可行性。
5. **时区与服务器时钟未核对**：`starwatt/timeutil.py` 的时间来源与 ECS 实际时区未验证，L2 只断言了「界面不标注时区」这一事实。
6. **触控目标尺寸未测量**：375px / 768px 视口下按钮与表格行高是否达到 44–48px 的触控建议，仅从 class 名（`size="sm"`、`py-1`）推测可能偏小。
7. **构建产物与源码是否一致未验证**：`static/assets/index-C68XpXFq.js` 是构建产物，本次审计全部以 `frontend/src/**` 为准。若线上运行的是旧包，本文所有前端修复位置都需要重新 `build` 后才生效；也未逐字节比对产物与源码。
8. **读屏实际表现未测**：M10（图表无文本替代）、M6（弹窗焦点）的具体障碍程度，未用 NVDA / VoiceOver 实测。
9. **后端在异常输入下的响应未测**：例如自定义区间起止颠倒、`limit` 超界、导出/导入畸形 JSON 时前端会显示什么，只读了前端的错误出口（`ApiError` → `Notice`），未验证文案可读性。
10. **未审计的领域**：部署脚本（`deploy/**`）、`.github/**`、告警推送文案（`starwatt/notify/**` 的卡片契约）只在与本次 UX 问题直接相关的点上（H3、卡片标题）做了交叉核对，未做独立审计。

---

*本报告为只读审计产物；除本文件外未修改仓库中任何源码、测试或配置。*
