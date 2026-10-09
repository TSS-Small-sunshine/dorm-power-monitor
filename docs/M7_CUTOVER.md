# 生产切换与验收（M7）

这份文档回答两个问题：**「我手上那个库能用吗」** 和 **「切之前怎么测」**。

原则：**先旁路跑一套新的，确认没问题再动旧的。** 全程不用停旧服务，
随时可以放弃（旧的连一个字节都没被碰过）。

---

## 第 0 步：先确认你的数据库能用（1 分钟）

你不需要知道手上那个 `records.db` 是哪一版代码建的 —— 让工具告诉你：

```bash
# 在**新代码**目录里跑（它会现场建一个空库做基准来比对）
python -m scripts.check_db /path/to/你的/records.db

# Docker 用户：镜像里也带着这个脚本，直接在容器里查
docker compose exec starwatt python -m scripts.check_db /data/records.db
```

输出长这样（这是真的输出，不是示意）：

```
schema_version：0（老库（没有 schema_version 记录：v1 基线或更早））

表与列：
  ✅ audit_log
  ✅ daily_elec      0 行
  ✅ records         12345 行
  ⚠  users                        缺列 disabled, must_change_password
  ...

结论：✅ 可以直接用。启动时 init() 会自动补齐，**不动你的数据**：
  1. 给这些表加列（ALTER TABLE ADD COLUMN）：users.disabled、users.must_change_password
  2. 把 meta.schema_version 写成 3
```

**三种结论怎么处理：**

| 结论 | 含义 | 你要做什么 |
|---|---|---|
| `✅ 可以直接用`（可能有 ⚠ 缺列） | 结构与新代码兼容，缺的列启动时自动补 | 备份后直接用 |
| `❌ 认不出这是本项目的库` | 这个文件不是本项目的库（或表名完全不同） | 确认路径对不对；确实不是就重新走 OOBE |
| `❌ 不是 SQLite 数据库（或已损坏）` | 文件坏了 / 传错了 | 用备份恢复 |

想**先看结果再决定**？加 `--fix`，它会在**副本**上真跑一遍升级并对比行数：

```bash
python -m scripts.check_db /path/to/records.db --fix
```

```
在副本上跑了一遍 init()（原库未改动）
  schema_version：0 → 3
  补上的列：users.disabled、users.must_change_password
  行数对比（升级前 → 升级后）：
    ✅ records      12345 → 12345
    ✅ daily_elec     180 → 180
结论：✅ 副本升级干净 —— 数据一行不少、结构已与当前代码一致。
```

> 🔑 **这个工具绝不会改你的库**：默认用只读方式打开，`--fix` 也是先复制再动。
> 测试里有一条断言专门盯着这件事（比对操作前后原文件的 SHA-256）。

### 为什么敢说「零迁移」

新代码启动时做两件事（`starwatt/db/connection.py::init()`）：

1. `CREATE TABLE IF NOT EXISTS ...` —— 缺的表建出来，**已存在的表不动**
2. `migrations.migrate()` —— 只做 `ALTER TABLE ADD COLUMN` / 加表 / 加索引，
   **从不删列、不改列**，且每条迁移都先查 `PRAGMA table_info` 保证幂等

从 1.x 到 2.0，**唯一的结构差异是 `users` 表多 2 列**
（`disabled`、`must_change_password`），正是启动时自动补的那两列。
这条结论有测试兜底：`tests/regression/test_legacy_db.py` 会拿 M0 阶段在
**旧代码上抓下来的真实表结构**造一个库，塞进数据，跑一次真实启动路径，
然后断言数据一行不少、结构补齐、版本号写对。

---

## 第 1 步：旁路起一套新的（**用旧库的副本**）

关键：**在另一个端口、用一份副本**。这样新旧两套可以同时开着对比，
而且就算新版本有问题，删掉副本就行 —— 旧的完全没被碰过。

```bash
# 先做副本（别直接指向正在用的库！）
mkdir -p /tmp/starwatt-test
cp /path/to/records.db /tmp/starwatt-test/records.db
```

### A. Docker

