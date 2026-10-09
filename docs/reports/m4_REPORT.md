# M4 报告 —— API 层 + 访问控制 + 配置 API

> **日期**：2026-10-08
> **分支**：`rewrite/r69`
> **里程碑**：M4（`REWRITE_PLAN.md §M4`，步骤 4.1–4.9）
> **tag**：未打（工作树按批次交给用户提交）
> **前置**：M2（scraper）✅ / M3（notify）✅

---

## 1. 产出总览

| 项 | 结果 |
|---|---|
| 业务端点 | **27** 个（另有 `/static/<path>` 与 JSON 404/405 处理器） |
| 蓝图 | **7** 个（`REWRITE_PLAN §1.2` 的七蓝图全部落地） |
| service | **5** 个（dashboard / admin / auth / oobe / setup） |
| 新增源码文件 | `config_registry/portable.py` + `services/*`（5）+ `web/blueprints/*`（7）+ `web/{factory,security}.py` |
| 测试 | **1323 全绿**（M4 两批共 +177：1146 → 1323） |
| `ruff check .` | **0 error** |
| `mypy starwatt/` | 9 error（**全部是 M1–M3 遗留**，M4 新增 0 —— 本批顺带修掉 13 个） |
| `scripts.ast_guard` | 通过（已实现的 R1 / R4 / R5 / R7 / R8 全部通过） |
| `scripts.secret_scan` | 通过 |
| `compileall` | exit 0 |
| 契约快照 | `tests/regression` 47 项全绿（`algorithms` / `api` / `cards` / `schema` / `behaviors` 逐项对齐） |

---

## 2. §4.1–4.9 逐项对照

| 步 | 计划内容 | 落地情况 |
|---|---|---|
| 4.1 | `services/` 四个 service | ✅ 实为 **5** 个：`dashboard` / `admin` / `auth` / `oobe` / `setup`（`auth` 是 Web 适配层，与 `auth.service` 分工见其 docstring） |
| 4.2 | `web/` 7 个蓝图 | ✅ `health` / `auth_api` / `dashboard_api` / `admin_api` / `oobe_api` / `setup_api` / `feishu` |
| 4.3 | 访问控制（Q11） | ✅ `read_access` 认 `public_readonly`（默认匿名读 401）；`admin_access` 对 viewer 403；写操作全部要求 CSRF |
| 4.4 | OOBE 收敛 5 步 + 端点 10 → 2 | ✅ `GET /api/oobe/state` + `POST /api/oobe/advance`；步骤 = 欢迎 / 学校接入 / 飞书群 / 飞书机器人 / 偏好+完成（**无建号步**）；表单校验直接来自配置注册表（消除 L6） |
| 4.5 | 自助配置 API（N4） | ✅ `/api/setup/parse-url` / `verify`（**不落库**）/ `commit`；SSRF 4 层防护 + host 白名单 |
| 4.6 | 配置 API 七端点（Q15） | ✅ schema / get / put / test / export / import / reset（PUT 逐键独立校验，部分成功回 **207**） |
| 4.7 | 开关接入（Q16） | ✅ 18 个开关全部真正生效；**新增 `tests/unit/test_flags_degrade.py`（73 项）逐个穷举验证** |
| 4.8 | bootstrap 建号 + 强制首登改密 | ✅ `BOOTSTRAP_ADMIN_PASSWORD` 建号 → 用后即焚 → `must_change_password=1` **拦截除 3 条白名单外的所有接口**（本批补齐，见 §4） |
| 4.9 | 彻底移除 Basic Auth | ✅ 源码树 `grep -r "WWW-Authenticate\|request.authorization\|_admin_required\|HTTPBasicAuth"` = **空**（仅 `docs/*.md` 的旧版现状描述里出现） |

---

## 3. 端点清单（27）

