# 功能现状清单（FEATURE INVENTORY）

> **用途**：重写前的功能盘点。请逐项在「决策」列填：`保留` / `改` / `删` / `?`（待定）
> **基线**：`develop` @ `bb031f6` + 工作区未提交的 R61–R67 成果
> **编写日期**：2026-10-06
> **标注约定**：⚠ = **隐性行为**（界面上看不见，只在代码里，重写最易丢失）
> 　　　　　　🔒 = 安全相关，不可省略
> 　　　　　　📌 = 有历史踩坑记录，重写时必须保留其结论

---

## 0. 汇总

| 域 | 项数 | 其中隐性 ⚠ | 其中安全 🔒 | 其中踩坑 📌 |
|---|---|---|---|---|
| A 数据采集 | 11 | 6 | 1 | 2 |
| B 推送通知 | 12 | 8 | 0 | 2 |
| C 飞书私聊机器人 | 10 | 4 | 1 | 2 |
| D 飞书事件安全 | 5 | 2 | 5 | 1 |
| E Web 仪表盘 | 18 | 5 | 0 | 3 |
| F Admin 后台 | 8 | 1 | 3 | 1 |
| G 认证与安全 | 12 | 3 | 12 | 2 |
| H OOBE 向导 | 7 | 2 | 1 | 1 |
| I 健康检查 | 3 | 0 | 0 | 0 |
| J 运维部署 | 7 | 2 | 1 | 2 |
| K 非功能约定 | 12 | 12 | 3 | 3 |
| **合计** | **105** | **45** | **27** | **19** |

> **关键数字**：105 项功能里，**45 项（43%）是隐性行为** —— 这就是重写的最大风险来源。

---

## A. 数据采集（11 项）

| # | 功能 | 触发 | 当前行为（含参数） | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|---|
| A1 | roomId 自动发现 | 每次抓取（当 `DORM_ROOM_ID` 为空） | GET `/dormEmRealRead/finduser`，正则解析隐藏 `<input id="roomId">` 与房间名；有 env 则跳过 | `dorm_power.py:423,2162-2172` | ⚠ | |
| A2 | **F1 实时电量** | 每次抓取（10 分钟） | POST `getEmRealRead`，取 9 字段：`remainEq / freeEq / rechargeEq / useEq / totalEq / remainWqMoney / dt` | `dorm_power.py:509` | | |
| A3 | **F2 每日用电** | **24h 节流** | POST `getEmDayElectQuery`，30 天窗口，字段 `esbm / eebm / total_eq / zong_eq`（累积表码） | `dorm_power.py:784` | ⚠ | |
| A4 | **F3 违规记录** | **1h 节流** | POST `selectWgElect`，30 天窗口，字段 `wg_reason / wg_power`（**单位 kW 不是 kW·h**） | `dorm_power.py:799` | ⚠📌 | |
| A5 | **F4 电表状态** | 每次抓取 | POST `getEmRunStatus`，字段 `vol / cur / yggl / runStatus / meterNo / updateDt / stopReason` | `dorm_power.py:814` | | |
| A6 | **F5 缴费历史** | **24h 节流** | POST `getEmPayQuery`，日期**锚定 2026-09-07 学期起点**（不是 30 天滚动） | `dorm_power.py:839,849` | ⚠📌 | |
| A7 | 30 天历史回填 | **一次性** | F1 `selectRecord` + F2 + F5；**三者全成功**才写 `meta.backfill_done=1`，否则下次 cron 重试 | `dorm_power.py:982-1069` | ⚠ | |
| A8 | 单价缓存 | 首次抓取 | 读 `meta.eqprice`，缺失时走 `env DORM_EQPRICE → 0.5`；**仅在非默认值（≠0.5）时写回 meta** | `dorm_power.py:2230-2241` | ⚠ | |
| A9 | 请求伪装 | 每次 HTTP | 微信 iOS UA（`MicroMessenger/8.0.49`）+ Referer 指向 `/finduser` + `X-Requested-With` | `dorm_power.py:115,687` | ⚠ | |
| A10 | 请求重试 | 每次 HTTP | `retry_school` 3 次指数退避；区分 `HTTPError` 与传输层错误分别处理 | `dorm_power.py:140-300` | | |
| A11 | 抓取失败降级 | F1 失败时 | 从 `records` 表取最近一条的 JSON body 兜底，标记 `scrape_status="stale"`，推黄卡 | `dorm_power.py:957,2195` | ⚠ | |

