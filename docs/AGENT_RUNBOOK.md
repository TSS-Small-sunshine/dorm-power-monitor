# 服务器切换 Runbook（交给服务器上的 Agent 执行）

> **给使用这份文档的人**：把整个文件发给服务器上的 Agent，并告诉它
> 「按这个 Runbook 执行，**每个阶段结束把结果报给我**，遇到决策点先问我」。
>
> 本文档面向 **amd64**（x86_64）服务器。目标：把旧的 StarWatt 1.x 换成 2.0，
> **历史数据一条不丢**；能更新就更新，只有旧环境无法修复时才重装。

---

## 给执行者的说明（Agent 先读这一段）

**你的任务**：按阶段推进，每阶段结束向人报告；**不确定就停下来问**，不要自己发挥。

**三条红线（任何阶段都不得违反）**

1. **不要删除或覆盖 `records.db`**（那是历史数据）。可以复制、可以备份、可以只读打开，
   但不能删、不能覆盖、不能 `rm -rf` 它所在的目录。
2. **不要覆盖已有的 `.env`**。里面的 `FLASK_SECRET_KEY` 同时用于加密配置中心里的
   凭据（openid / 飞书 / QQ 密钥）。它一变，那些**密文永久解不开**，只能重新填一遍。
3. **不要停掉旧服务，直到阶段 3 的验收全部通过**。全程用副本、用另一个端口旁路验证。

**其他纪律**

* 阶段 0（侦察）**只读**，不改任何东西、不装任何东西。
* 每个命令都写清楚「预期看到什么」。**输出与预期不符就停下来报告**，不要继续往下走。
* 如果你发现本文档与服务器实际情况不一致（例如根本没有 `records.db`），
  **停下来报告**，不要猜测、不要编造路径。
* 报告用阶段末尾的模板。**贴原始输出**，不要只写「已完成」。

**环境**：amd64 / Linux。Docker 与裸机两条路线，阶段 0 会判定走哪条。

---

## 阶段 0：侦察（**只读**，不改任何东西）

目标：搞清楚「现在跑的是什么、数据在哪、怎么起的」。

```bash
# 0.1 旧服务在跑吗？怎么起的？
docker ps -a --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' | head -20
systemctl list-units --type=service --all 2>/dev/null | grep -iE 'dorm|starwatt|power' || echo "(无 systemd 服务)"
ps aux | grep -iE 'dorm|starwatt|web\.py|gunicorn' | grep -v grep || echo "(无相关进程)"
crontab -l 2>/dev/null | grep -iE 'dorm|starwatt|power' || echo "(无相关 crontab)"
ss -lntp 2>/dev/null | grep -E ':5000|:5001' || echo "(5000/5001 端口没在监听)"
```

```bash
# 0.2 数据库在哪？（最关键的一步）
sudo find / -name 'records.db*' -not -path '*/node_modules/*' 2>/dev/null
# 预期：1~2 个路径。典型位置：
#   /var/lib/dorm-power-monitor/records.db     ← 裸机
#   /opt/dorm-power-monitor/records.db         ← 裸机（旧版可能直接放代码目录）
#   /var/lib/docker/volumes/*/_data/records.db ← Docker 命名卷
#   ~/dorm-power-monitor/records.db            ← 手动 clone 跑
```

```bash
# 0.3 每个库有多大、多少行、最后一次写入（**只读**）
for db in <把 0.2 找到的路径列在这里>; do
  echo "=== $db ==="
  ls -lh "$db"
  sudo sqlite3 "file:$db?mode=ro" \
    "SELECT 'records', COUNT(*), MAX(ts) FROM records;
     SELECT 'daily_elec', COUNT(*), MAX(dt) FROM daily_elec;" 2>&1
done
# 预期：records 有几千~几万行，MAX(ts) 是最近几小时/几分钟
# ⚠️ 若 MAX(ts) 是几天前，说明旧服务其实早就没抓了 —— 记下来，报告里说明
```

```bash
# 0.4 配置与密钥在哪
sudo find / -name '.env' -path '*dorm*' -not -path '*/node_modules/*' 2>/dev/null
sudo find / -name '.flask_secret_key' 2>/dev/null
# 预期：一个 .env（含 DB_PATH / DORM_DATA_DIR / FLASK_PORT / FLASK_SECRET_KEY）
#       Docker 用户可能没有 .env，密钥在卷里的 .flask_secret_key
```

**阶段 0 报告模板**

```
【阶段 0 · 侦察】
部署方式：Docker / 裸机 / 其他（说明）
旧服务：容器名/服务名/进程 = ____，启动方式 = ____
数据库：路径 = ____，大小 = ____，records 行数 = ____，最后写入 = ____
配置：.env 路径 = ____（或：无 .env，密钥在 ____）
端口：5000 在监听？是/否
可疑点：<例如「records 最后写入是 3 天前」「找到 2 个 records.db」>
```

