# StarWatt 2.0 切换执行规范（服务器 Agent 的唯一依据）

> **这份文档是自包含的**：你（服务器上的 Agent）只需要读它，不需要向任何人追问
> 「交付物在哪 / 用 Docker 还是裸机 / 端口怎么安排」—— 这些**已经定好并写在
> [§1.4 已定决策](#14-已定决策不要再问)里**。
>
> 只有下面两种情况才停下来问人：
> 1. 输出与本文档写的**预期不符**；
> 2. 遇到本文档**没有覆盖**的情况。
>
> 其余情况按文档执行，每个阶段结束贴出报告。

---

## §0 执行契约（先读，全文适用）

### 0.1 你的任务

把这台服务器上的 **StarWatt 1.x** 升级为 **2.0.2**，**历史数据一条不丢**。

### 0.2 三条红线（任何阶段、任何理由都不得违反）

| # | 红线 | 为什么 |
|---|---|---|
| **R1** | **不得删除、覆盖、移动 `/opt/dorm-power-monitor/records.db`**。只允许：复制它、以只读方式打开它 | 那是全部历史数据 |
| **R2** | **不得覆盖 `/opt/dorm-power-monitor/.env`** | 里面的 `FLASK_SECRET_KEY` 用于加密配置中心的凭据；换掉它，那些密文**永久解不开** |
| **R3** | **阶段 3 验收全绿之前，不得停止/重启旧服务 `dorm-web.service`** | 旧服务是唯一在跑的东西，停了就没有可回退状态 |

### 0.3 行为准则

1. **只读优先**：能用只读命令验证的，就不要写。
2. **先备份再动手**：任何写操作之前，确认阶段 1 的备份存在且可打开。
3. **不猜**：找不到文件、命令报错、输出与预期不符 → **停下来报告**，不要换一种做法试。
4. **不扩大范围**：只做本文档写的操作。不要顺手升级系统、不要改 nginx 配置、
   不要动其他服务。
5. **不贴敏感值**：报告里**永远不要**贴 `.env` 的值、openid、密钥、密码明文。
   需要证明时贴**键名**或 sha256 前缀即可。
6. **每个命令都贴原始输出**（不要只写「已完成」）。

### 0.4 每阶段必须报告什么

固定四段：**① 做了什么（命令）② 看到了什么（原始输出）③ 与预期是否一致 ④ 结论/待决**。
各阶段的报告模板见 [附录 E](#附录-e-报告模板汇总)。

### 0.5 遇到这些情况必须停下来问人

* 输出与本文档的「预期」不一致
* 出现本文档没写的报错
* 发现实际环境与 [§1.3 环境基线](#13-环境基线已侦察过的事实) 不符
* 需要执行本文档之外的操作才能继续
* 任何一步让你不确定「会不会动到数据」

---

## §1 项目与本次任务

### 1.1 命名对照（**最容易搞混的一点**）

同一个项目有两个名字，服务器上看到的是旧名字：

| 你会看到 | 说明 |
|---|---|
| `dorm-power-monitor` | **1.x 时期的项目名**。目录 `/opt/dorm-power-monitor`、systemd 单元 `dorm-web.service`、`web.py` |
| `StarWatt` / `星瓦` | **2.0 起的项目名**（本任务的目标） |
| `starwatt` | 2.0 的 Python 包名（`import starwatt`）与容器镜像名 |
| `starcore` | 2.0 的版本代号（版本号形如 `2.0.2-starcore`） |

**结论：服务器上只有 `dorm-power-monitor`、完全没有 `starwatt`，这是正常的** ——
`/opt/dorm-power-monitor` 就是要升级的那个 1.x。**不要以为找错了对象。**

### 1.2 目标产物

> ⚠️ **版本提示（2026-10-09 审计后新增）**
>
> 本文件原本指向 `2.0.1`（见下方 Release 页）。该版本有一个**会让 F2/F3/F5 静默失败**的
> 缺陷（HTTP 客户端拒绝 JSON 数组 → 每日用电/违规/缴费三张表恒空，而抓取仍
> 报成功），以及电表「最后上报」恒为 `—`、概览「日均用量」算不出、充值后日均
> 变负数等问题。**修复已合并到 `main`，但不在 2.0.1 里。**
>
> **请用 `2.0.2-starcore`（或更新）执行切换**；若该 tag 尚未发布，先请维护者
> 发版 —— **不要用 2.0.1 切换**（数据不会丢，但上述几段会一直空着且不报错）。

* 项目主页：https://github.com/TSS-Small-sunshine/dorm-power-monitor （公开仓库）
* 目标版本：**2.0.2-starcore**（2.0.1 有上述缺陷，勿用）
* Docker 镜像：`ghcr.io/tss-small-sunshine/starwatt:2.0.2-starcore`
* 离线包（镜像拉不动时用）：Release 页的 `starwatt-2.0.2-starcore-amd64.tar.gz`
  https://github.com/TSS-Small-sunshine/dorm-power-monitor/releases
* 自检工具（在仓库 `scripts/` 下，镜像里也带着）：
  * `scripts/check_db.py` —— 数据库兼容性自检（**纯标准库**，宿主机 `python3` 可直接跑）
  * `scripts/cutover_check.py` —— 切换验收自检（需要 `requests` → **在容器里跑**）

### 1.3 环境基线（已侦察过的事实）

这些是**已经查明的**，不要再重复侦察；发现与下面不符就报告。

| 项 | 值 |
|---|---|
| 主机 | Ubuntu 24.04.2 LTS，`x86_64`（amd64） |
| Docker | 已安装（29.4.3），**没有任何 starwatt 容器** |
| 旧服务 | systemd 单元 `dorm-web.service`（enabled、active），用户 `www-data`，监听 `127.0.0.1:5000`，入口 `/opt/dorm-power-monitor/web.py` |
| 旧应用目录 | `/opt/dorm-power-monitor`（含 `records.db`、`.env`、多份历史备份） |
| 数据库 | `/opt/dorm-power-monitor/records.db`，**69632 字节** |
| 配置文件 | `/opt/dorm-power-monitor/.env`，**730 字节** |
| 反向代理 | nginx 占用 80/443，反代到后端 5000 |
| 已完成 | **阶段 1 备份已完成**：`/opt/deploy/backup-20261009-165444`（权限 750，含 `records.db` 与 `env.bak`），sha256 双向一致，`PRAGMA integrity_check` = ok |
| ⚠️ 该备份的**已知局限** | 它是在**旧服务仍在运行**时 `cp` 出来的。旧库是 **WAL 模式**（旧代码就是 WAL），`cp` 只复制主文件 → 可能丢掉还在 `records.db-wal` 里、尚未 checkpoint 的最近提交。**正式切换前必须重做一次**（见 §5.1 的 `.backup` 写法），并用行数确认它与源库一致 |
| 待补的只读事实 | `PRAGMA journal_mode`（确认是否 `wal`）、`records` 的**行数与 `MAX(ts)`**（"数据没丢"的基线） |
| 工作目录约定 | 本机已用 `/opt/deploy/` 放备份 → 后续也用 `/opt/deploy/` 放测试实例 |

### 1.4 已定决策（**不要再问**）

| 决策项 | 结论 | 理由 |
|---|---|---|
| 迁移对象 | `/opt/dorm-power-monitor` 这个 1.x | §1.1 |
| 部署形态 | **Docker**（不是裸机 venv+systemd） | 镜像预编译（不必装 Node/编译器）；回滚最干净（旧 systemd 单元原封不动）；本机 Docker 已装 |
| 影子实例端口 | **5001**（只监听回环） | 不碰 nginx、不碰旧服务 |
| 正式实例端口 | **沿用 5000** | nginx 的 `proxy_pass` **一行都不用改** |
| 数据布局 | **把库复制到 `/var/lib/dorm-power-monitor/`**，不要 chown 旧目录去复用 | 旧 `records.db` 全程一个字节不动 → 回滚零风险（无权限问题、无「旧代码能否容忍新列」的疑问） |
| 容器属主 | 数据目录必须 `chown 10001:10001` | 容器内是非 root（uid 10001），否则写不进去 |
| 升级还是重装 | **升级**（重装只在 [附录 A](#附录-a-重装判据) 的三种情况） | 保留可回退状态 |
| 执行脚本 | 用 `bash`（`sh` 不支持 `pipefail`） | 本机 `sh` 是 dash |

### 1.5 当前进度

| 阶段 | 状态 |
|---|---|
| Phase 0 侦察 | ✅ 完成（见 §1.3） |
| Phase 1 备份 | ✅ 完成（`/opt/deploy/backup-20261009-165444`） |
| Phase 2 数据库自检 | ⬜ 待执行（见 §3） |
| Phase 3 旁路起新版 | ⬜ 待执行（见 §4） |
| Phase 4 正式切换 | ⬜ 待执行（见 §5） |
| Phase 5 回滚演练 | ⬜ 待执行（见 §6） |
| Phase 6 交接报告 | ⬜ 待执行（见 §7） |

**下一步**：执行 Phase 2（§3）。它会给出**行数基线** —— 后续每一步都要拿它对账。

---

## §2 Phase 0：侦察（只读）

### 2.1 已完成部分

见 §1.3 环境基线。**不要重复执行**，除非发现与基线不符。

### 2.2 仍需补收的只读信息（**Phase 4 需要，请补做**）

```bash
bash -c '
echo "=== 1) 旧服务怎么起的（决定回滚命令）==="
systemctl cat dorm-web 2>/dev/null | head -40

echo "=== 2) 有没有 cron 在抓取（1.x 靠它，2.0 不需要 → Phase 4 要清理）==="
sudo crontab -l 2>/dev/null | grep -vE "^\s*#|^\s*$" || echo "(root 无 crontab)"
sudo crontab -u www-data -l 2>/dev/null | grep -vE "^\s*#|^\s*$" || echo "(www-data 无 crontab)"

echo "=== 3) 数据库行数与最后写入（★最重要：这是「数据没丢」的基线）==="
sudo sqlite3 "file:/opt/dorm-power-monitor/records.db?mode=ro" \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM records;
   SELECT COUNT(*) FROM daily_elec;
   SELECT COUNT(*) FROM users;"

echo "=== 4) nginx 反代与域名（决定切换后从哪个域名验证）==="
sudo grep -rnE "proxy_pass|server_name" /etc/nginx/sites-enabled/ 2>/dev/null | head -20

echo "=== 5) .env 的键名（★只要键名，不要值）==="
sudo grep -oE "^[A-Z_]+=" /opt/dorm-power-monitor/.env 2>/dev/null | tr -d "="

echo "=== 6) 应用目录结构（venv / 备份 / 代码）==="
sudo ls -la /opt/dorm-power-monitor | head -30
'
```

**报告**：贴原始输出（**`.env` 只贴键名**）。

**怎么算合格**：第 3 项必须给出 `records` 的具体行数与 `MAX(ts)`。
若 `MAX(ts)` 距现在超过 1 小时，说明旧服务其实没在正常抓取 —— 报告里注明。

---

## §3 Phase 2：数据库自检（不改原库）

**目的**：在动任何东西之前，确认这个库能不能被 2.0 直接用。

### 3.1 取一份 2.0 源码（只为拿自检工具，不安装依赖）

```bash
bash -c '
sudo mkdir -p /opt/deploy/starwatt-2.0
cd /opt/deploy/starwatt-2.0
sudo git clone --depth 1 https://github.com/TSS-Small-sunshine/dorm-power-monitor.git .
ls scripts/
'
```

**预期**：`scripts/` 里有 `check_db.py`、`cutover_check.py`、`ast_guard.py`、`secret_scan.py`。

> 如果 `github.com` 拉不动（国内常见），换加速前缀：
> `sudo git clone --depth 1 https://gh-proxy.com/https://github.com/TSS-Small-sunshine/dorm-power-monitor.git .`

### 3.2 跑自检

```bash
cd /opt/deploy/starwatt-2.0
python3 -m scripts.check_db /opt/dorm-power-monitor/records.db --fix
```

**说明**：`check_db` 只用 Python 标准库，宿主机 `python3` 直接能跑。
`--fix` 会在**副本**上真跑一遍升级并对比行数 —— **原库不会被修改**。

**预期输出**（关键行）：

```
schema_version：0（老库（没有 schema_version 记录：v1 基线或更早））
  ✅ records        N 行
  ⚠  users                        缺列 disabled, must_change_password
结论：✅ 可以直接用。启动时 init() 会自动补齐，**不动你的数据**
----------------------------------------------
在副本上跑了一遍 init()（原库未改动）
  schema_version：0 → 3
  补上的列：users.disabled、users.must_change_password
  行数对比（升级前 → 升级后）：
    ✅ records      N → N
结论：✅ 副本升级干净 —— 数据一行不少、结构已与当前代码一致。
```

### 3.3 判定规则

| 输出 | 含义 | 动作 |
|---|---|---|
| `✅ 可以直接用` + `副本升级干净` | 库与 2.0 兼容 | **继续 Phase 3** |
| `❌ 认不出这是本项目的库` | 文件不是本项目的库 | **停下报告**（可能找错文件） |
| `❌ 不是 SQLite 数据库（或已损坏）` | 文件坏了 | **停下报告**（用阶段 1 备份评估恢复） |
| 行数对比里出现 `❌` | 升级过程丢了行 | **停下报告**，不要继续 |

### 3.4 阶段报告

```
【Phase 2 · 数据库自检】
命令：python3 -m scripts.check_db /opt/dorm-power-monitor/records.db --fix
原始输出：<整段粘贴>
行数基线：records = ____ 行，daily_elec = ____ 行，users = ____ 行
结论：可直接用 / 需要人工介入（说明）
```

> ⚠️ 这一步之后原库**仍是旧的**（`--fix` 只动副本），这是正常的 ——
> 真正的升级发生在 Phase 3/4 启动新版本时。

---


## §4 Phase 3：旁路起新版（**旧服务全程不动**）

**目的**：用**副本**、在**另一个端口**把 2.0 跑起来并验收。旧服务、旧库、nginx
在这一阶段**完全不被触碰**。

### 4.1 拉取镜像

```bash
sudo docker pull ghcr.io/tss-small-sunshine/starwatt:2.0.2-starcore
```

**拉不动时走离线包**（离线包不依赖任何外网）：

```bash
# 在能上网的机器下载（或让人传给你）：
#   https://github.com/TSS-Small-sunshine/dorm-power-monitor/releases
#   → starwatt-2.0.2-starcore-amd64.tar.gz
sudo docker load -i starwatt-2.0.2-starcore-amd64.tar.gz
sudo docker images | grep starwatt      # 确认镜像已导入
```

### 4.2 生成容器环境文件（**关键：必须沿用旧的密钥**）

🔴 **这一步不做，配置中心里加密的凭据会永久解不开**（openid / 飞书 / QQ 密钥都是
用旧 `FLASK_SECRET_KEY` 加密存进库里的）。容器如果自己生成一把新密钥，
那些密文就再也读不出来了。

先生成一个可复用的脚本（它会自动从旧 `.env` 里找出密钥项并沿用）：

```bash
sudo tee /opt/deploy/starwatt-env.sh >/dev/null <<'SH'
#!/usr/bin/env bash
# 生成容器环境文件：路径指向容器内 /data，密钥沿用旧 .env 里那一项
set -euo pipefail
OUT="${1:?用法: starwatt-env.sh <输出文件>}"
OLD_ENV=/opt/dorm-power-monitor/.env

# 旧 .env 里的键名可能不叫 FLASK_SECRET_KEY，所以先找它、再退化到「含 SECRET 或 KEY」
SECRET="$(grep -E '^FLASK_SECRET_KEY=' "$OLD_ENV" | cut -d= -f2- || true)"
if [ -z "$SECRET" ]; then
  SECRET="$(grep -iE '^[A-Z_]*(SECRET|KEY)[A-Z_]*=' "$OLD_ENV" | head -1 | cut -d= -f2- || true)"
fi
if [ -z "$SECRET" ]; then
  echo "!! 旧 .env 里找不到任何含 SECRET/KEY 的项 —— 请报告，不要自己造一个" >&2
  exit 1
fi

cat > "$OUT" <<EOF
DORM_DATA_DIR=/data
DB_PATH=/data/records.db
FLASK_PORT=5000
GUNICORN_BIND=0.0.0.0:5000
FLASK_SECRET_KEY=$SECRET
EOF
chmod 600 "$OUT"
echo "已生成 $OUT（沿用旧密钥，权限 600，共 $(grep -c . "$OUT") 行）"
SH
sudo chmod +x /opt/deploy/starwatt-env.sh
```

### 4.3 影子数据目录（用副本，绝不用原库）

```bash
bash -c '
sudo mkdir -p /opt/deploy/starwatt-test
sudo chown -R 10001:10001 /opt/deploy/starwatt-test

# ⚠️ 不要用 cp 复制主文件：旧库是 **WAL 模式**，最近的提交可能还在
# records.db-wal 里没 checkpoint —— cp 只拿主文件，会静默丢掉它们。
# sqlite3 的 .backup 是**在线一致性快照**，WAL 感知，且不打扰正在运行的旧服务。
sudo sqlite3 /opt/dorm-power-monitor/records.db \
  ".backup '/opt/deploy/starwatt-test/records.db'"
sudo chown 10001:10001 /opt/deploy/starwatt-test/records.db

# 立刻比对行数（副本不得明显少于源库）
echo "副本 records 行数：$(sudo sqlite3 "file:/opt/deploy/starwatt-test/records.db?mode=ro" "SELECT COUNT(*) FROM records;")"
echo "源库 records 行数：$(sudo sqlite3 "file:/opt/dorm-power-monitor/records.db?mode=ro" "SELECT COUNT(*) FROM records;")"
sudo ls -la /opt/deploy/starwatt-test

# 生成影子实例的环境文件（沿用旧密钥）
sudo /opt/deploy/starwatt-env.sh /opt/deploy/starwatt-test.env
'
```

**预期**：`records.db` 属主 `10001`；两个行数**相同或源库只多出「复制之后刚抓的那一行」**
（旧服务仍在每 10 分钟抓一次）。**若副本少了几十上百行 → 停下报告**（说明复制丢数据）。
环境文件生成成功且**不含任何明文打印**。

### 4.4 起影子实例

```bash
sudo docker run -d --name starwatt-test \
  -p 127.0.0.1:5001:5000 \
  -v /opt/deploy/starwatt-test:/data \
  --env-file /opt/deploy/starwatt-test.env \
  ghcr.io/tss-small-sunshine/starwatt:2.0.2-starcore

sleep 8
sudo docker logs starwatt-test | tail -40
```

**预期日志（关键行）**：

```
[entrypoint] 复用数据卷里的 FLASK_SECRET_KEY
migration: applying v1 (baseline)
migration: applying v2 (users.disabled)
migration: applying v3 (users.must_change_password)
migration: schema_version 0 -> 3
Starting gunicorn ...
Listening at: http://0.0.0.0:5000
调度器已启动（pid=..，jobs=['l3_gate', 'scrape']）
```

> 第一行是「**复用**」而不是「已生成」—— 说明用的是你给的旧密钥 ✓。
> 如果看到「已生成 FLASK_SECRET_KEY」，说明 `--env-file` 没生效，**停下来报告**。

**如果旧 `.env` 里确实没有任何密钥项**（脚本会以 `!!` 退出）：
报告，并预期**需要重新填一遍凭据**（openid / 飞书 / QQ）—— 因为旧代码可能是用
硬编码默认密钥加密的，那种情况下谁都解不开。**历史数据本身不受影响**，
只是配置中心里那几项要重填。这一步不要自己编一个密钥。

**没有 Traceback** 才算通过。

### 4.5 建一个能登录的管理员（**必须做**）

1.x 的密码哈希是 bcrypt，2.0 换成 stdlib scrypt → **旧密码不再可用**：

```bash
sudo docker exec -i starwatt-test python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码Aa1!", role="admin")
print("created admin2")
PY
```

**密码要求**：≥8 位且同时含**大写字母、数字、特殊字符**，否则会被拒绝并说明原因。

### 4.6 验收

**4.6.1 密钥正确性（最重要的一项）**

```bash
sudo docker exec -i starwatt-test python - <<'PY'
from starwatt.db.connection import init
init()
from starwatt import config_registry as cfg
try:
    v = cfg.get_str("dorm_openid", "")
    print("✅ dorm_openid 解密成功，长度 =", len(v))
except Exception as exc:
    print("❌ dorm_openid 解密失败：", type(exc).__name__, exc)
    print("   → 说明 FLASK_SECRET_KEY 与旧库不一致，请报告，不要继续")
PY
```

**预期**：`✅ dorm_openid 解密成功，长度 = N`（N > 0）。
**如果报 ❌** → 停下来报告（继续下去会让用户丢掉所有凭据）。

**4.6.2 自动验收**

```bash
sudo docker exec starwatt-test python -m scripts.cutover_check \
  --base http://127.0.0.1:5000 \
  --user admin2 --password '你刚设的密码' \
  --db /data/records.db
```

> **为什么在容器里跑**：`cutover_check` 需要 `requests`，宿主机 `python3` 大概没装；
> 镜像自带。容器内回环 `127.0.0.1:5000` 就是那个实例本身。

**预期输出**：

```
结论：✅ N 项通过，0 项失败，M 项待观察
```

并且能看到**真实数字**：概览条数、电表电压/电流/功率、历史天数、违规条数、
配置中心 `94 项 / 9 组`、功能开关 `18 个`、日志 `7 级 × 8 类`、`schema_version=3`。

### 4.6 允许出现的「正常现象」（不是故障）

| 现象 | 解释 |
|---|---|
| `⚠️ 历史/违规/缴费 暂时为空` | 这几段按房间号过滤，房间号来自 state 键 `last_room_id`，**由成功的抓取发布**；旧库没有这个键。点一次首页「刷新」或等一个周期即可。**不是数据丢失** |
| `⚠️ 还没抓到过实时电表数据` | 同上（首次抓取成功后才有） |
| 影子实例里配置中心凭据显示「解密失败」 | **要报告**：说明影子实例用的 `FLASK_SECRET_KEY` 与旧 `.env` 不同 |

### 4.7 判定与报告

**全部 ✅ / 仅上述正常现象** → 报告并**等人确认后**再进 Phase 4。

```
【Phase 3 · 旁路验收】
镜像来源：ghcr.io 拉取 / 离线包导入
容器状态：sudo docker ps --filter name=starwatt-test
日志关键行：<migration 三行 + schema_version + gunicorn + 调度器>
管理员：admin2 已创建（是/否）
cutover_check 完整输出：<整段粘贴>
行数对比：原库 ____ 行 / 影子库 ____ 行（应一致）
结论：可以切换 / 有问题（说明）
```

> 🔴 **本阶段结束时不要删容器**。它要留到 Phase 4 之后。

---

## §5 Phase 4：正式切换

**前置条件（缺一不可）**：Phase 3 全绿；人已明确说「可以切换」。

### 5.1 停旧服务 + 清理 cron

```bash
bash -c '
# 备份 crontab（回滚要用）
sudo crontab -l > /opt/deploy/crontab-root-backup-$(date +%F).txt 2>/dev/null || true

# 停旧服务
sudo systemctl stop dorm-web
sudo systemctl status dorm-web --no-pager | head -5     # 预期：inactive (dead)

# 去掉与抓取相关的 cron（1.x 靠 cron，2.0 自带调度器，留着会重复抓）
sudo crontab -l 2>/dev/null | grep -v -iE "dorm|starwatt|power" | sudo crontab - || true

# 确认端口已释放
ss -lntp | grep :5000 || echo "(5000 已释放)"
'
```

### 5.2 准备正式数据目录（**旧库保持原样**）

```bash
bash -c '
sudo mkdir -p /var/lib/dorm-power-monitor
# 旧服务此时已停（§5.1）→ WAL 已在关闭时合并。这里仍用 .backup：
# 它不依赖「关闭时是否 checkpoint」，永远拿到一致性快照。
sudo sqlite3 /opt/dorm-power-monitor/records.db \
  ".backup '/var/lib/dorm-power-monitor/records.db'"
sudo chown -R 10001:10001 /var/lib/dorm-power-monitor
sudo ls -la /var/lib/dorm-power-monitor

# 生成正式实例的环境文件（沿用旧密钥）
sudo /opt/deploy/starwatt-env.sh /opt/deploy/starwatt.env

# 记录旧库的 sha256（切换后要核对它没被动过）
sudo sha256sum /opt/dorm-power-monitor/records.db
'
```

**为什么复制而不是复用旧目录**：旧 `records.db` 全程**一个字节都不动** →
回滚时旧服务起来就是原状态，既没有权限问题，也没有「旧代码能否容忍新列」的疑问。

### 5.3 起正式实例

🔴 **用 `docker run` 显式挂载，不要用 `docker compose up`。**
仓库里的 compose 默认用**命名卷** `starwatt-data`，那样你复制到
`/var/lib/dorm-power-monitor` 的库**不会被使用**，新实例会是一个**空库**
（看起来就像「历史数据全丢了」）。显式 `-v` 才指向正确的库。

```bash
sudo docker run -d --name starwatt \
  --restart unless-stopped \
  -p 127.0.0.1:5000:5000 \
  -v /var/lib/dorm-power-monitor:/data \
  --env-file /opt/deploy/starwatt.env \
  ghcr.io/tss-small-sunshine/starwatt:2.0.2-starcore

sleep 8
sudo docker ps --filter name=starwatt
sudo docker logs starwatt | tail -30
```

**参数说明（每条都必要）**

| 参数 | 作用 |
|---|---|
| `--restart unless-stopped` | 开机/崩溃后自动起来（替代旧 systemd 的 `enabled`） |
| `-p 127.0.0.1:5000:5000` | 沿用 5000 → **nginx 不用改** |
| `-v /var/lib/dorm-power-monitor:/data` | 指向**你复制的那份库**（不是命名卷） |
| `--env-file /opt/deploy/starwatt.env` | 带来**旧密钥**与容器内路径 |

**预期**：容器 `Up`，日志里是 `复用数据卷里的 FLASK_SECRET_KEY` + migration 三行 +
`schema_version 0 -> 3` + gunicorn + 调度器，**没有 Traceback**。

### 5.4 验证

```bash
bash -c '
echo "=== 1) 探活 ==="
curl -fsS http://127.0.0.1:5000/healthz

echo "=== 2) 密钥正确性（凭据能否解密）==="
sudo docker exec -i starwatt python - <<PY
from starwatt.db.connection import init
init()
from starwatt import config_registry as cfg
try:
    v = cfg.get_str("dorm_openid", "")
    print("✅ dorm_openid 解密成功，长度 =", len(v))
except Exception as exc:
    print("❌ 解密失败：", type(exc).__name__, exc)
PY

echo "=== 3) 自动验收 ==="
sudo docker exec starwatt python -m scripts.cutover_check \
  --base http://127.0.0.1:5000 --user admin2 --password "你的密码" \
  --db /data/records.db

echo "=== 4) 行数对账（应与 Phase 2 基线一致）==="
sudo sqlite3 "file:/var/lib/dorm-power-monitor/records.db?mode=ro" \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM records;"

echo "=== 5) 旧库确实没被动过（sha256 应与 5.2 记录一致）==="
sudo sha256sum /opt/dorm-power-monitor/records.db
'
```

**判定**：第 1 项 200；第 2 项 ✅；第 3 项 `0 项失败`；第 4 项行数与基线一致；
第 5 项 sha256 与 5.2 一致。**任何一项不符 → 停下报告。**

### 5.5 nginx 需要改吗

**不需要**。新旧都监听 `127.0.0.1:5000`，`proxy_pass` 一行都不用动。
从域名访问验证即可：

```bash
curl -fsSI https://<你 nginx 配置里的 server_name>/ | head -3
```

### 5.6 阶段报告

```
【Phase 4 · 正式切换】
旧服务：已停止（systemctl status 显示 inactive）
cron：已清理（备份在 /opt/deploy/crontab-root-backup-____.txt）
正式数据目录：/var/lib/dorm-power-monitor（属主 10001）
容器：docker compose ps 输出
自检：cutover_check 结论 = ____ 项通过 / ____ 项失败
行数对账：基线 ____ 行 → 现在 ____ 行（应一致）
旧库 sha256：____（与 5.2 记录一致：是/否）
域名访问：HTTP ____
结论：切换成功 / 需要回滚（说明）
```

> 🔴 切换后**不要删**：旧容器（若有）、`/opt/dorm-power-monitor` 目录、
> `dorm-web.service` 单元、旧 `records.db`。Phase 5 回滚要用。

---


## §6 Phase 5：回滚演练（**Phase 4 成功之后立刻做**）

**为什么放在切换之后**：切换前旧服务本来就在跑，演练没有意义。切换后立刻
真做一次「停新 → 起旧 → 验证 → 再切回新」，才能证明**回滚路径真的可用** ——
这是唯一能验证它的时机，而且代价只有几十秒。

### 6.1 演练步骤

```bash
bash -c '
echo "=== 1) 停新实例 ==="
sudo docker stop starwatt

echo "=== 2) 起旧服务（原样回来）==="
sudo systemctl start dorm-web
sleep 5
sudo systemctl status dorm-web --no-pager | head -5

echo "=== 3) 验证旧服务可用 ==="
curl -fsS -o /dev/null -w "旧服务 HTTP %{http_code}\n" http://127.0.0.1:5000/

echo "=== 4) 确认旧库仍是原样（sha256 应与 5.2 一致）==="
sudo sha256sum /opt/dorm-power-monitor/records.db
'
```

**预期**：旧服务 `active`、HTTP 200、sha256 与 5.2 记录一致。

### 6.2 切回新版

```bash
bash -c '
sudo systemctl stop dorm-web
sudo docker start starwatt          # 容器与参数都还在，直接启动
sleep 8
curl -fsS http://127.0.0.1:5000/healthz
'
```

**预期**：新实例恢复、`/healthz` 200。

### 6.3 如果演练中旧服务起不来

* **不要继续**。保持新版在跑（它至少是好的），把现象报告给人。
* 旧服务起不来的常见原因：cron 被清了但旧代码依赖它（不影响服务本身）、
  venv 损坏、端口被占。**报告现象，不要自行修复旧环境。**
* 数据没有风险：旧库 sha256 不变（§6.1 第 4 步可证）。

### 6.4 阶段报告

```
【Phase 5 · 回滚演练】
停新 → 起旧：旧服务状态 = active / 失败（说明）
旧服务 HTTP：____
旧库 sha256：____（与 5.2 一致：是/否）
切回新版：/healthz = ____
演练结论：回滚路径可用 / 不可用（说明）
```

---

## §7 Phase 6：交接报告（最终，贴给人）

```bash
bash -c '
echo "=== 最终状态 ==="
sudo docker ps --filter name=starwatt --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
sudo systemctl is-active dorm-web || true
sudo sqlite3 "file:/var/lib/dorm-power-monitor/records.db?mode=ro" \
  "SELECT COUNT(*) FROM records; SELECT COUNT(*) FROM users;"
curl -fsS http://127.0.0.1:5000/healthz
'
```

```
【StarWatt 2.0 切换完成报告】
版本：1.x（dorm-power-monitor）→ 2.0.2-starcore
部署形态：Docker（容器名 starwatt，端口 5000）
数据目录：/var/lib/dorm-power-monitor/records.db
行数对账：切换前 ____ 行 → 切换后 ____ 行（一致：是/否）
旧库保护：/opt/dorm-power-monitor/records.db sha256 = ____（未改动：是/否）
备份：/opt/deploy/backup-20261009-165444（已另存一份：是/否）
自检：cutover_check = ____ 项通过 / ____ 项失败
回滚演练：可用 / 不可用
旧环境保留：dorm-web.service 单元、/opt/dorm-power-monitor、旧 records.db 均未删除
nginx：未改动（仍是 127.0.0.1:5000）

★ 仍需人工确认（脚本无法代替，请人在浏览器/飞书里点）：
  □ 用 admin2 登录，首登是否直接跳到改密页
  □ 飞书私聊逐个发这 9 个命令，都有回复：
    /状态 /剩余 /电表 /电表状态 /今日 /历史 /缴费 /违规 /帮助
  □ 群里收到过一张卡片（或「配置中心 → 测试连通」成功）
  □ 等一个抓取周期（默认 10 分钟）后，首页「最后更新」时间会变

异常与遗留：____
```

---

## 附录 A：重装判据

**默认答案是「不要重装」** —— 升级路径更安全（旧环境原封不动，随时能回滚）。
只有下面三种情况才重装：

1. 旧环境**无法确定**怎么起的（进程/服务/cron 都对不上，找不到启动脚本）
2. 旧代码目录已被删，或依赖坏到修不了
3. 系统盘要重做 / 换机器

**重装也必须保住数据**：

```bash
bash -c '
# 1) 停掉旧的一切（服务 / cron）
sudo systemctl stop dorm-web 2>/dev/null || true
sudo crontab -l 2>/dev/null | grep -v -iE "dorm|starwatt|power" | sudo crontab - || true

# 2) 数据目录：把备份里的库放进去
sudo mkdir -p /var/lib/dorm-power-monitor
# ⚠️ 这个备份是「旧服务运行期间 cp」出来的，可能缺最近几行（WAL 未 checkpoint）。
# 优先用 §5.2 重做过的那一份（切换前的 .backup 快照）；两者都比没有强。
sudo cp -a /opt/deploy/backup-20261009-165444/records.db /var/lib/dorm-power-monitor/
sudo chown -R 10001:10001 /var/lib/dorm-power-monitor

# 3) 密钥：★必须用原来那把（换了它，加密凭据永久解不开）
sudo cp -a /opt/deploy/backup-20261009-165444/env.bak /opt/deploy/starwatt-2.0/.env
sudo chmod 600 /opt/deploy/starwatt-2.0/.env

# 4) 起新版（用显式挂载，别用 compose 的命名卷）
sudo docker run -d --name starwatt \
  --restart unless-stopped \
  -p 127.0.0.1:5000:5000 \
  -v /var/lib/dorm-power-monitor:/data \
  --env-file /opt/deploy/starwatt.env \
  ghcr.io/tss-small-sunshine/starwatt:2.0.2-starcore
'
```

> 重装时也要先按 §4.2 生成 `starwatt.env`（它会把**备份里的旧密钥**带进来）。
> 若旧 `.env` 已丢失，就用 §1.3 备份目录里的 `env.bak`：
> `sudo cp -a /opt/deploy/backup-20261009-165444/env.bak /opt/dorm-power-monitor/.env`
> 之后再跑生成脚本。

**重装后必做**：建管理员（§4.4）、跑 `cutover_check`（§4.5）、核对行数（§5.4）。

---

## 附录 B：错误对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| 登录返回 **500**，日志 `no such column: disabled` | 库结构没升级 | 确认用的是 **2.0.1** 镜像（`create_app()` 会自己迁移）；确认不是自己写脚本启动的 |
| 旧密码登不上 | 1.x 用 bcrypt，2.0 用 scrypt | **正常现象** → 按 §4.4 建新管理员 |
| 「历史/违规/缴费/电表」空但概览有数据 | `last_room_id` 尚未由抓取发布 | 点「刷新」或等一个周期（**不是数据丢失**） |
| 「违规/缴费/每日用电」**长期**空，日志里有 `返回的 JSON 不是对象` | 2.0.1 的 HTTP 客户端只接受 JSON **对象**，而 F2/F3/F5 返回**数组** → 三个端点每次都失败（`ok=True` 仍报成功） | **升到 2.0.2+**（修复已合并）；`cutover_check` 的 `/api/daily`、`/api/payments` 两栏会标红 |
| 备份/副本比源库**少几行** | 旧库是 WAL 模式，`cp` 只复制主文件，丢掉未 checkpoint 的提交 | 用 `sqlite3 <源库> ".backup '<目标>'"` 重做（§4.3 / §5.2） |
| `Permission denied` 写 `/data` | 宿主目录属主不对 | `sudo chown -R 10001:10001 <数据目录>` |
| 容器起来但外部访问不到 | 绑到了 127.0.0.1 | 需要 `-e GUNICORN_BIND=0.0.0.0:5000`（compose 已设） |
| 首页空白 / 资源 404 | 前端产物没进镜像 | 用官方镜像；裸机则 `cd frontend && npm ci && npm run build` |
| 配置中心凭据显示「解密失败」 | `FLASK_SECRET_KEY` 变了 | 用备份的 `env.bak` / `.flask_secret_key` 恢复后重启 |
| 抓取一直失败 | openid 过期 / 内网被 SSRF 拦 | 「管理 → 配置中心 → 测试连通」看具体错误 |
| 端口 5000 被占用 | 旧服务没停干净 | `ss -lntp \| grep :5000` 找占用者；`systemctl stop dorm-web` |
| **新实例起来后历史数据是空的** | 用了 `docker compose up`（命名卷），没用 `-v` 挂载你复制的那份库 | 按 §5.3 用显式 `-v /var/lib/dorm-power-monitor:/data` 重建容器 |
| 配置中心凭据显示「解密失败」 | 容器没拿到旧的 `FLASK_SECRET_KEY` | 确认 `--env-file` 生效（日志应为「**复用**数据卷里的 FLASK_SECRET_KEY」）；用 §4.6.1 检查 |
| `python3 -m scripts.cutover_check` 报 `No module named requests` | 宿主机没装 requests | **在容器里跑**（§4.6.2 / §5.4） |

---

## 附录 C：红线清单（每阶段开始前默读一遍）

1. ❌ **不删、不覆盖、不移动** `/opt/dorm-power-monitor/records.db`（只可复制 / 只读打开）
2. ❌ **不覆盖** `/opt/dorm-power-monitor/.env`
3. ❌ **Phase 3 全绿前不停旧服务** `dorm-web.service`
4. ❌ **Phase 4 之后不删**旧容器 / `/opt/dorm-power-monitor` / `dorm-web.service` / 旧库
5. ❌ **不跳过 Phase 1 备份**（已完成，勿删 `/opt/deploy/backup-20261009-165444`）
6. ❌ **不贴敏感值**：`.env` 值、openid、密钥、密码
7. ❌ **不猜**：输出不符 / 找不到文件 / 拿不准 → 停下来报告

---

## 附录 D：命令速查

```bash
# 探活
curl -fsS http://127.0.0.1:5000/healthz

# 容器状态与日志
sudo docker ps --filter name=starwatt --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
sudo docker logs starwatt | tail -40
sudo docker logs starwatt-test | tail -40

# 数据库（只读）
sudo sqlite3 "file:/var/lib/dorm-power-monitor/records.db?mode=ro" \
  "SELECT COUNT(*), MIN(ts), MAX(ts) FROM records;"

# 文件指纹（证明没被动过）
sudo sha256sum /opt/dorm-power-monitor/records.db

# 服务
sudo systemctl status dorm-web --no-pager
sudo systemctl start dorm-web / stop dorm-web

# 自检
python3 -m scripts.check_db /opt/dorm-power-monitor/records.db --fix          # 宿主机
sudo docker exec starwatt python -m scripts.cutover_check \
  --base http://127.0.0.1:5000 --user admin2 --password '<密码>' --db /data/records.db

# 容器生命周期（正式实例名 starwatt）
sudo docker stop starwatt / start starwatt / restart starwatt
sudo docker rm -f starwatt-test        # 仅在全部验收通过、且人确认后才删
```

---

## 附录 E：报告模板汇总

每阶段结束按这个格式贴（**贴原始输出，不要只写「已完成」**）：

```
【Phase N · <阶段名>】
① 做了什么：<关键命令>
② 看到了什么：<原始输出>
③ 与预期是否一致：一致 / 不一致（差在哪）
④ 结论 / 待决：<结论> 或 <需要人确认的问题>
```

各阶段的专用模板分别在：§2（Phase 0 补收）、§3.4、§4.7、§5.6、§6.4、§7。

---

**文档版本**：随 StarWatt 2.0.2-starcore 发布；如与服务器实际不符，以报告为准并停下来问人。