---

## B. 推送通知（群机器人，12 项）

| # | 功能 | 触发条件 | 当前行为（含参数） | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|---|
| B1 | **L1 低电告警** | `remain < 30` **且是"非低→低"跃迁** | 红色卡「🚨 剩余电量过低」+ 剩余值 + 建议充值 | `dorm_power.py:1882` | ⚠📌 | |
| B2 | **L2 常规摘要卡** | 每次抓取成功 | 每次推；**整点（`minute==0`）**时标题加「🔔 ⏰ 整点播报」且 header 变 `green` | `dorm_power.py:2314-2367` | ⚠ | |
| B3 | **L3 日报** | BJ 09:00（`meta.push_daily_time` 可改）+ `push_daily_enable=1` | 长文本日报，戳 `YYYY-MM-DD` | `dorm_power.py:1173,1239` | ⚠ | |
| B4 | **L3 周报** | BJ **周一** 09:00 + `push_weekly_enable=1` | 长文本周报，戳 ISO `YYYY-Www` | `dorm_power.py:1200` | ⚠ | |
| B5 | **L3 月报** | BJ **1 号** 09:00 + `push_monthly_enable=1` | 长文本月报，戳 `YYYY-MM` | `dorm_power.py:1210` | ⚠ | |
| B6 | **L4 离线卡** | `runStatus ∉ {在线,正常,通讯正常}` **或字段缺失** | 红卡「⚠ 电表离线」；**缺行也算离线**（防止静默失败） | `dorm_power.py:1869` | ⚠📌 | |
| B7 | **stale 卡** | 上次成功抓取距今 **> 2h** 且本段未告警过 | 橙卡「⚠ 抓取器长时间无响应」+ 已过去分钟数 | `dorm_power.py:2057` | ⚠ | |
| B8 | **违规告警卡** | F3 拉到新违规 + **30 分钟冷却** | 红卡列最近 8 条违规（`dt / reason / power`） | `dorm_power.py:1957` | ⚠ | |
| B9 | **静默时段** | 落在 `quiet_hours_start`–`quiet_hours_end` 内 | 抑制推送 | `feishu_bot.py:2047` | ⚠ | |
| B10 | 卡片颜色分级 | 每次卡片 | `remain<30`→red / `<80`→orange / `<200`→blue / `≥200`→green | `dorm_power.py:299-301,1574` | ⚠ | |
| B11 | 卡片版式 | 每次卡片 | 2 列布局（免费/充值、已用/水费）+ 单价行 + 抄表时间 footer + roomId **尾 4 位** | `dorm_power.py:1592-1778` | | |
| B12 | L3/L2 去重 | L3 触发的那一分钟 | 抑制 L2 的整点前缀，**防止同分钟收到两张卡** | `dorm_power.py:2329,2353` | ⚠📌 | |

---

## C. 飞书私聊机器人（10 项）