> **如果有两个 `records.db`**：用「行数多 / 最后写入新」的那个，并在报告里说明另一个是什么。

---

## 阶段 1：备份（**动任何东西之前**）

```bash
STAMP=$(date +%F-%H%M%S)
sudo mkdir -p /root/starwatt-backup-$STAMP

# 1.1 数据库（连同 WAL 边车文件，否则最近的写入可能在副本里看不到）
sudo cp -a <0.2 的 records.db>        /root/starwatt-backup-$STAMP/
sudo cp -a <0.2 的 records.db>-wal    /root/starwatt-backup-$STAMP/ 2>/dev/null || true
sudo cp -a <0.2 的 records.db>-shm    /root/starwatt-backup-$STAMP/ 2>/dev/null || true

# 1.2 密钥（丢了就无法解密配置中心里的凭据）
sudo cp -a <0.4 的 .env>              /root/starwatt-backup-$STAMP/ 2>/dev/null || true
sudo cp -a <0.4 的 .flask_secret_key> /root/starwatt-backup-$STAMP/ 2>/dev/null || true

sudo tar -czf /root/starwatt-backup-$STAMP.tar.gz -C /root starwatt-backup-$STAMP
ls -lh /root/starwatt-backup-$STAMP.tar.gz
```

**1.3 必须验证备份能打开**（打不开的备份等于没备份）：

```bash
sudo tar -tzf /root/starwatt-backup-$STAMP.tar.gz | head
sudo sqlite3 "file:/root/starwatt-backup-$STAMP/records.db?mode=ro" "SELECT COUNT(*) FROM records;"
# 预期：行数与阶段 0 完全一致
```

**报告**：备份路径、大小、行数是否一致。**不一致就停下来。**

> 🔴 把 `.tar.gz` 再拷一份到**另一台机器或对象存储**，然后才继续。
> 做不到就在报告里说明，让人决定。

---

## 阶段 2：数据库自检（回答「我这个库能不能直接用」）

```bash
cd /opt/dorm-power-monitor        # 或任何一份 2.0 代码目录；没有就先克隆：
# git clone https://github.com/TSS-Small-sunshine/dorm-power-monitor.git && cd dorm-power-monitor

python -m scripts.check_db <0.2 的 records.db> --fix
```

**三种结论与处置**

| 输出 | 含义 | 怎么做 |
|---|---|---|
| `✅ 可以直接用`（可能带 ⚠ 缺列） | 结构与 2.0 兼容，缺的列启动时自动补 | **继续阶段 3** |
| `❌ 认不出这是本项目的库` | 这个文件不是本项目的库 | 停下报告（可能找错文件了） |
| `❌ 不是 SQLite 数据库（或已损坏）` | 文件坏了 | 用阶段 1 的备份恢复；备份也坏就停下报告 |

`--fix` 会在**副本**上试跑一遍升级并对比行数 —— 预期看到
`schema_version：0 → 3`、`补上的列：users.disabled、users.must_change_password`、
以及每张表 `✅ 行数不变`。

**报告**：贴出 `check_db --fix` 的完整输出。


## 阶段 3：旁路起新版（**旧服务完全不动**）

用**副本**、在**另一个端口**起一套新的，验收通过再谈切换。

### 3.1 Docker 路线（推荐，隔离最干净）

```bash
sudo mkdir -p /tmp/starwatt-test
sudo cp -a <0.2 的 records.db> /tmp/starwatt-test/records.db
sudo chown -R 10001:10001 /tmp/starwatt-test      # 容器内是非 root(10001)，属主不对会写不进去

docker pull ghcr.io/tss-small-sunshine/starwatt:2.0.1-starcore
# ⚠️ 如果拉不动（国内访问 ghcr.io 经常慢/超时），走下面的「离线包」路线

docker run -d --name starwatt-test \
  -p 127.0.0.1:5001:5000 \
  -v /tmp/starwatt-test:/data \
  ghcr.io/tss-small-sunshine/starwatt:2.0.1-starcore

sleep 5 && docker logs starwatt-test | tail -30
# 预期：能看到迁移日志（migration: applying v1/v2/v3、schema_version 0 -> 3）、
#       gunicorn 启动、调度器启动；没有 Traceback
```

**离线包路线**（拉不动 ghcr.io 时用；离线包不依赖任何外网）：