```bash
sudo chown -R 10001:10001 /tmp/starwatt-test     # 容器内是非 root(10001)

docker run -d --name starwatt-test \
  -p 127.0.0.1:5001:5000 \
  -v /tmp/starwatt-test:/data \
  -e BOOTSTRAP_ADMIN_PASSWORD='Str0ng-Pass!' \
  ghcr.io/tss-small-sunshine/starwatt:2.0.0-starcore

docker logs starwatt-test | tail -20      # 看有没有报错
```

### B. 裸机（Debian/Ubuntu）

```bash
cd /opt/dorm-power-monitor        # 新代码
FLASK_PORT=5001 \
DB_PATH=/tmp/starwatt-test/records.db \
DORM_DATA_DIR=/tmp/starwatt-test \
BOOTSTRAP_ADMIN_PASSWORD='Str0ng-Pass!' \
.venv/bin/gunicorn -c gunicorn.conf.py web:app
```

> 环境变量**优先于 `.env`**（systemd 的 `EnvironmentFile=` 同理），
> 所以这里不用改你已有的 `.env`。

打开 <http://127.0.0.1:5001> 用 `admin` / `Str0ng-Pass!` 登录。

---

## 第 2 步：验收清单（9 项）

前 6 项能立刻验完，第 7 项要等一个抓取周期，第 8 项要等到报表时间。

### 1. 探活

```bash
curl -fsS http://127.0.0.1:5001/healthz
# {"status":"ok","time":"...","version":"starwatt 2.0.0 ..."}
```

### 2. 首登被强制改密（Q19）

用 `admin` + 引导密码登录 → 应**直接跳到改密页**，不让你进仪表盘。
改完再进。这条是「新用户按文档装完能安全上手」的关键，务必实测。

### 3. 首页 4 段全部渲染

概览（剩余电量 + 趋势图 + 4 张统计卡）/ 历史 / 违规 / 电表 —— 四段都点一遍。
**历史与违规应该有旧库里的真实数据**（这正是「零迁移」的直观证据）。

### 4. 旧数据确实在

```bash
# 直接查副本，对比新旧两套显示的数字
sqlite3 /tmp/starwatt-test/records.db \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM records;"
```

### 5. 抓取在跑（`*/10`）

打开「管理 → 日志」，把 `scrape` 类别调到 `DEBUG`；等一个周期（默认 10 分钟，
`scrape_interval_sec=600`），日志里应出现抓取记录，首页「最后更新」时间会变。

想立刻验证而不等：把「配置中心 → 数据采集 → 抓取间隔」临时改成 `60` 秒。

### 6. 机器人 9 个命令

在飞书私聊（或 QQ）逐个发：

```
/状态  /剩余  /电表  /电表状态  /今日  /历史  /缴费  /违规  /帮助
```

群里要 **@机器人** 才响应。全部有回复即通过；某个命令没反应就去
「管理 → 功能开关 → 命令」看它是不是被关了（默认私聊机器人总开关是**关**的，
要先打开）。

### 7. 配置中心 94 项 + 功能开关 18 个

「管理 → 配置中心」应显示「94 个配置项」；随便改一项（例如站点名称）→ 保存 →
刷新页面确认生效。「管理 → 功能开关」应有 3 个总开关 + 8 个告警层 + 7 个命令开关，
关掉一个再看抑制原因是否显示。

### 8. L3 日报（默认 09:00）

「配置中心 → 推送（群）→ 日报时间」默认 `09:00`，`L3 日报` 开关默认**开**。
等过了那个时间点，群里应收到日报卡片。**验证成功的最快方式是看状态键**：

「配置中心」勾选「显示高级项」→ 找 `last_daily_report_date`，
它应等于今天的日期。

### 9. 日志 7 级 × 8 类

「管理 → 日志」应能看到 7 个级别（TRACE…CRITICAL）与 8 个类别
（scrape / push / auth / config / scheduler / db / web / notify），
以及「有效级别」表。改一下某类的级别，确认保存后立即生效。

---

## 第 3 步：正式切换

验收都过了再动旧的。**先备份**（数据 + 密钥）：