| # | 功能 | 入口 | 当前行为 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|---|
| C1 | **9 个 slash 命令** | 私聊/群内 @ | `/状态` `/剩余` `/电表` `/电表状态` `/今日` `/历史` `/缴费` `/违规` `/帮助` | `feishu_bot.py:110,1229-1338` | | |
| C2 | `/历史 [N]` 参数 | 命令带参 | 解析小时数，默认 24 | `feishu_bot.py:1212` | | |
| C3 | 自定义菜单事件 | 菜单点击 | 7 个 `menu_key` → 同命令处理器 | `feishu_bot.py:123,1585` | ⚠ | |
| C4 | **Pillow PNG 卡片** | 命令回复 | 800×600 深色 stat card，8 种模板（remain/meter/today/history/pay/violations/stat） | `feishu_bot.py:538-795` | | |
| C5 | 中文 + emoji 混排渲染 | PNG 渲染 | MiSans（中文）+ NotoEmoji；**按 emoji 边界分段绘制**（MiSans 无 emoji 字形） | `feishu_bot.py:205,390-524` | ⚠📌 | |
| C6 | 图片上传 + 缓存 | 每次图片回复 | POST `im/v1/images`，`sha256(png)[:16] → image_key` 缓存进 `meta`，避免重复上传 | `feishu_bot.py:829-968` | ⚠ | |
| C7 | tenant_access_token 缓存 | 每次 API 调用 | 内存缓存 token + 过期时间 | `feishu_bot.py:1097` | ⚠ | |
| C8 | 卡片 img 元素字段名 | 每次图片卡 | 必须用 `img_key`（**不是** `image_key`），否则飞书静默丢弃 | `feishu_bot.py:973` | 📌 | |
| C9 | 群内 @ 剥离 | 群消息 | 去掉 @机器人 的前缀再解析命令 | `feishu_bot.py:1199` | | |
| C10 | 无凭据降级 | 缺 App ID/Secret | 返回中文提示，**不抛异常**（保证 dashboard 不被拖垮） | `feishu_bot.py:1186-1210` | 🔒 | |

---

## D. 飞书事件安全（5 项）

| # | 功能 | 说明 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|
| D1 | url_verification 探活 | 回显 challenge；POST 与 GET 两个路由都支持 | `web.py:963,1037` | 🔒 | |
| D2 | **v1 加密事件解密** | AES-256-CBC；**3 条路径**：官方 spec（SHA256→AES）+ 2 条 IV=前 16 字节 fallback | `feishu_bot.py:1624-1724` | 🔒📌 | |
| D3 | **v2 签名校验** | HMAC-SHA256 over `timestamp + nonce + body` | `feishu_bot.py:1800-1867` | 🔒 | |
| D4 | verification token 校验 | 事件体内 token 比对 | `feishu_bot.py:1739` | 🔒 | |
| D5 | 无凭据 fail-open | 开发环境跳过校验（生产需注意） | `feishu_bot.py:1739` | 🔒⚠ | |

---

## E. Web 仪表盘（18 项）