```bash
# 1) 在能上网的机器上下载（或让用户下载后传上来）：
#    https://github.com/TSS-Small-sunshine/dorm-power-monitor/releases
#    取 starwatt-2.0.1-starcore-amd64.tar.gz
# 2) 传到服务器后导入：
docker load -i starwatt-2.0.1-starcore-amd64.tar.gz
docker run -d --name starwatt-test -p 127.0.0.1:5001:5000 \
  -v /tmp/starwatt-test:/data \
  ghcr.io/tss-small-sunshine/starwatt:2.0.1-starcore
```

### 3.2 裸机路线

```bash
cd /opt/dorm-power-monitor          # 一份 2.0 代码（没有就先 git clone）
python3 -m venv .venv-2.0 && .venv-2.0/bin/pip install -q -r requirements.txt
FLASK_PORT=5001 \
DB_PATH=/tmp/starwatt-test/records.db \
DORM_DATA_DIR=/tmp/starwatt-test \
  .venv-2.0/bin/gunicorn -c gunicorn.conf.py web:app
# 前台跑着，另开一个终端做下面的验收；验收完 Ctrl-C 停掉
```

### 3.3 建一个能登录的管理员（**必须做**）

旧版 1.x 的密码哈希是 bcrypt，2.0 换成了 stdlib scrypt —— **旧密码不再可用**，
所以要新建一个管理员（这也是切换后你自己要做的第一件事）：

```bash
# Docker
docker exec -i starwatt-test python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码Aa1!", role="admin")
print("已创建 admin2")
PY
```

> 密码要求：≥8 位且同时含**大写字母、数字、特殊字符**，否则会被拒绝并给出原因。

### 3.4 验收（自动项）

```bash
python -m scripts.cutover_check \
  --base http://127.0.0.1:5001 --user admin2 --password '你刚设的密码' \
  --db /tmp/starwatt-test/records.db
```

**预期**：`结论：✅ N 项通过，0 项失败`，并且能看到**真实数字**（概览条数、
电表电压电流、历史天数、违规条数、94 项配置、18 个开关、7×8 日志、`schema_version=3`）。

**如果出现 ⚠️「历史/违规/缴费 暂时为空」**：这是**正常过渡态**，不是数据丢失 ——
这几段按房间号过滤，而房间号由**成功的抓取**发布。等一个周期（或点首页「刷新」）即可。
想确认库里确实有数据：

```bash
sudo sqlite3 "file:/tmp/starwatt-test/records.db?mode=ro" \
  "SELECT COUNT(*) FROM records; SELECT COUNT(*) FROM daily_elec;"
```

**报告**：贴出 `cutover_check` 的完整输出 + 上面两条 `COUNT(*)`。
**有 ❌ 就停下来**，把输出发给人看。

---

## 阶段 4：正式切换

前提：阶段 3 全绿，且人已确认可以切换。

```bash
# 4.1 停旧的（Docker：docker stop <旧容器名>；裸机：sudo systemctl stop <旧服务名>）
#     裸机还要处理 cron（旧版靠它抓取，2.0 不需要，留着会重复抓）
crontab -l > /root/crontab-backup-$(date +%F).txt      # 先备份
crontab -l | grep -v -iE 'dorm|starwatt|power' | crontab -   # 去掉相关行

# 4.2 起新的 —— 这次用**原库**（不是副本）
#     Docker：改回正式端口与正式卷/目录，然后
docker compose up -d
#     裸机：会保留 .env 与数据，只重建依赖/前端
sudo ./install.sh --upgrade
```

> ⚠️ **不要删旧容器、旧代码目录、旧 systemd 单元** —— 阶段 5 回滚要用它们。

**4.3 验证**（与阶段 3 相同的检查，换成正式地址）

```bash
curl -fsS http://127.0.0.1:5000/healthz
python -m scripts.cutover_check --base http://127.0.0.1:5000 \
  --user admin2 --password '...' --db <正式库路径>
```

**4.4 数据没丢的最终确认**

```bash
sudo sqlite3 "file:<正式库路径>?mode=ro" \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM records;"
# 预期：与阶段 0 的行数一致（升级只会加列，不会删行）
```

---

## 阶段 5：回滚（**切换前先演练一次**）

```bash
# Docker：停新的、把旧的按原样起回来（旧容器还在）
docker stop <新容器>
docker start <旧容器>          # 或 docker compose -f <旧 compose> up -d

# 裸机
sudo systemctl stop <新服务>
sudo systemctl start <旧服务>
```

**数据不用动**：新旧代码读写同一个 `records.db`。2.0 的迁移**只新增**列
（`users` 多 2 列），不删列、不改列，所以回滚通常是安全的。

> ⚠️ 但我**无法替旧代码保证**：它读 `users` 表时如果用 `SELECT *` 再按位置解包，
> 多出来的列可能让它出错。所以**回滚演练必须真做一次**（阶段 5 就是为此），
> 而且切换后先别删旧环境。如果演练时旧代码起不来，就恢复阶段 1 的备份。

