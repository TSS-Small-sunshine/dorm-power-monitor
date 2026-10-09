# 配置说明

## 一、两层配置模型（Q15）

配置被**故意**分成两层，因为「打开数据库之前就需要知道的东西」不能存在数据库里：

| 层 | 存在哪 | 内容 | 怎么改 |
|---|---|---|---|
| **启动配置** | `.env`（4 项） | `DB_PATH` / `DORM_DATA_DIR` / `FLASK_PORT` / `FLASK_SECRET_KEY` | 编辑 `.env` 后重启 |
| **业务配置** | 数据库 `meta` 表（**94 项**） | 抓取、阈值、推送、机器人、认证、日志、高级 | 网页「管理 → 配置中心」，**改完立刻生效** |

```bash
# .env 的完整样子（.env.example 有注释版）
DB_PATH=/var/lib/dorm-power-monitor/records.db
DORM_DATA_DIR=/var/lib/dorm-power-monitor
FLASK_PORT=5000
FLASK_SECRET_KEY=<64 位十六进制>
```

> **优先级**：进程环境变量 > `.env` 文件。所以 systemd 的
> `EnvironmentFile=`、Docker 的 `environment:` 都能覆盖 `.env` 里的值
> （这是故意的：容器里不该依赖容器内的 `.env`）。

## 二、配置中心：9 组 94 项

| 组 | 项数 | 管什么 |
|---|---|---|
| 站点 | 6 | 站点名称、外观、房间显示名 |
| 数据采集 | 20 | 学校接口地址、openid、房间号、电价、抓取间隔与超时重试、各接口节流 |
| 告警阈值 | 8 | 低电阈值、离线判定、陈旧判定、违规阈值等 |
| 推送（群） | 26 | 飞书群机器人 webhook、签名密钥、卡片开关与文案、静默时段 |
| 机器人（私聊） | 12 | 飞书自建应用（App ID/Secret/Verification Token/Encrypt Key） |
| 机器人（QQ） | 8 | QQ 官方机器人的 AppID 与密钥 |
| 认证 | 4 | 匿名只读访问、会话有效期（默认 24 小时）、失败锁定阈值（5 次）、锁定时长（15 分钟） |
| 日志 | 5 | 全局级别、模块级覆盖、格式（text/json）、文件日志与保留天数 |
| 高级 | 5 | 内部 API Token、调试开关等（**改动需谨慎**） |

字段类型一共 **10 种**，网页会自动渲染成对应控件：

| 类型 | 控件 | 说明 |
|---|---|---|
| `bool` | 开关 | |
| `int` / `float` | 数字框 | |
| `str` | 文本框 | |
| `url` | URL 框 | |
| `enum` | 下拉 | 选项由注册表给出 |
| `json` | 多行编辑器 | 如模块级日志覆盖 |
| `time` | 时间选择器 | |
| `duration` | 数字框（**秒**） | 抓取间隔等 |
| `secret` | 密码框 | **加密存储 + 脱敏显示** |

### 哪些是加密存储的（9 项）

`dorm_openid`、`feishu_webhook_url`、`feishu_secret`、`feishu_app_secret`、
`feishu_verification_token`、`feishu_encrypt_key`、`qq_app_secret`、
`qq_bot_secret`、`api_internal_token`

它们用 `FLASK_SECRET_KEY` 派生的密钥做 **AES 加密**后落库，
`GET /api/admin/config` 只会返回 `••••1234` 这样的掩码。
写值时明文会被登记进日志脱敏表 —— 即使误打到日志也只会看到 `***`。

> 🔴 所以：**`FLASK_SECRET_KEY` 变了 = 这 9 项全部解不开**。
> 备份时务必连密钥一起备份（见 [DEPLOY.md 的备份一节](DEPLOY.md#五备份与恢复)）。

## 三、改配置后什么时候生效

* **绝大多数项：立刻生效**。写入后配置缓存按 key 失效（没有 TTL 等待）。
* **抓取类参数**（间隔 / 节流 / 超时）：下一次调度 tick 生效，不用重启。
* **日志级别**：立即生效（日志子系统会重算有效级别）。
* **`.env` 里的 4 项**：必须重启进程（它们决定 DB 路径与监听端口）。

## 四、功能开关（18 个）

「管理 → 功能开关」把**运行时状态**直接摊开给你看，因为开关是分层的：

| 组 | 内容 |
|---|---|
| 总开关（3） | 群推送 / 私聊机器人 / QQ 机器人 |
| 告警层（8） | L1 低电、L2 常规摘要、L3 日报·周报·月报、L4 离线、违规、数据陈旧 |
| 命令（7） | `/状态` `/电表` `/今日` `/历史` `/缴费` `/违规` `/帮助` |

**父子关系**：告警层挂在「群推送总开关」下，命令挂在「私聊机器人总开关」下。
父关掉 → 子即使自身是开的也不会发（页面会显示**抑制原因**，例如
「父开关『群推送总开关』已关闭」）。

**静默时段**（默认 23:00–07:00）只作用于 **L1 / L2** ——
半夜低电提醒会被压住，但 L3 日报与 L4 离线照常（它们本身就不是打扰性的）。

> 开关改动会记进审计日志（谁、什么时候、从什么改成什么）。

## 五、日志（7 级 × 8 类）

**级别**：`TRACE` `DEBUG` `INFO` `NOTICE` `WARNING` `ERROR` `CRITICAL`
（比标准库多了 `TRACE` 与 `NOTICE`；`NOTICE` 用来记「开关关闭所以跳过了」这类
正常降级，不是错误。）

**类别**：`scrape` `push` `auth` `config` `scheduler` `db` `web` `notify`

「管理 → 日志」页面能做的事：

* 改全局级别
* 按类别覆盖（例如只把 `scrape` 调到 `DEBUG`，其余保持 `INFO`）
* 切换 `text` / `json` 格式（json 便于接入 Loki / ELK）
* 开文件日志并设保留天数（默认 7 天，自动轮转清理）
* 看**有效级别表**（全局 + 覆盖 → 实际生效值，一眼看出谁在生效）
* 看**已登记的敏感值数量**（脱敏表大小）

> 排查问题时的推荐动作：先把 `scrape` 调 `DEBUG`，复现一次，再调回 `INFO`。

## 六、导入 / 导出

```bash
# 导出（含除 STATE/CACHE 外的全部配置；secret 以密文形式导出）
curl -fsS -b cookie.txt http://127.0.0.1:5000/api/admin/config/export > config.json

# 导入（**合并**语义：只覆盖 JSON 里出现的键）
curl -fsS -b cookie.txt -X POST -H 'Content-Type: application/json' \
  --data @config.json http://127.0.0.1:5000/api/admin/config/import
```

适合：换服务器迁移、把配置同步给隔壁宿舍、改动前留一份快照。

## 七、直接改数据库可以吗

可以，但**不推荐**：注册表在启动时会把缺失的键补成默认值
（`ensure_defaults`），手工写的值如果格式不对，会在读取时被当成坏值
（记 WARNING 并回落到默认值）。改配置请走网页或 API —— 它们会做类型与范围校验。