| 分组 | 端点 |
|---|---|
| 探活 | `GET /healthz` |
| 数据 | `GET /api/data`、`GET /api/live` |
| 认证 | `POST /api/auth/login`、`logout`、`password`、`GET /api/auth/me` |
| 配置（Q15） | `GET/PUT /api/admin/config`、`GET /api/admin/config/schema`、`POST /api/admin/config/{test,import,reset}`、`GET /api/admin/config/export` |
| 用户（F2） | `GET/POST /api/admin/users`、`PATCH/DELETE /api/admin/users/<id>`、`POST /api/admin/users/<id>/password` |
| 审计 | `GET /api/admin/audit` |
| OOBE | `GET /api/oobe/state`、`POST /api/oobe/advance` |
| 自助配置 | `POST /api/setup/{parse-url,verify,commit}` |
| 平台回调 | `POST /feishu/event`、`POST /qq/events` |

---

## 4. 本批修掉的 6 个真 bug / 缺口

> 全部由测试或逐项核对发现，**都不是「加功能」而是「修 bug」**。

### 4.1 `parse_url` 把 hostname 当成 URL 再解析（M4 第二批）

`assert_public_url()` 的返回值是 **hostname 字符串**，旧代码把它当 URL 又 `urlparse`
了一次 → `scheme` / `netloc` 全空 → 用户粘贴的地址永远解析不出 `base_url`。
修法：回到 `urlparse(raw)`；并把 host 白名单检查提到 DNS 之前（错误信息更准、少一次 DNS）。

### 4.2 7 个命令开关是**死代码**（缺陷 L21 复发）🔴

`flags.command_enabled()` 有**零个**生产调用点 —— 管理页能改 `cmd_*_enabled`，
但 `commands.dispatch_text` 从不看它，**改了没有任何影响**。
这正是 legacy L21 的形态。修法：在 `commands._run()`（文本与菜单的唯一汇合点）
加开关卡点，飞书私聊与 QQ 一次覆盖。

📌 这里必须用 `flags.own_value()` 而不是 `is_enabled()`：机器人总开关**默认关闭**，
用 `is_enabled` 会把命令层连带否决，且总开关语义被判两遍（渠道层已判过）。

### 4.3 `transport.post_card` 出口不判总开关

`flags.py` 的文档写着「总开关关闭 → `post_card()` 直接返回」，但代码里没有 ——
出口不设防意味着**将来任何新推送路径忘了 `should_push` 就会「关了还在发」**。
修法：在出口兜一道 `push_group_enabled`；并保留一个**显式例外**
（管理页「发送测试消息」传 `respect_flags=False`，否则关推送时按钮失效）。

### 4.4 `must_change_password` 只存不拦（§4.8 未落地）🔴

`users.must_change_password` 有列、有迁移、有 API 标记，但**没有任何地方拦截**——
bootstrap 临时密码可以无限期使用。修法：在 `require_auth` 加门禁，
除 3 条白名单外一律 `403 {"error": "must_change_password"}`：

| 白名单 | 为什么必须放行 |
|---|---|
| `POST /api/auth/password` | 唯一出口，改完即解除 |
| `POST /api/auth/logout` | 允许反悔 |
| `GET /api/auth/me` | 前端据此把用户引导到改密页 |

⚠️ **不能改成「拒绝登录」**：拒绝 = 没有会话 = 永远改不了密码，用户被彻底锁死。

### 4.5 F2 用户管理后端能力不可达

`auth_service` 有 `create_user` / `set_role` / `set_disabled` / `delete_user`，
但蓝图只有 `GET /api/admin/users` —— `SCOPE_DECISION.md` 要求 F2「后端能力保留」
（列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除），当时**只剩列表可用**。
修法：补齐 4 个端点 + 3 条护栏：

1. **不能禁用/删除自己**（当场把自己锁在门外）
2. **不能把最后一个管理员降级/禁用/删除**（service 层兜底，CLI 调用同样受保护）
3. 重置密码 → 目标用户被踢下线且**强制首登改密**（临时密码 ≠ 本人持有）

### 4.6 `stats()` 每行调用两次 `coerce_float`

推导式为了过滤得把 `coerce_float(row.remain)` 写两遍（顺带让 mypy 无法收窄类型，
产生 10 个 `float | None` 算术错误）。修法：抽成 `_valid_rows()` 显式循环，
一次转换 + 类型收窄 —— 一并把 M4 的 mypy 错误清零，且复杂度回到预算内。