**报告**：回滚演练是否成功、`/healthz` 是否恢复、旧页面能不能打开。

---

## 阶段 6：交接报告（最后贴给用户）

```
【切换完成报告】
旧版本/新版本：____ → 2.0.1-starcore
数据库：路径 ____，切换前 ____ 行，切换后 ____ 行（应一致）
备份：/root/starwatt-backup-____.tar.gz（已另存一份：是/否）
自检：cutover_check 结论 = ____ 项通过 / ____ 项失败
新管理员：admin2（首登是否要求改密：是/否）
仍需人工确认：
  □ 飞书私聊 9 个命令都有回复
  □ 群里收到卡片
  □ 等一个抓取周期后首页「最后更新」时间会变
  □ 旧容器/旧目录仍保留（未删）
异常与遗留：____
```

---

## 附录 A：什么时候才该「重装」

**默认答案是「不要重装」** —— 阶段 3/4 的升级路径已经足够，而且更安全
（旧环境原封不动地留着，随时能回滚）。

只有下面这些情况才重装：

* 旧环境**无法确定**是怎么起的（进程/服务/cron 都对不上，找不到启动脚本）
* 旧代码目录已被删、或依赖坏到修不了（`pip`/`venv` 全乱）
* 系统盘要重做 / 换机器

**重装也必须保住数据**（否则等于从零开始，历史全没）：

```bash
# 1) 停掉旧的一切（容器 / systemd 服务 / crontab）
# 2) 建数据目录，把备份里的库放进去
sudo mkdir -p /var/lib/dorm-power-monitor
sudo cp -a /root/starwatt-backup-<STAMP>/records.db /var/lib/dorm-power-monitor/
sudo chown -R 10001:10001 /var/lib/dorm-power-monitor     # Docker 跑的话必须这个属主

# 3) 恢复密钥 —— 🔴 必须用**原来那把** FLASK_SECRET_KEY
#    （换了它，配置中心里加密的 openid / 飞书 / QQ 凭据就永久解不开）
sudo cp -a /root/starwatt-backup-<STAMP>/.env /opt/dorm-power-monitor/.env
#    或（Docker 卷方案）把 .flask_secret_key 放回数据目录：
#    sudo cp -a /root/starwatt-backup-<STAMP>/.flask_secret_key /var/lib/dorm-power-monitor/

# 4) 用官方 compose 起新版
cd /opt/dorm-power-monitor && docker compose up -d

# 5) 建管理员 + 自检（同阶段 3.3 / 3.4）
```

**重装后必须做的两件事**：建新管理员、跑 `cutover_check` 确认旧数据都在。

---

## 附录 B：常见错误对照

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录返回 **500**，日志有 `no such column: disabled` | 库结构没升级 | 用 2.0.1 及以上（`create_app()` 现在自己会迁移）；确认启动方式不是自己写的小脚本 |
| 旧密码登不上 | 1.x 是 bcrypt、2.0 是 scrypt | 正常现象 → 按 3.3 建新管理员 |
| 「历史/违规/缴费/电表」空，但概览有数据 | `last_room_id` 还没由抓取发布 | 点一次「刷新」或等一个周期（**不是数据丢失**） |
| 容器起来但外部访问不到 | 绑到了 127.0.0.1 | 容器必须 `GUNICORN_BIND=0.0.0.0:5000`（官方 compose 已设） |
| `Permission denied` 写 `/data` | 宿主目录属主不对 | `sudo chown -R 10001:10001 <数据目录>`；或改用命名卷 |
| 首页空白 / 资源 404 | 前端产物没构建 | Docker 镜像里已内置；裸机 `cd frontend && npm ci && npm run build` |
| 配置中心里凭据显示「解密失败」 | `FLASK_SECRET_KEY` 变了 | 把备份里的旧密钥放回去重启 |
| 抓取一直失败 | openid 过期 / 内网地址被 SSRF 拦 | 「管理 → 配置中心 → 测试连通」看具体错误 |
| 端口 5000 被占用 | 旧服务没停干净 | `ss -lntp \| grep :5000` 找出占用者 |

---

## 附录 C：红线清单（贴给执行者反复确认）

1. ❌ **不删、不覆盖 `records.db`**（只能复制 / 只读打开）
2. ❌ **不覆盖已有的 `.env`**（`FLASK_SECRET_KEY` 丢了 = 加密凭据永久解不开）
3. ❌ **阶段 3 验收全绿之前，不停旧服务**
4. ❌ **切换后不删旧容器 / 旧目录 / 旧 systemd 单元**（回滚要用）
5. ❌ **不跳过阶段 1 的备份**，且备份必须验证能打开
6. ❌ **不猜**：找不到文件、输出与预期不符、拿不准 —— 停下来报告

---