| # | 功能 | 入口 | 当前行为 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|---|
| E1 | 4 段式导航 | 顶部 tab | 概览 / 历史 / 违规 / 电表；当前段存 `localStorage["dorm-power-monitor.section"]` | `dashboard.html:21-26`、`dashboard.js` | | |
| E2 | 概览 hero | `/` | 剩余电量大字 + 连接状态点 + 抄表时间 + 最后更新时间 | `dashboard.html:41-51` | | |
| E3 | 剩余电量趋势图 | 概览 | Chart.js 折线，4 档范围 `24h / 3d / 7d / 30d` | `dashboard.html:53-62` | | |
| E4 | **4 张 stat card** | 概览 | ①本月费用 ②1 小时耗电（差值，≥3500s）③日均（窗口内）④抄表时间 | `dashboard.html:64-95` | | |
| E5 | 本月预计电费算法 | 概览 | **月内累计表码 `last-first`** × 单价 + `日均 × 剩余天数` × 单价；负数 delta（换表）走降级 | `web.py:366-487` | ⚠📌 | |
| E6 | 历史段：每日用电曲线 | 历史 | Chart.js 折线，近 30 天有效用电 | `dashboard.html:110-119` | | |
| E7 | 历史段：采集记录表 | 历史 | records 表，倒序 | `dashboard.html:137-165` | | |
| E8 | **记录时间区间筛选** | 历史 | 起始/截止 `datetime-local` + 查询/重置；走 `/api/data?start=&end=` | `dashboard.html:127-135` | ⚠ | |
| E9 | 违规记录列表 | 违规 | `wg_reason` + `dt` + tag；**`wg_power > 1000` 显示红色标签** | `dashboard.html:185-201` | 📌 | |
| E10 | 缴费记录列表 | 违规 | `pay_type/fee_type` + `dt` + 金额；**含"罚"字显示红色** | `dashboard.html:209-229` | | |
| E11 | 电表面板 | 电表 | 电压(V) / 电流(A) / 有功功率(W) / 电表状态 / 最后上报时间 | `dashboard.html:249-282` | | |
| E12 | 30 秒轮询 | 全站 | `setInterval` 拉 `/api/live`，失败累积计数 | `dashboard.js` | ⚠ | |
| E13 | 手动刷新 | 顶部按钮 | `POST /api/refresh` → 服务端触发抓取（**不推送飞书**） | `web.py:802` | | |
| E14 | 主题切换 | 顶部按钮 | 深/浅色切换 + `localStorage` | `dashboard.js` | | |
| E15 | 键盘快捷键 | 全站 | `1`–`4` 切段、`R` 刷新 | `dashboard.html:294` | ⚠ | |
| E16 | 状态栏 | 底部 | 连接状态 + 最近采集时间 + 快捷键提示 + 版本号 `v51b` | `dashboard.html:290-296` | | |
| E17 | toast 通知 | 全站 | `<dorm-toast>` 自动消失 + ARIA | `components/dorm-toast.js` | | |
| E18 | **离线判定** | 状态点 | `ts` 距今 **< 7200s（2h）** 视为在线；`ts` 是朴素 CST，JS 补 `+08:00` 解析 | `dashboard.js:53-80` | ⚠ | |

**⚠ 已发现的一致性缺陷**（重写时需决定以哪个为准）

| 项 | 仪表盘显示 | 飞书机器人显示 | 代码 |
|---|---|---|---|
| 违规功率单位 | **W**（`'%.2f' % v.wg_power ~ ' W'`） | **kW**（`f"{wg_power:.2f} kW"`） | `dashboard.html:190` vs `feishu_bot.py:1330`、`dorm_power.py:2054` |
| 违规红色阈值 | `wg_power > 1000` | 无阈值（全部展示） | `dashboard.html:189` |

---

## F. Admin 后台（8 项）

| # | 功能 | 入口 | 当前行为 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|---|
| F1 | Admin 落地 | `/admin` | 302 → `/admin/config` | `web.py:2143` | | |
| F2 | 用户管理页 | `/admin/users` | 列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除 | `web.py:2533-2754` | | |
| F3 | 配置页 | `/admin/config` | 抓取配置（openid / base_url / roomId / eqprice）+ 推送配置（L1/L2/L3 enable + 时间 + 接收人 + 静默时段） | `web.py:2765`、`admin_config.html` | | |
| F4 | 测试页 | `/admin/test` | 手动触发抓取 + 手动推测试卡（`raise_on_error` 显式报错） | `web.py:2814-2917` | | |
| F5 | 审计页 | `/admin/audit` | 审计日志列表 | `web.py:2921-3012` | | |
| F6 | **密码恢复入口** | `/admin/set-password` | **无认证**；仅当 `meta.admin_password` 为空时可用，否则 302 | `web.py:2292` | 🔒⚠ | |
| F7 | **URL 导入配置** | `POST /admin/api/import-url` | 从学校 URL 自动解析 roomId / openid / eqprice（**SSRF 4 层防护**） | `web.py:2403,1258` | 🔒 | |
| F8 | 配置读写 API | `GET/PUT /api/admin/config` | 逐键审计写入 | `web.py:3020,3060` | 🔒 | |

---

## G. 认证与安全（12 项）