```bash
# Docker
docker run --rm -v starwatt-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/starwatt-$(date +%F).tar.gz -C /data .
# 裸机
sudo tar -czf ~/starwatt-$(date +%F).tar.gz \
  -C /var/lib/dorm-power-monitor records.db -C /opt/dorm-power-monitor .env
```

然后：

```bash
# 1) 停旧的
#    Docker：  docker stop <旧容器名>
#    裸机：    sudo systemctl stop <旧服务名>       # 旧版可能挂着 cron
#              sudo crontab -l | grep -i dorm      # 有 cron 就注释掉（Q14 后不再需要）
# 2) 起新的（用**原库**，不再是副本）
docker compose up -d           # 或 sudo ./install.sh --upgrade
# 3) 验证（同第 2 步的 1/2/3 项）
curl -fsS http://127.0.0.1:5000/healthz
```

> 新旧代码读写**同一个** `records.db`，所以中间不需要停机窗口 ——
> 就算你不停旧的，两套同时跑也只会各自抓一次（数据库层面无冲突）。

---

## 第 4 步：回滚演练（**动手前先做一遍**）

切换前拿副本练一次，心里有底。Docker 上真的是 30 秒：

```bash
docker compose down                                  # ~3 秒
STARWATT_TAG=<旧版本> docker compose up -d            # ~10 秒（镜像已在本地）
curl -fsS http://127.0.0.1:5000/healthz              # 通 → 完成
```

数据不用动：同一个 `records.db`，旧代码直接能读。

裸机版见 [UPGRADE.md](UPGRADE.md#四回滚30-秒演练)。

---

## 附录 A：哪些必须真机测（本机测不了）

我（开发机）在 M6/M7 阶段能验证到哪一步，如实说明：

| 项 | 本机 | 由谁验证 |
|---|---|---|
| 旧库零迁移（数据 + 结构） | ✅ 已用 M0 真实快照造库测过 | `tests/regression/test_legacy_db.py` |
| `check_db` 不改原库 | ✅ 有 SHA-256 断言 | 同上 |
| 前端 4 段渲染 / 首登改密 | ✅ 真实 app + 无头浏览器截过图 | `docs/screenshots/` |
| `docker compose up -d`（amd64） | ❌ 本机无 Docker | CI 的 release 冒烟（load → run → `/healthz` → SPA） |
| `docker compose up -d`（arm64/armv7） | ❌ 无 arm 机器 | **需要你在真机确认** |
| `install.sh` 在干净 VPS | ❌ 无 VPS | **需要你在真机确认**（只验证过语法与参数行为） |
| 机器人 9 命令 / 日报卡片 | ❌ 需要真实凭据 | **需要你用真实机器人确认** |

## 附录 B：常见失败与处理

| 现象 | 原因 | 处理 |
|---|---|---|
| 新实例起来但首页空白 | 前端产物没构建 | Docker 镜像里已内置；裸机跑 `cd frontend && npm ci && npm run build` |
| 登录后一直跳回登录页 | 会话 Cookie 没发出去 | 检查是不是 HTTPS 反代但 `SESSION_COOKIE_SECURE` 与协议不匹配 |
| 配置中心里凭据显示「解密失败」 | 换过 `FLASK_SECRET_KEY` | 把旧密钥填回 `.env`（或卷里的 `.flask_secret_key`）后重启 |
| 抓取一直失败 | openid 过期 / 内网地址被 SSRF 拦 | 「配置中心 → 测试连通」看具体错误；必要时开 `allow_private_hosts` |
| 群里收不到卡片 | 开关 / 静默时段 / 签名密钥 | 「功能开关」看抑制原因；`feishu_secret` 要与飞书后台一致 |
| 容器起来但外部连不上 | 绑了 127.0.0.1 | 容器必须 `GUNICORN_BIND=0.0.0.0:5000`（compose 已设） |
| 启动即退出，日志有 `RuntimeError` | 多进程下调度器守卫 | 确认 `workers=1` 且用 `gunicorn -c gunicorn.conf.py web:app` |

更多见 [FAQ.md](FAQ.md)。
