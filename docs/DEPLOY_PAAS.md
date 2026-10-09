# 托管平台部署（Vercel / Netlify / EdgeOne / Fly / Render）

先说结论，再说为什么。

## 一、结论表

| 平台 | 前端（SPA） | 后端（抓取 + 调度 + 数据库） | 怎么用 |
|---|---|---|---|
| **Vercel** | ✅ 一键 | ❌ 跑不了 | 前端托管 + 反代 `/api` 到你的后端 |
| **Netlify** | ✅ 一键 | ❌ 跑不了 | 同上 |
| **EdgeOne Makers**（腾讯） | ✅ 一键 | ❌ 跑不了（重写**不能**反代外部后端） | 只托管前端；接后端要写 Cloud Function |
| **Fly.io** | ✅ | ✅ **全功能** | `fly.toml` 已备好，见第三节 |
| **Render** | ✅ | ✅ **全功能** | `render.yaml` 已备好，见第三节 |
| 你自己的机器（树莓派 / 旧笔记本 / VPS） | ✅ | ✅ **全功能** | [DEPLOY.md](DEPLOY.md)，**最省事** |

> **一句话**：这个应用的后端需要「常驻 + 有磁盘」，所以 Vercel/Netlify/EdgeOne
> 只能托管它的界面；想一键部署**全部功能**，用支持容器和持久卷的平台。

## 二、为什么后端跑不了 serverless

不是配额问题，是**架构**问题 —— 这个应用需要三样东西，而 serverless 三样都不给：

| 需要 | 代码里的事实 | serverless 的现实 |
|---|---|---|
| **持久可写磁盘** | SQLite `records.db` 直接落盘（WAL 模式，抓取写 / 网页读并发） | 函数只有 `/tmp`，且每次冷启动都是新的 → 数据每次都没了 |
| **常驻进程** | 调度器在 gunicorn worker 里启动，每 10 分钟抓一次、每天 9 点发日报 | 函数「来请求才跑」，没人访问就什么都不发生 → 抓取与日报静默失效 |
| **单实例** | 调度器是进程内单例（多实例会重复抓取、重复推送） | 函数会并发扩缩容 |

想真在 serverless 上跑，得把存储换成外部服务（Turso/libSQL、EdgeOne KV/Blob）、
把调度换成平台 cron、把应用改成无状态 —— 那是另一次重写，而且会**放弃
「零迁移」**（Q9/Q10：现有 `records.db` 直接可用）。本项目不打算这么做。

## 三、方案 A：全功能一键部署（推荐）

因为已经有 `Dockerfile`，任何「支持容器 + 持久卷」的平台都能直接跑，**不用改代码**。

### Fly.io

```bash
fly launch --copy-config --no-deploy        # 用仓库里的 fly.toml
fly volumes create starwatt_data --size 1   # 持久卷（名字要与 [mounts] 一致）
fly deploy
fly logs | grep 初始密码                     # 首启随机管理员密码，只打印一次
```

`fly.toml` 里三处是**为这个应用专门设的**，别改：

```toml
auto_stop_machines = false   # 机器不许因没流量被停 —— 停了就不抓取、不发日报
min_machines_running = 1     # 至少留一个实例（调度器是单例）
[mounts]                     # 数据库与会话密钥都在卷里，重部署不丢
```

### Render

控制台 → **New → Blueprint** → 选这个仓库（自动读 `render.yaml`）。

`render.yaml` 里两处值得注意：

* `plan: starter` —— **Free 套餐不支持持久磁盘**，而且闲置会休眠（休眠 = 不抓取）
* `FLASK_PORT/GUNICORN_BIND = 10000` —— Render 按 `PORT`（默认 10000）转发与探活

### 其他同类平台

Railway、Zeabur、Koyeb 等都支持「Dockerfile + 卷」，套路一样：
**挂一个卷到 `/data`、设 `GUNICORN_BIND=0.0.0.0:5000`、保证实例常驻**。

> 三个环境变量就是全部要求（`DORM_DATA_DIR=/data`、`DB_PATH=/data/records.db`、
> `GUNICORN_BIND=0.0.0.0:PORT`）。`FLASK_SECRET_KEY` 不用配：容器启动时会生成
> 并写进 `/data/.flask_secret_key`，重启复用（配了更稳妥，见 [CONFIG.md](CONFIG.md)）。