| # | 功能 | 当前行为（含参数） | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|
| G1 | 密码散列 | bcrypt **rounds=12**（约 250ms/次） | `auth.py:292` | 🔒 | |
| G2 | 服务端 session | cookie 名 `dorm_session`；HttpOnly + SameSite=Lax + **Secure（默认开，`DORM_COOKIE_SECURE=0` 可关）**；默认 **24h** 过期 | `auth.py:217,224`、`web.py:166-172` | 🔒⚠ | |
| G3 | **失败锁定** | **5 次失败 / 15 分钟** 内锁定 15 分钟（按 username + IP） | `auth.py:502-556` | 🔒 | |
| G4 | 审计日志 | 登录/登出/改密/用户增删改/角色变更/OOBE 创建管理员；含 IP + User-Agent + details | `auth.py:560-591` | 🔒 | |
| G5 | **CSRF 防护** | `token_urlsafe(32)` 存 `meta` 表 + cookie `dorm_csrf` + `X-CSRF-Token` 头；**所有 admin POST/PUT/DELETE 都校验** | `auth.py:988-1140`、`web.py:187-208` | 🔒⚠ | |
| G6 | 角色模型 | `admin` / `viewer` 两角色 | `auth.py:212-214` | 🔒 | |
| G7 | 内部 token 守卫 | `/api/refresh` 校验 `meta.api_internal_token`；**为空则跳过**（个人 dashboard 默认关） | `web.py:1168` | 🔒⚠ | |
| G8 | **SSRF 4 层防护** | ①host 白名单 ②IP 字面量拒绝 ③scheme 白名单(http/https) ④`allow_redirects=False` | `web.py:1258` | 🔒📌 | |
| G9 | 密码强度校验 | 长度 + 字符类别要求 | `web.py:1456` | 🔒 | |
| G10 | Webhook URL 校验 | host 白名单 + 拒 IP 字面量 + 不跟随重定向 | `web.py:1491-1520` | 🔒 | |
| G11 | cron 表达式校验 | admin 可改抓取间隔时校验格式 | `web.py:1521` | 🔒 | |
| G12 | FLASK_SECRET_KEY 自举 | 缺失时自动生成并**写回 `.env`**（保证重启后签名 cookie 仍有效） | `auth.py:831-915` | 🔒⚠ | |

---

## H. OOBE 首启向导（7 项）

| # | 功能 | 当前行为 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|
| H1 | 6 步向导 | `/oobe`：①欢迎 ②管理员用户名 ③密码+强度计 ④主题色 ⑤健康检查 ⑥完成 | `web.py:1614`、`oobe.html`、`oobe.js` | | |
| H2 | 未完成时强制跳转 | `meta.oobe_completed != "1"` 时 `/` 与 `/admin/*` 都跳 `/oobe` | `web.py:685-690` | ⚠ | |
| H3 | 双状态源 | `meta.oobe_step` + `meta.oobe_completed`（持久）+ `session["oobe_complete"]`（本会话） | `web.py:1531-1560,685` | ⚠ | |
| H4 | **双后端并存** | 新 `/api/oobe/*`（9 端点）+ 旧 `/admin/api/oobe/save`（1 端点）逻辑重复 | `web.py:1641-2140` | 📌 | |
| H5 | step 2 自动解析 | 从学校 URL 解析 `openid` / `roomId` / `eqprice` 并写入 meta；解析失败返回 400 | `web.py:2009-2037` | | |
| H6 | 创建管理员并自动登录 | OOBE 完成时 `auth.create_user()` + `create_session()` + 审计 | `web.py:2080-2135` | | |
| H7 | 完成后不可再进入 | `oobe_completed=1` 后向导不可访问（除非手改 meta） | `web.py:1561-1585` | ⚠ | |

---

## I. 健康检查（3 项）

