# 扩展指南（Q18）

> 设计目标：**加东西只改 1–2 个文件**，不必先读懂整个系统。
> 这份文档是「分步 SOP」——每一步都对应仓库里真实存在的文件与函数。

## 0. 六类扩展速查

| 想做什么 | 改哪里 | 文件数 | 验证方式 |
|---|---|---|---|
| [加一个配置项](#1-加一个配置项1-个文件) | `config_registry/registry.py` | **1** | `pytest tests/unit/test_config_api.py`；配置中心刷新即见 |
| [加一个通知渠道](#2-加一个通知渠道微信邮件2-个文件) | `notify/` 新模块 + `notify/channels.py` | **2** | 连通测试页点一次；`pytest tests/unit/test_notify_channels.py` |
| [适配其他学校](#3-适配其他学校2-个文件) | `scraper/endpoints.py`（+ 注册表 base_url） | **2** | 配置中心「测试连通」；`pytest tests/unit/test_service.py` |
| [加一个告警层](#4-加一个告警层3-个文件) | `flags.py` + `policies.py` + `registry.py` | **3** | 关掉开关看是否静默；`pytest tests/unit/test_flags_degrade.py` |
| [加一个机器人命令](#5-加一个机器人命令2-个文件) | `notify/commands.py` + 契约测试的字面量表 | **2** | 飞书私聊 / QQ 发一次；`pytest tests/unit/test_notify_commands.py` |
| [加一个前端页面](#6-加一个前端页面2-个文件) | `frontend/src/views/` + `router/index.ts` | **2** | `npm run build` 后访问新路径 |

**改完之后必跑**（CI 跑的就是这些）：

```bash
python -m pytest -q              # 全量测试
python -m ruff check .           # 静态检查
python -m scripts.ast_guard      # 分层守卫（R1/R4/R5/R7/R8）
cd frontend && npm run build     # 前端类型检查 + 构建
```

---

## 1. 加一个配置项（1 个文件）

**只改** `starwatt/config_registry/registry.py`，在对应分组的元组里加一行：

```python
cfg("my_option", "我的选项", SCRAPE, T.INT, 10,
    help="说明文字会显示在配置中心的输入框下方",
    validate="1..100"),
```

**自动获得的能力**（都不需要再改别的文件）：

* 配置中心里出现对应控件（`type` → 控件见下表）
* `GET/PUT /api/admin/config` 的读写与**逐键校验**（`validate` 规则）
* 导出 / 导入（自动纳入 `CONFIG`；`STATE`/`CACHE` 不会被导出）
* 审计留痕、默认值落库（`ensure_defaults`）

| `type` | 控件 | `validate` 例子 |
|---|---|---|
| `T.BOOL` | 开关 | `none` |
| `T.INT` / `T.FLOAT` | 数字框 | `1..100` |
| `T.STR` | 文本框 | `len:1..32` |
| `T.URL` | URL 框 | `url` |
| `T.SECRET` | 密码框（**加密存储 + 脱敏显示**） | `len:0..128` |
| `T.ENUM` | 下拉（配 `choices=(...)`） | `enum:a\|b` |
| `T.JSON` | 多行 JSON | `json` |
| `T.TIME` | 时间选择器 | `none` |
| `T.DURATION` | 数字框（单位：**秒**） | `60..86400` |

想让它出现在 **OOBE 向导**里：在 `starwatt/services/oobe_service.py` 的
`STEPS` 里把键加进对应步骤的 `keys=`（可写项）或 `required=`（必填项）。

> ⚠️ 敏感值请用 `T.SECRET`，不要用 `T.STR` —— 只有 SECRET 会加密存储并在
> `GET` 时脱敏（`••••1234`）。用 STR 存凭据等于把它明文写进数据库。

---

## 2. 加一个通知渠道（微信/邮件…，2 个文件）

**第一步**：新建 `starwatt/notify/wechat.py`，实现
`starwatt/protocols.py` 里的 `Notifier` 协议：

```python
class WeChatNotifier:
    name = "wechat"

    def enabled(self) -> bool:
        """读缓存、**不发网络请求**：未配凭据或开关关闭 → False。"""
        return bool(get_str("wechat_token", ""))

    def send_card(self, card: dict) -> None:
        """失败只记日志，**不得向上抛**（单点失败不影响抓取主流程）。"""

    def send_text(self, text: str) -> None: ...
```

**第二步**：在 `starwatt/notify/channels.py` 的 `CHANNELS` 里注册一行：

```python
CHANNELS: dict[str, Callable[[], Notifier]] = {
    transport.WebhookNotifier.name: transport.webhook_notifier,
    qq.QQNotifier.name: qq.qq_notifier,
    wechat.WeChatNotifier.name: wechat.wechat_notifier,   # ← 新增
}
```

**凭据**：在 `registry.py` 里加 `T.SECRET` 项（见第 1 节）。

**立即生效的位置**：`policies._send()` 的扇出 —— 飞书之外的所有告警都会自动
发一份到这个渠道（`broadcast_card` 会跳过 `enabled() == False` 的渠道）。
渠道之间互相隔离：微信挂了不影响飞书。

---

## 3. 适配其他学校（2 个文件）

**同厂商系统**（易班 / 完美校园等，路径结构一致）：只改配置
——「配置中心 → 数据采集 → 学校接口地址」+ 新的 openid，**不用改代码**。

**完全不同的系统**：在 `starwatt/scraper/endpoints.py` 里加一个端点：

```python
class F6MyThing(_Endpoint):
    key = "F6"                    # 唯一；F1 是核心数据（决定本次抓取 ok 与否）
    interval_sec = 3600           # None = 每次抓取；否则按节流间隔
    path = "/campus/webchat/xxx"  # 相对 base_url

    def parse(self, payload, room_id: str) -> list[dict[str, Any]]:
        """把上游 JSON 变成**纯 dict 列表**；字段名用 snake_case。"""
        return [...]

    def persist(self, rows: list[dict[str, Any]]) -> None:
        """写库（用 starwatt/db/repositories.py 里的仓储）。"""
```

然后把实例加进 `ENDPOINTS` 元组。

**规则**（AST 守卫与测试会拦）：

* `parse` **不要抛异常**：上游字段缺失时返回空列表，由上层记进 `errors`
* `persist` 只走仓储，不要在端点里拼 SQL
* 需要新表/新字段 → `db/models.py` + `db/schema.py` + `db/migrations.py`
  （**只增不删**，老库升级时才不会炸）
* 纯计算放进 `starwatt/domain/`（那里禁止 IO，便于单测）

---

## 4. 加一个告警层（3 个文件）

以「用水超标」为例：

1. **开关** `starwatt/flags.py`：加声明 + 映射
   ```python
   _flag("push_water_enable", "用水超标", GROUP_MASTER),
   LAYER_FLAGS = {..., "water": "push_water_enable"}
   ```
2. **阈值** `starwatt/config_registry/registry.py`：
   `cfg("water_limit", "用水上限", THRESHOLD, T.FLOAT, 5.0, validate="0..100000")`
3. **判定 + 发送** `starwatt/notify/policies.py`：
   写 `push_water_if_due()`，内部调 `_send("water", card)`；
   需要新卡片时在 `notify/card_builder.py` 加一个 `build_*_card()`

**三条纪律**：

* 所有发送必须经 `_send()`（它内部先过 `flags.should_push(layer)`）——
  绕过它 = 开关失效（legacy 的 L21 就是这么来的）
* **打点必须在发送成功之后**（失败的下个 tick 还能重试）
* 开关关闭时行为是**静默降级**：不发、记一条 NOTICE，**不报错**

> `tests/unit/test_config_registry.py::test_all_literal_flag_names_in_source_are_registered`
> 会扫描源码里出现的字面量开关名 —— 拼错或漏注册会直接红。

---

## 5. 加一个机器人命令（2 个文件）

**只改** `starwatt/notify/commands.py` 一个文件，但里面是**三处**改动：

```python
# ① 命令表（斜杠命令 → 处理器名）
COMMANDS = {..., "/天气": "weather"}

# ② 卡片标题 —— ⚠️ 必须加，不是可选项：
#    test_notify_commands.py::test_every_handler_has_a_title 要求
#    每个处理器（含菜单键）都有非空标题
TITLES = {..., "weather": "🌤 天气"}
```

③ 在 `_run()` 的 if 链里加分支：

```python
if handler == "weather":
    return cmd_weather()
```

**命令层是渠道无关的**：飞书私聊与 QQ 会同时支持它（两个渠道都调
`dispatch_text` / `dispatch_menu`）。

**想让菜单里也出现**（可选）：`MENU_KEYS["menu_weather"] = "weather"`，
再去飞书开放平台把按钮的 `event_key` 设成 `menu_weather`。

**想要一个独立开关**（第 2 个文件）：`starwatt/flags.py`

```python
_flag("cmd_weather_enabled", "命令 /天气", BOT_MASTER),
COMMAND_FLAGS = {..., "weather": "cmd_weather_enabled"}
```

关掉后回一句「该命令已禁用」并记 NOTICE（`tests/unit/test_flags_degrade.py`
会逐个命令验证这一点）。

> ⚠️ **实测发现（照 SOP 走一遍会遇到的第二处改动）**：
> `tests/unit/test_notify_commands.py::TestMaps::test_commands_match_legacy`
> 把 legacy 的斜杠命令集**写死在断言里**（防止无意改掉老命令）。
> 有意新增命令时，把新命令登记进那个字面量表即可 —— 于是实际改动是
> **1 个实现文件 + 1 个测试文件**。别去删这个测试：它挡的是「不小心让
> 老命令消失」。

---

## 6. 加一个前端页面（2 个文件）

1. `frontend/src/views/MyView.vue`
2. `frontend/src/router/index.ts` 加一条路由

管理后台的页面还要在 `frontend/src/views/admin/AdminShell.vue` 的 `TABS`
里加一行（否则导航里看不到）。

**要调接口**：在 `frontend/src/api/client.ts` 加方法、`api/types.ts` 加类型
—— `client.ts` 是全站唯一的 `fetch` 出口（自动带 Cookie 与 CSRF 头）。

**三条约定**：

* 字段名必须与后端一致；`/api/data`、`/api/live` 的字段由
  `tests/regression/fixtures/api.json` **冻结**，不要为了前端方便改后端字段名
* 配置类页面优先复用 `components/config/ConfigField.vue`（按 schema 渲染，
  新增配置项时前端**零改动**）
* 产物要重新 `npm run build`（`static/` 不进仓库）

---

## 附录 A：分层与守卫

`scripts/ast_guard.py` 会在 CI 里拦下这些写法：

| 规则 | 含义 |
|---|---|
| R1 | `starwatt/domain/**` 禁止 IO（`requests` / `flask` / `sqlite3` / `config_registry`） |
| R4 | `starwatt/**` 禁止回引旧顶层模块（`web` / `db` / `feishu_bot` / `dorm_power`） |
| R5 | 禁止跨模块 import 下划线私有符号 |
| R7 | 禁止裸用 `datetime.now()` / `date.today()`（统一走 `starwatt.timeutil.now_cst()`） |
| R8 | 禁止 UTF-8 BOM |

分层方向：`domain` → `config_registry`/`db`/`auth` → `scraper`/`notify` →
`services` → `web` → `web.py`。**只能向上依赖，不能反向。**

## 附录 B：契约快照

`tests/regression/fixtures/*.json` 是 **M0 在旧代码上真实运行**产出的行为基准
（卡片 JSON / API 字段集 / 算法向量 / 表结构 / 45 项隐性行为）。

**不要为了让测试变绿而改它们** —— 改 fixtures 等于改需求，请先确认那确实是
有意的行为变更。

## 附录 C：改完之后怎么验证

1. 重启服务（或等调度器下一轮）
2. 「管理 → 配置中心」找到该项 → 改 → 保存（失败项会带中文原因留下）
3. 「管理 → 日志」把对应类别的级别调成 `DEBUG`，观察行为
4. 带开关的项去「管理 → 功能开关」看**实时生效状态**与抑制原因