## 四、方案 B：只把前端托管在 Vercel / Netlify / EdgeOne

**什么时候值得这么做**：你已经有后端（家里那台常开的机器、VPS、NAS），
但想要一个 HTTPS 域名 + CDN 加速的界面，不想自己配证书。

配置已经在仓库里了，都是「改一个地方就能用」：

| 文件 | 平台 | 要改的地方 |
|---|---|---|
| `vercel.json` | Vercel | `YOUR-BACKEND.example.com` → 你后端的公网 HTTPS 域名 |
| `netlify.toml` | Netlify | 同上 |
| `edgeone.json` | EdgeOne Makers | **没有反代**，见下面的说明 |

### 三个平台都要注意的两件事

**1. `/static/*` 必须映射回根目录。**
前端产物里的资源引用带 `/static/` 前缀（`vite.config.ts` 的 `base: '/static/'`，
是为了和 Flask 的 `static_url_path` 对齐）。静态托管把 `static/` 目录当作站点根
之后，`/static/assets/x.js` 会 404 —— 所以三个配置里都有一条
`/static/* → /:splat` 的重写。**删掉它页面就会白屏。**

**2. SPA 兜底必须是最后一条规则。**
`/* → /index.html` 放在前面会把 `/api` 和静态资源一起吃掉。

### 反代之后 Cookie 还好用吗

好用，**后端一行都不用改**。因为浏览器只看到托管平台这一个域名，
`/api` 是平台服务端转发过去的：

* 会话 Cookie（`SameSite=Lax`）作用在这个域名上 ✓
* CSRF 头照常带上 ✓
* 后端不需要开 CORS ✓（不是跨域请求）

### EdgeOne 的特别之处（重要）

EdgeOne Makers 的 `rewrites` **只对静态资源有效，不能反代到外部后端**
（官方文档明确写了适用范围限制）。所以：

* `edgeone.json` 里**故意没有** `/api` 规则 —— 假装能反代只会让你以为配好了
* 想在 EdgeOne 上接后端，得用它的 **Cloud Functions**（支持 Node.js / Python / Go）
  写一个转发函数，把 `/api/*` 请求转给你的后端
* 顺带一提：EdgeOne 还有 KV / Blob 存储与 `schedules` 定时任务，
  理论上能做一个 serverless 版 —— 但要重写存储层与调度，等于放弃零迁移

## 五、各平台免费额度的坑（务必先看）

| 坑 | 影响 |
|---|---|
| **闲置休眠** | 实例被停 = 调度器停了 = **不抓取、不发日报**，而且你不会收到任何报错。这是最危险的一个：界面看着正常，数据却是几小时前的 |
| **免费套餐没有持久磁盘** | SQLite 没地方放；就算塞进 `/tmp`，重启就全丢 |
| **冷启动** | 每次访问都要等几秒（对这个应用影响不大，但抓取 tick 可能超时） |
| **函数执行时长上限** | 单次抓取 + 卡片渲染可能超时（Vercel/Netlify 都是秒级到几十秒） |

**判断标准**：
* 想要「装完就不用管」→ 用 Fly / Render（付费档）或你自己的机器
* 已经有一台常开的机器 → 直接 `docker compose up -d`，**比任何平台都省事**，
  数据也在自己手里（见 [DEPLOY.md](DEPLOY.md)）

## 六、一键部署按钮？

平台方一般要求「部署按钮」指向**模板仓库**，所以这里不放按钮，而是给配置：

* 支持容器的平台：仓库里已有 `Dockerfile` + `fly.toml` / `render.yaml`，
  在平台控制台选这个仓库即可（Render 认 `render.yaml`，Fly 认 `fly.toml`）
* 静态平台：仓库里已有 `vercel.json` / `netlify.toml` / `edgeone.json`，
  在平台控制台选仓库 → 它会自动按配置构建

> 这些配置都有测试盯着（`tests/unit/test_deploy.py::TestPlatformDeployConfigs`）：
> 比如「SPA 兜底必须是最后一条」「`/static/*` 映射不能少」「Fly 的机器不许自动停」。
> 因为它们只在平台上才被执行，本地跑不到，写错了只会在线上表现为白屏。