| # | 功能 | 当前行为 | 代码位置 | 标记 | 决策 |
|---|---|---|---|---|---|
| I1 | 三段探测 | `db`（读 `last_scrape_status` + 算 `last_scrape_age_sec`）/ `school_api`（GET finduser，5s 超时）/ `feishu_token`（可选，5s 超时） | `web.py:864-956` | | |
| I2 | 200 判定 | `db_ok AND school_api_ok`；`feishu_token_ok` 不参与判定 | `web.py:955` | ⚠ | |
| I3 | 无凭据降级 | web 进程没有 `DORM_OPENID` 时跳过学校探测并标记 `school_api_skipped` | `web.py:913-917` | ⚠ | |

---

## J. 运维与部署（7 项）

| # | 功能 | 当前行为 | 文件 | 标记 | 决策 |
|---|---|---|---|---|---|
| J1 | **双 cron** | `*/10 * * * *` 抓取（`dorm-scrape.lock`）+ `* * * * *` tick（`dorm-tick.lock`）；两把 `flock -n` 不同锁，可并行 | `deploy/dorm-cron.txt` | ⚠📌 | |
| J2 | systemd unit | 15 项 hardening（`ProtectSystem=full` / `PrivateTmp` / `NoNewPrivileges` / `ReadWritePaths` …） | `deploy/dorm-web.service` | | |
| J3 | nginx 反代 | 127.0.0.1:5000 + gzip + 1MB 上限 | `nginx/dorm.conf` | | |
| J4 | 日志轮转 | logrotate 每日 + 保留 7 天 + `copytruncate` | `deploy/dorm-power-monitor.logrotate` | | |
| J5 | 一键安装 | `setup.sh` 建 venv + pip install + 生成 systemd unit | `setup.sh` | | |
| J6 | 构建/部署脚本 | 11 个构建脚本（r35–r68）+ 9 个部署脚本（r41–r68） | `scripts/` | 📌 | |
| J7 | CI | `ast.parse` + `py_compile` + secret scan（**不跑测试**） | `.github/workflows/ci.yml` | ⚠ | |

---

## K. 非功能约定（12 项，全部为隐性）

| # | 约定 | 说明 | 重写影响 | 标记 | 决策 |
|---|---|---|---|---|---|
| K1 | **全栈朴素 CST 时间** | 所有时间戳存 `YYYY-MM-DD HH:MM:SS` 无时区；跨表字符串可直接比较（R47 契约） | 改 UTC 会破坏所有历史数据比较 | ⚠📌 | |
| K2 | **openid 全程脱敏日志** | `_safe_path()` 去 query；`_safe_room_tail()` 只留尾 4 位 | 泄露密钥 | ⚠🔒 | |
| K3 | 卡片不发原始 HTML | 只用 `plain_text` / `lark_md` | 注入风险 | ⚠🔒 | |
| K4 | SQLite WAL 模式 | `init()` 显式设置 | 并发性能 | ⚠ | |
| K5 | **`records.ts` UNIQUE + `INSERT OR REPLACE`** | 10 分钟内重复抓取**覆盖**而非堆叠（R49） | 重复行污染图表 | ⚠📌 | |
| K6 | `init()` 幂等 | `CREATE TABLE IF NOT EXISTS` + `DROP COLUMN raw_html` 安全 no-op | 升级失败 | ⚠ | |
| K7 | **单点失败不阻断 cron** | F2/F3/F4/F5/backfill/各告警各自 `try/except`，任一失败只 warning | 一处异常导致整轮抓取失败 | ⚠📌 | |
| K8 | meta 表 = 状态中心 | 33 个键承担去重 / 节流 / 状态 / 缓存（含 image_key 缓存） | 重写若换存储需迁移 | ⚠ | |
| K9 | 零硬编码密钥 | 全部走 `.env`（`.gitignore` 已排除） | 安全 | ⚠🔒 | |
| K10 | 前端时区补齐 | JS 解析 `ts` 时补 `+08:00` | 时间显示错 8 小时 | ⚠ | |
| K11 | Chart.js 公网 CDN | `cdn.jsdelivr.net/npm/chart.js@4.4.1` | 内网/离线不可用 | ⚠ | |
| K12 | 上游依赖宽松 | `requirements.txt` 全部 `>=`，无锁文件 | 构建不可重现 | ⚠ | |