---

## 5. 新增的安全不变量（都有测试钉住）

| 不变量 | 位置 | 测试 |
|---|---|---|
| 匿名读默认 401（`public_readonly` 两态） | `web/security.py` | `test_web_api.py::TestAccessControl` |
| viewer 访问 `/api/admin/*` → 403 | 同上 | `test_config_api.py::TestConfigApiAccess` |
| 所有写操作必须带 CSRF | `require_csrf` | `test_config_api.py`（配置 / 用户各一条） |
| secret 默认脱敏 `••••1234`，审计**绝不含** secret | `config_registry` / `admin_service` | `test_config_api.py::TestConfigValues::test_secrets_are_masked_by_default` |
| 导出→导入往返：**跳过脱敏值**、跳过 `STATE`/`CACHE`、未知键跳过 | `portable.py` | `test_config_api.py::TestPortable` |
| 强制改密只放行 **3 条**路径（多一条即缺口） | `auth/decorators.py` | `test_config_api.py::TestMustChangePasswordGate` |
| 开关关闭 = 静默降级 + NOTICE，**不打点** | `flags` / `policies` / `commands` | `test_flags_degrade.py`（73 项穷举） |
| 关闭 ≠ 免验签（fail-closed 不受开关影响） | `feishu` 蓝图 | `test_flags_degrade.py` |
| 用户管理 3 条护栏 | `admin.py` + `auth_service` | `test_config_api.py::TestUserManagement` |
| openid 不落审计、不回显 | `setup_service` | `test_config_api.py::TestSetupApi` |

---

## 6. 与计划的偏差（有意，非遗漏）

| 项 | 计划 | 实际 | 原因 |
|---|---|---|---|
| 配置分组数 | 8 分组 | **9 分组**（多「机器人（QQ）」） | M3 追加 QQ 官方机器人渠道（Q18 扩展点）；`test_config_api` 断言 `>= 8`，QQ 组单独测 |
| `feishu` 蓝图职责 | 「飞书事件订阅」 | 飞书 **+ QQ** 回调 | 两者结构完全相同（平台签名鉴权 / 秒级回 200 / fail-closed），合一个蓝图共用同一流程 |
| 删掉 `web/bots.py` | 计划里是「7 个蓝图」 | 蓝图注册统一，`bots.py` 已删除 | 保留两套注册路径会重复注册路由 |
| `mypy starwatt/` | 「无**新增**错误」 | 9 error（M1–M3 遗留：`logging_setup` 5 / `secrets` 2 / `ssrf` 1 / `renderer` 1 / `scraper.service` 1） | 都在安全敏感模块，**不为纯类型修饰改动密码学与脱敏代码**；列入 M5 前的技术债 |
| OOBE 与强制改密的先后 | 未明说 | **先改密，再 OOBE** | 白名单是「所有非改密接口」，OOBE 走 `admin_access` 自然被拦；5 步里**没有**改密步，所以不会自锁。M5 前端必须按此顺序路由 |

---

## 7. 遗留 / 移交 M5

1. **前端**（`static/`）：消费 `/api/data`、`/api/live`、`/api/admin/config/schema`；
   6 个认证界面复用 `AuthLayout`（Q21）；路由守卫读 `/api/auth/me` 的
   `must_change_password` 与 `csrf_token`。
2. **`must_change_password` 的 UI 顺序**：登录 → 改密页 → OOBE（见 §6 最后一行）。
3. **`setup` 蓝图的 host 白名单**：与已配置的 `dorm_base_url` **同源**（host 必须一致）；
   未配置或仍是出厂占位值时采纳粘贴 URL 的 host（首次配置的引导路径）。
   换学校域名时要先在「配置中心 → 站点 → 学校接口地址」里改 —— M5 需在 UI 上给出提示。
4. **9 个 mypy 遗留错误**（§6）。
5. `docs/REWRITE_PLAN.md §6` 的 DoD 勾选：契约快照、AST 守卫、ruff、pytest 已满足；
   `git status` 干净 / 打 tag 由用户在分批提交时处理。
