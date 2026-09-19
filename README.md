# 宿舍电量监控 (Dorm Power Monitor)

[![CI](https://github.com/TSS-Small-sunshine/dorm-power-monitor/actions/workflows/ci.yml/badge.svg)](https://github.com/TSS-Small-sunshine/dorm-power-monitor/actions/workflows/ci.yml)

> **Round 51c — baseline (2026-09-19)**  
> **Branch state**: `main` 已锁定 R51c baseline；R60+ 后续开发在 `develop` 分支推进  
> **Release**: [`v1.0.0-r51c-baseline`](https://github.com/TSS-Small-sunshine/dorm-power-monitor/releases/tag/v1.0.0-r51c-baseline)  
> **R51c reference SHA256**: `AD73D661D256E2FC1A7513ED1C9D031E30499E169CF1BFC40B9ED83941E5B86F`
>
> 福建省某高校宿舍电量实时监控 + Feishu 机器人推送 + 美观 Web 仪表盘
>
> 一个自托管、可二次开发的项目，专为不习惯"商业电费 SaaS"的中国高校师生设计。
>
> **Round 路线图**：R51c baseline → R60 (GitHub 初始化) → R61-R67 (develop branch)

---

## 📂 项目结构（R44 整理后）

```
dorm-power-monitor/
├── README.md  LICENSE              ← 入口
├── config.py  db.py  dorm_power.py  feishu_bot.py  web.py   ← 生产代码（5 个）
├── requirements.txt  setup.sh  .env.example  .gitignore
├── assets/  deploy/  nginx/        ← 资源 / 部署配置
├── docs/                            ← 文档
│   ├── TECHNICAL.md
│   ├── Round25_dorm_id_research.md
│   └── reports/  (r41_REPORT.md, r42_REPORT.md)
├── tests/
│   ├── modern/   ← test_round35-42.py（zip 内打包，6 个）
│   └── legacy/   ← test_round5-34d.py（仅仓库，不入 zip，23 个）
└── scripts/
    ├── build/   ← r35-r42_*.py（zip 构建脚本）
    └── deploy/  ← r41-r42_*.sh（部署脚本）
```

> **R44 变更**：零散测试文件归档到 `tests/{modern,legacy}/`，文档归档到 `docs/`，构建/部署脚本归档到 `scripts/{build,deploy}/`。生产代码、README、LICENSE、配置/资源目录位置不动。`docs/TECHNICAL.md` 现已纳入 zip。

---

## ✨ 特性

- ⚡ **实时抓取**：每 10 分钟自动抓取宿舍剩余电量、累计用电、违规、缴费、单价
- 📱 **Web 仪表盘**：Apple HIG + MCSM 风格响应式 UI（移动端 / 桌面自适应）
- 🤖 **Feishu 机器人**："电费助手"私聊查询，9 条 slash 命令（`/状态` `/剩余` `/电表` `/今日` 等）
- 🖼️ **图片卡片回复**：Pillow 渲染的深色 stat card PNG（800×600，含中文字体）
- 🔔 **告警推送**：低余额 / 抓取失败 / 违规 / 电表离线 → Feishu 群消息
- 🔐 **零硬编码密钥**：所有凭据走 `.env`，适合二次开发与团队协作
- 💾 **SQLite 存储**：单文件 `records.db`，无需额外数据库
- 🌐 **零外部依赖**：纯 Python + Flask，本地一台小服务器即可运行

---

## 🖼️ 截图

> [待补] 建议在 `docs/screenshots/` 放 2-3 张：仪表盘、Feishu stat card、低电量告警。
>
> 临时预览：项目内置 `assets/fonts/MiSans-*.ttf`，本地启动后访问 `http://localhost:5000/` 即可看到完整效果。

---

## 🚀 快速开始

### 前置条件

- **Python 3.10+**（推荐 3.12）
- Linux / macOS / Windows 均可（生产推荐 Ubuntu 24.04+ / Debian 12+）
- 一台能稳定联网的小服务器（树莓派 4 / 旧笔记本 / 1 核 512MB VPS 即可）

### 1. 克隆与安装

```bash
git clone <your-repo-url> dorm-power-monitor
cd dorm-power-monitor
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

或者使用我们提供的一键脚本（推荐 Linux 用户）：

```bash
chmod +x setup.sh
./setup.sh
```

### 2. 配置 `.env`

```bash
cp .env.example .env
chmod 600 .env                     # 仅当前用户可读，保护凭据
nano .env                          # 或用你喜欢的编辑器
```

**必填项**（详见下方的 [配置项](#-配置项) 章节）：

- `DORM_OPENID` — 你从宿舍电费 H5 页面拿到的 openid（如何获取见下文）
- `FEISHU_WEBHOOK` — 你的群机器人 webhook URL（可选告警）

### 3. 初始化数据库

```bash
python3 -c "from db import init; init()"
```

会创建 `records.db`（位置由 `DB_PATH` 控制）以及 6 张表。

### 4. 启动 Web 仪表盘

```bash
python3 web.py
```

打开浏览器访问 `http://localhost:5000/` 即可。

### 5. (可选) 配置 systemd + nginx（生产部署）

```bash
sudo cp deploy/dorm-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dorm-web

sudo cp nginx/dorm.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/dorm.conf /etc/nginx/sites-enabled/dorm.conf
sudo nano /etc/nginx/sites-available/dorm.conf   # 修改 server_name
sudo nginx -t && sudo systemctl reload nginx
```

### 6. (可选) 配置定时抓取 cron

```bash
# 把 deploy/dorm-cron.txt 里的那行粘到你的 crontab：
crontab -e
# 添加：
*/10 * * * * /opt/dorm-power-monitor/.venv/bin/python /opt/dorm-power-monitor/dorm_power.py >> /var/log/dorm-power-monitor.log 2>&1
```

### 7. (可选) 接入 Feishu 机器人

让用户能在飞书私聊发 `/状态` 查询。详见下方 [🤖 Feishu 机器人部署指南](#-feishu-机器人部署指南)。

---

## 📋 配置项

所有配置都在 `.env` 文件里，`config.py` 会通过 `python-dotenv` 自动加载。

### 必填

| 变量 | 说明 | 示例 |
|------|------|------|
| `DORM_OPENID` | 宿舍电费 H5 页面的 openid 查询参数，等同于密码 | `oV3xX5-xxxxxxxx` |
| `FEISHU_WEBHOOK` | 群机器人 webhook URL（无则告警退化为 stdout） | `https://open.feishu.cn/open-apis/bot/v2/hook/xxxx` |

### Feishu 群机器人（告警）

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_WEBHOOK` | ✅ | 群机器人 webhook |
| `FEISHU_SECRET` | ❌ | 仅当机器人开启"签名校验"时需要 |

### Feishu 机器人应用（私聊命令，可选）

| 变量 | 必填 | 说明 |
|------|------|------|
| `FEISHU_APP_ID` | ❌ | 企业自建应用的 App ID |
| `FEISHU_APP_SECRET` | ❌ | 企业自建应用的 App Secret |
| `FEISHU_VERIFICATION_TOKEN` | ❌ | 事件订阅的 verification token |
| `FEISHU_ENCRYPT_KEY` | ❌ | 加密模式下的 AES key |

### 宿舍电费系统

| 变量 | 默认 | 说明 |
|------|------|------|
| `DORM_OPENID` | _(空)_ | **必填**。openid 查询参数 |
| `DORM_BASE_URL` | `http://ybhqcz.fjny.edu.cn` | 福建省某高校的电费服务地址，适配其他学校时**必须**改 |
| `DORM_ROOM_ID` | _(空)_ | 房间 UUID；留空则自动从 finduser 页面发现 |

### 本地存储

| 变量 | 默认 | 说明 |
|------|------|------|
| `DB_PATH` | `./records.db` | SQLite 文件路径，生产建议 `/var/lib/dorm-power-monitor/records.db` |
| `SCRAPE_INTERVAL_MINUTES` | `60` | 与 cron 调度保持一致（仅供参考） |

### Flask 仪表盘

| 变量 | 默认 | 说明 |
|------|------|------|
| `FLASK_HOST` | `127.0.0.1` | 监听地址，生产用 nginx 代理时保持 127.0.0.1 |
| `FLASK_PORT` | `5000` | 监听端口 |
| `FLASK_DEBUG` | `0` | `1` = 开启调试（仅开发） |

---

## 🔑 如何获取 openid

1. **微信**中打开宿舍电费小程序 / H5 页面
2. 复制**微信内嵌浏览器地址栏**的完整 URL，形如：
   ```
   https://ybhqcz.fjny.edu.cn/campus/webchat/dormEmRealRead/finduser?openid=oV3xX5-xxxxxxxx
   ```
3. 把 `oV3xX5-xxxxxxxx` 这一段（**仅 openid 的值**，不要整个 URL）粘到 `.env` 的 `DORM_OPENID=`

⚠️ **安全提示**：openid 等同于你宿舍电费的密码。如果泄露：解绑并重新绑定微信账号 → 学校会发新的 openid；单纯改 `.env` 不会让旧值失效。

---

## 🤖 Feishu 机器人部署指南

让用户在飞书里私聊机器人"电费助手"，发 `/状态` `/电表` 等命令。

### 1. 创建企业自建应用

1. 访问 [飞书开放平台](https://open.feishu.cn/app)
2. **创建企业自建应用** → 填名字（如"宿舍电费助手"）+ 头像
3. 进入应用详情

### 2. 申请权限

**权限管理** → 搜索并添加：

- `im:message` — 发送消息
- `im:message:send_as_bot` — 以机器人身份发消息
- `im:resource` — 上传图片

### 3. 配置事件订阅

**事件订阅**：

- 请求 URL：`https://your-domain/feishu/event`（必须公网 HTTPS）
- 添加事件：`im.message.receive_v1`（接收消息）

**校验方式**（可选，推荐）：

- 加密模式 → 把生成的 Encrypt Key 填到 `.env` 的 `FEISHU_ENCRYPT_KEY`
- 签名校验 → 启用后用 verification token 填 `FEISHU_VERIFICATION_TOKEN`

### 4. 启用机器人能力

**应用能力** → **机器人** → 启用

### 5. 发布版本

**版本管理** → 创建 1.0.0 → 提交发布 → 管理员审批（或"仅自用"）

### 6. 填入 `.env`

把刚才拿到的 `App ID` / `App Secret` / `Verification Token` / `Encrypt Key` 填到 `.env`。

### 7. 测试

在飞书搜索"电费助手" → 私聊 → 发 `/帮助` → 应该看到命令列表。

---

## 🏫 学校适配

本项目默认绑定**福建省某高校**的电费系统。要适配其他学校：

### 方法一：同厂商系统（"易班" / "完美校园" 等 H5）

只需改 `DORM_BASE_URL`：

```bash
# 例如换成南京某高校
DORM_BASE_URL=https://your-school.edu.cn
```

只要 URL 模式仍是 `finduser?openid=...` + `getEmRealRead` POST 即可。

### 方法二：完全不同的系统

1. 看 `dorm_power.py` 的 `_fetch_html` / `_fetch_data` / `_post_form` 三个函数
2. 替换 URL 路径和请求头（`User-Agent` / `Referer` / `Origin`）
3. 替换 `_build_card` / `_build_offline_card` 的字段映射（`remainEq` / `totalEq` 等）
4. 改 `db.py` 的 schema（如果返回字段名不同）
5. 改 `web.py` 模板里 `data-zong-eq` 等字段名

> 💡 **设计原则**：核心抓取逻辑 (`dorm_power.py`)、存储 (`db.py`)、Web 仪表盘 (`web.py`)、Feishu 机器人 (`feishu_bot.py`) 是解耦的。改一个学校的适配不需要碰其他模块。

---

## 🐛 故障排查

### 抓取失败 / 日志里没有新数据

```bash
# 1. 看最近的抓取日志
tail -f /var/log/dorm-power-monitor.log   # Linux 生产
# 或
python3 dorm_power.py                     # 本地手动跑一次

# 2. 检查 openid 是否还有效
#    重新走一次微信里的电费页面，看 URL 的 openid 是否变了

# 3. 检查 cron 是否在跑
crontab -l                                # 应包含 dorm_power.py 那一行
sudo systemctl status cron
```

### Feishu 机器人不回复

```bash
# 1. 看 Flask 日志（systemd）
sudo journalctl -u dorm-web -f

# 2. 检查 webhook 探活
curl -X POST -H "Content-Type: application/json" \
     -d '{"msg_type":"text","content":{"text":"test"}}' \
     $FEISHU_WEBHOOK

# 3. 检查应用是否已发布 + 权限已审批
#    飞书开放平台 → 你的应用 → 版本管理 + 权限管理
```

### Web 仪表盘打不开

```bash
# 检查 Flask 进程
sudo systemctl status dorm-web
# 检查端口
sudo ss -tlnp | grep 5000
# 看 nginx 错误
sudo tail -f /var/log/nginx/dorm.error.log
```

### Pillow 中文乱码（口口口）

```bash
# 检查字体文件是否存在
ls -la assets/fonts/MiSans-*.ttf
# 如果缺失，重新从仓库拉
git pull
```

### 数据库锁死

```bash
# SQLite WAL 模式下一般不会锁死。如果锁了：
fuser records.db          # 看谁在用
# 重启 systemd 服务
sudo systemctl restart dorm-web
```

### 完整重置

```bash
# 停止服务 + 删除数据库 + 重新初始化
sudo systemctl stop dorm-web
rm -f records.db
python3 -c "from db import init; init()"
sudo systemctl start dorm-web
```

---

## 🏗️ 项目结构

```
dorm-power-monitor/
├── README.md              # 本文件
├── LICENSE                # MIT
├── config.py              # 中心化配置（读 .env）
├── db.py                  # SQLite 助手（6 张表 + 元数据 KV）
├── dorm_power.py          # 抓取器 + Feishu 群卡片推送
├── feishu_bot.py          # Feishu 私聊机器人（Pillow 渲染 + 上传）
├── web.py                 # Flask 仪表盘（HTML + JSON API）
├── requirements.txt
├── setup.sh               # Linux 一键安装脚本
├── .env.example
├── .gitignore
├── assets/
│   └── fonts/             # MiSans TTF（Pillow CJK 渲染用）
├── deploy/
│   ├── dorm-cron.txt      # crontab 配置示例
│   ├── dorm-web.service   # systemd unit
│   └── dorm-power-monitor.logrotate
├── nginx/
│   └── dorm.conf          # nginx 反向代理示例
├── docs/                  # R44 起文档集中目录
│   ├── TECHNICAL.md
│   ├── Round25_dorm_id_research.md
│   └── reports/           # 各 round 的工作记录
├── tests/                 # R44 起测试集中目录
│   ├── modern/            # test_round35-42.py（zip 内打包）
│   └── legacy/            # test_round5-34d.py（仓库保留，zip 不含）
└── scripts/               # R44 起脚本集中目录
    ├── build/             # r35-r42_*.py（zip 构建）
    └── deploy/            # r41-r42_*.sh（一键部署）
```

### 数据流

```
┌──────────────┐  HTTP   ┌─────────────┐
│ dorm_power.py│ ──────> │ 学校电费 API │
└──────┬───────┘         └─────────────┘
       │ SQLite
       ▼
┌──────────────┐         ┌─────────────┐
│  records.db  │ <────── │  web.py     │ ← 浏览器
└──────┬───────┘         └─────────────┘
       │ (cron 10min)
       ▼
┌──────────────┐
│ Feishu webhook│ → 群告警卡
└──────────────┘

┌──────────────┐  HTTPS  ┌─────────────┐
│ feishu_bot.py│ <───── │ 飞书用户私聊 │
└──────┬───────┘         └─────────────┘
       │ 读 db / 渲染 PNG / 上传图片
       ▼
   飞书 API
```

---

## 🛠️ 开发

### 本地运行

```bash
# 拉依赖
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 跑一次抓取（用 dev .env）
python3 dorm_power.py

# 启动 Web（自动 reload）
FLASK_DEBUG=1 python3 web.py
```

### 测试

```bash
# 静态检查
python3 -m py_compile *.py

# （项目内未自带单元测试；推荐补充 pytest 覆盖 decrypt_payload / _upload_image）
```

### 添加新学校 / 新字段

1. 在 `dorm_power.py` 加一个 fetcher（参考 `_fetch_dormEmPayQuery`）
2. 在 `db.py` 加表 + helpers
3. 在 `web.py` 模板加展示
4. （可选）在 `feishu_bot.py` 加命令

---

## 🤝 贡献

欢迎 PR！特别是：

- 适配其他学校（改 `DORM_BASE_URL` 不够的话）
- 单元测试（pytest）
- Docker 镜像
- i18n（英文 README）
- 移动端 PWA

请在 PR 里写清楚：你的学校、URL 形态、字段差异。

---

## 📜 License

MIT — 见 [LICENSE](./LICENSE)。

---

## 🙏 致谢

- 福建省某高校的电费系统（虽然抓得很烦人，但能用）
- [Pillow](https://pillow.readthedocs.io/) / [Flask](https://flask.palletsprojects.com/) / [requests](https://requests.readthedocs.io/)
- 所有 PR 贡献者