---

## L. 已识别的现存缺陷（供"改"时参考）

| # | 缺陷 | 证据 | 严重度 |
|---|---|---|---|
| L1 | **R61–R67 成果未提交** | `git status` 24 项未跟踪/未提交；HEAD 仍是 R51c 形态 | 🔴 |
| L2 | **R62 模板拆分只做一半** | 模板被 `_load_template()` 读成字符串 + `render_template_string()`；`url_for` 仅 2 处、硬编码 `/static/` 40 处 | 🟠 |
| L3 | 数据层三份真相 | `db/_legacy.py`（"禁止修改"）+ `models.py` + `repo.py`（后两者生产零调用） | 🟠 |
| L4 | 飞书推送双实现 | `dorm_power._post_feishu` + `feishu_bot._post_feishu` | 🟠 |
| L5 | 跨模块调私有函数 | `web.py:2238,2882` → `from dorm_power import _post_feishu` | 🟠 |
| L6 | OOBE 双后端重复 | 9 个新端点 + 1 个 legacy 端点做同一件事 | 🟠 |
| L7 | **违规功率单位不一致** | 仪表盘 `W` vs 飞书 `kW` | 🟡 |
| L8 | 违规红色阈值不一致 | 仪表盘 `>1000` vs 飞书无阈值 | 🟡 |
| L9 | 重复工具函数 | `_read_eqprice` ×2、`_coerce_float` ×2 | 🟡 |
| L10 | CSS 设计系统分裂 | 4 份 `:root`、两套色板（`#0a0a0b` / `#0f172a`） | 🟡 |
| L11 | 测试无法回归 | 41 个 round 命名文件、纯 AST 断言、**硬编码绝对路径**、CI 不跑 | 🟠 |
| L12 | 无 lint / 无类型检查 / 无依赖锁 | 无 `pyproject.toml` | 🟡 |
| L13 | 项目内遗留临时文件 | `_tmp_*`、`diff_tests.txt`、`preview_v2.html`、`records.db` | 🟢 |
| L14 | 文档脱节 | `TECHNICAL.md` 停在 R42；README 说"5 个生产文件" | 🟡 |
| L15 | **6 个 secrets 曾泄露到 chat buffer** | `docs/TECHNICAL.md:188`（TODO #23） | 🔴 |
| L16 | **OOBE 步骤文档与代码不符** | `R68_RELEASE_NOTES.md` 说 step2=用户名/3=密码/4=主题色/5=健康检查；实际代码 `web.py:1415-1428` 是 step2=**飞书App凭据**/3=**Webhook**/4=**cron+时区**/5=**数据保留**/6=**管理员账号** | 🟡 |
| L17 | **session token 明文入库** | `auth.py:411-425`：token **原样**写入 `sessions.token`；`R68_RELEASE_NOTES.md` 声称"server-side session row keyed by the token **SHA-256**" —— 与代码不符 | 🟠🔒 |
| L18 | **`users` 表无 `disabled` 字段** | 实际 schema `db/_legacy.py:239-246` 只有 `id/username/password_hash/role/created_at/last_login_at`；`R68_RELEASE_NOTES.md` 声称有 "disabled flag" —— 与代码不符 | 🟡 |
| L19 | **session 是滑动过期** | `auth.py:428-438`：每次校验都 `_shifted_expires()` 前推 24h，**实际可无限续期**（R68 文档未提及此行为） | 🟡 |
| **L20** | 🔴 **`meta.admin_password` 明文密码仍在活跃使用** | `web.py:1240` 读它做认证（`_admin_required` 装饰器）；`web.py:2288/2396/3086` **三处明文写入**。R68 文档声称"fully removed" —— **与代码不符** | 🔴🔒 |
| **L21** | 🔴 **整个「推送开关体系」是死代码** | 见下方详述 —— Admin UI 的推送开关与静默时段**对行为完全无影响** | 🔴 |

### L21 详述：推送开关体系是死代码

**证据链（零调用证明）**：

| 位置 | 事实 |
|---|---|
| `feishu_bot.py:2107` | `def push_if_enabled(layer, card)` —— **零调用点** |
| `feishu_bot.py:2113` | `_should_push(layer)` **只**被 `push_if_enabled` 调用 |
| `feishu_bot.py:2116` | `_in_quiet_hours()` **只**被 `push_if_enabled` 调用 |
| `web.py:2199/2205/2206` | `push_l1_enable` / `push_l2_enable` 只被 **写入**与**返回**，**从无读取判断** |
| `push_receivers_l1/l2/report/alert` | 4 个常量**零调用** |

**后果**：

| 开关 | 实际有效性 |
|---|---|
| `push_l1_enable` | ❌ **死** |
| `push_l2_enable` | ❌ **死** |
| `quiet_hours_start` / `quiet_hours_end` | ❌ **死**（静默时段不生效） |
| `push_receivers_*`（4 个） | ❌ **死** |
| `push_daily/weekly/monthly_enable` | ✅ 有效（`_l3_due` 直接读 meta） |
| `push_daily/weekly/monthly_time` | ✅ 有效（`_read_push_time`） |

**五条推送路径全部绕过开关**：L1 低电 / L2 摘要 / 违规 / 离线 / stale
→ 都是直接 `_post_feishu(card)`，**没有** `push_if_enabled` 检查。

**对重写的影响**：`Q16（功能开关）` 不是"把现有开关搬到 UI"，而是
**从零实现真正的开关体系**。见 `REWRITE_PLAN.md §2.9`。

**快照固化**：`tests/regression/fixtures/behaviors.json` 的
`_findings_dead_push_switches` 已记录完整证据链。

---

## M. 怎么用这份清单

### 第一步：逐项标决策

在每张表的「决策」列填一个值：

| 值 | 含义 |
|---|---|
| `保留` | 重写后行为完全一致 |
| `改` | 保留但行为/参数/交互要变（请在备注写怎么改） |
| `删` | 重写后不要了 |
| `?` | 还没想好，需要讨论 |

### 第二步：重点看这三类

| 优先看 | 原因 |
|---|---|
| **带 ⚠ 的 45 项** | 界面上看不见。如果你忘了它们的存在，重写后就会"莫名少功能" |
| **带 📌 的 19 项** | 都是踩过坑换来的结论。删掉代码容易，重新踩一遍坑很贵 |
| **L 章的 15 个缺陷** | 这些是"改"的候选。想清楚哪些要借重写机会修掉 |

### 第三步：回答 3 个范围问题

| 问题 | 为什么关键 |
|---|---|
| **Q1** 45 项隐性行为，是全保留吗？ | 决定重写是"等价移植"还是"重新设计" |
| **Q2** 现在是**单人自用**还是以后会**多人/多宿舍**？ | 决定数据模型要不要加 `room_id` 维度、要不要多租户 |
| **Q3** L 章的 15 个缺陷，哪些要借重写修掉？ | 决定"改"清单的规模 |

---

## N. 下一步（对话路线）

```
[已完成] Q1 驱动力 → 技术栈重写
[已完成] 功能现状清单（本文档）
[当前]   ← 你审阅本文档，标决策
[下一步] Q2 用户与规模（单人 / 多人 / 多宿舍 / 多学校）
[下一步] Q3 技术栈选型（后端 / 前端 / 数据 / 部署）
[下一步] Q4 工程约束（自托管 / 依赖上限 / 测试要求 / 开源）
[下一步] Q5 交付节奏（并行开发 vs 停服重写 / 数据迁移）
[产出]   《需求确认书》
[之后]   重写方案（架构 + 里程碑 + 验收）
```

---

## O. 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-10-06 | draft-1 | 初稿：105 项功能盘点（11 域）+ 15 个现存缺陷 + 3 个范围问题 |

---

**清单结束** —— 本文档不含任何代码改动。




