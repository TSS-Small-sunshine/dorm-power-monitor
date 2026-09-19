# 技术栈 / 方案 / 安全性总览

> 本文档对应 commit HEAD = R42（zip SHA256 `7af026f9...`，23 文件 / 14.5 MiB）。
> 最后更新：2026-09-16。

---

## 1. 技术栈

### 1.1 语言 / 运行时

| 项目 | 版本 | 备注 |
|---|---|---|
| Python | 3.12（推荐 ≥ 3.10）| Ubuntu 24.10 自带；Windows 也可 |
| SQLite | 3.x | 单文件 `/var/lib/dorm-power-monitor/dorm.db` |
| Node | — | **未使用**（dashboard 静态 HTML，Chart.js 内嵌 CDN 加载） |

### 1.2 核心依赖（`requirements.txt`）

| 包 | 最低版本 | 用途 |
|---|---|---|
| `flask` | 3.0+ | HTTP 服务（dashboard + JSON API + 飞书事件回调）|
| `gunicorn` | 21.0+ | 生产 WSGI（实际部署走 systemd + `python web.py`，gunicorn 仅作兼容）|
| `requests` | 2.31+ | 学校 HTTP 调用 + 飞书 webhook POST |
| `pycryptodome` | 3.18+ | 飞书事件 AES-256-CBC 解密 + HMAC 签名 |
| `pillow` | 10.0+ | 飞书 stat card 静态图（MiSans + NotoEmoji 字体渲染）|
| `python-dotenv` | 1.0+ | `.env` 加载（`config.py:25`）|

**已移除的依赖**：`beautifulsoup4`（R21 审计后从代码库移除，全部改用 `re` 解析）。

### 1.3 前端栈

| 项 | 来源 | 备注 |
|---|---|---|
| HTML5 / CSS3 / Vanilla JS | 内嵌在 `web.py` `INDEX_HTML` / `OOBE_HTML` / `ADMIN_HTML` 常量 | 单文件 SPA，无 build step |
| Chart.js | jsDelivr CDN (`@2`) | 30 秒轮询 `/api/live` + `/api/data` |
| 字体（dashboard） | 内嵌 `/assets/fonts/MiSans-{Regular,Bold}.ttf` + `NotoEmoji-Regular.ttf` | OFL / Apache-2.0 |

### 1.4 部署栈

| 组件 | 角色 |
|---|---|
| **systemd** (`deploy/dorm-web.service`) | Flask 后台进程 + hardening（`PrivateTmp=true`、`ProtectSystem=full`、`NoNewPrivileges=true` 等）|
| **cron** (`deploy/dorm-cron.txt`) | 两个 entry：① `*/10 * * * *` 抓取 + L1/L2 推送；② `* * * * *` L3 cadence gate（tick）|
| **Nginx** (`nginx/dorm.conf`) | 反代 (`127.0.0.1:5000`)，gzip，TLS 推荐 Let's Encrypt，可选 HTTP Basic |
| **logrotate** (`deploy/dorm-power-monitor.logrotate`) | 日志每日轮转，保留 7 天 |

### 1.5 第三方平台

| 平台 | 用途 | 凭据位置 |
|---|---|---|
| 学校门户 `ybhqcz.fjny.edu.cn` | 抓电量 / 日用 / 违规 / 表状态 / 缴费 5 端点 | `.env` `DORM_OPENID`（密码级） |
| 飞书开发者平台 | 自定义 webhook bot（推卡片）+ Developer Platform App bot（接事件回调）| `.env` `FEISHU_APP_ID` / `APP_SECRET` / `VERIFICATION_TOKEN` / `ENCRYPT_KEY` + `FEISHU_WEBHOOK` |

---

## 2. 方案

### 2.1 系统架构图

```
              ┌─────────────────────────────────────┐
              │   Nginx (TLS optional)             │
              │   school.tssplus.top → :5000       │
              └──────────────┬──────────────────────┘
                             │ /api/live  /api/data  /api/refresh
                             │ /admin/*  /admin/oobe  /feishu/event
                             ▼
┌──────────────────────────────────────────────────────────────┐
│  Flask (web.py)  ── systemd dorm-web.service                 │
│  ├─ /api/live      ── 30s 轮询，dashboard 渲染               │
│  ├─ /api/data      ── 历史趋势 + 日用曲线                  │
│  ├─ /api/refresh   ── X-Internal-Token 守卫，触发抓取     │
│  ├─ /admin/oobe    ── 6 步首次配置向导                    │
│  ├─ /admin         ── Admin UI（HTTP Basic + OOBE 跳过后）│
│  └─ /feishu/event  ── 飞书 bot 事件回调（AES + HMAC）     │
│                                                               │
│  SQLite (db.py)  ── dorm.db                                 │
│  ├─ meta             (key-value: last_room_id / eqprice /    │
│  │                    push_daily_time / oobe_completed /     │
│  │                    last_daily_elect_at / backfill_done ...)│
│  ├─ records         (ts, read_time, remain)                 │
│  ├─ daily_elec      (roomId, dt, total_eq, esbm, eebm, zong_eq)│
│  ├─ violations      (roomId, dt, wg_reason, wg_power)       │
│  ├─ pay             (roomId, dt, kind, amount)              │
│  └─ run_status      (roomId, dt, vol, cur, yggl, meterNo)    │
└──────────────────────────────────────────────────────────────┘
                             ▲
                             │ db.insert / db.record_*
                             │
┌──────────────────────────────────────────────────────────────┐
│  Cron */10 ── flock /var/lock/dorm-scrape.lock                │
│  └─ dorm_power.py run_once (fetch_only=False)                │
│      ├─ F1  getEmRealRead    ── 实时剩余 / 推卡片主数据     │
│      ├─ F1b selectRecord     ── 30 天 backfill (one-shot)    │
│      ├─ F2  getEmDayElectQuery (daily, 24h 节流)             │
│      ├─ F3  selectWgElect     (hourly, 违规检查)             │
│      ├─ F4  getEmRunStatus    ── 表状态 / 离线检测           │
│      └─ F5  getEmPayQuery     ── 缴费历史 (24h, 学期锚定)   │
│      └─ L1 低电 / L2 整点 / L3 日报-周报-月报 推送         │
│                                                               │
│  Cron * * * * * ── flock /var/lock/dorm-tick.lock             │
│  └─ dorm_power.py --tick                                      │
│      └─ 仅跑 L3 cadence gate，不抓学校                       │
└──────────────────────────────────────────────────────────────┘
                             │
                             │ POST webhook (signed card)
                             ▼
                  ┌──────────────────────┐
                  │  Feishu group chat   │
                  │  接收卡片 + 响应命令  │
                  └──────────────────────┘
```

### 2.2 学校端 5 个数据源（`dorm_power.py:206-215`）

| F | Endpoint | 用途 | 节流 |
|---|---|---|---|
| **F1** | `/dormEmRealRead/getEmRealRead` | 实时电量（`remainEq`, `useEq`, `freeEq` 等 9 字段）| 每次抓取 |
| **F1b** | `/dormEmQuery/selectRecord` | backfill：30 天历史 `useEq` 累积值 | 一次性（`meta.backfill_done='1'`） |
| **F2** | `/dormEmDayElectQuery/getEmDayElectQuery` | 每日用电（`esbm`, `eebm` 累积，`useEq` delta）| `_F2_INTERVAL_SEC = 86400`（24h）|
| **F3** | `/dormEmWgQuery/selectWgElect` | 违规事件（`wg_reason`, `wg_power`）| `_F3_INTERVAL_SEC = 3600`（1h）|
| **F4** | `/dormEmRunStatus/getEmRunStatus` | 表状态（`vol`, `cur`, `yggl`, `runStatus`, `meterNo`）| 每次抓取 |
| **F5** | `/dormEmPayQuery/getEmPayQuery` | 缴费历史（`feeType=0` 全部 / `1` 缴费 / `-1` 退费）| `_F5_INTERVAL_SEC = 86400`；日期锚定 9/7（学期起点）|

**认证**：所有 endpoint 走 `?openid=<openid>` query string，User-Agent 伪装成微信 iOS 客户端，Referer 指向 `/dormEmRealRead/finduser`。

### 2.3 推送层级（`dorm_power.py:2208-2238`）

| 层级 | 触发 | 内容 | 节流 |
|---|---|---|---|
| **L0** | 实时 | API 调用方触发，无推送 | — |
| **L1** 低电警报 | 每次 scrape | `remainEq < THRESHOLD` 时推红卡 + 7 天均值 | `meta.last_low_battery_alert_at` dedupe |
| **L2** 整点播报 | 整点 (BJ 0-23) | ⏰ 整点 stat card（剩余 + 日均 + 月底预计） | `meta.last_scrape_status=ok` 时推 |
| **L3** 长报 | cron `* * * * *` tick | 日报（每天 09:00 BJ）+ 周报（周一 09:00）+ 月报（1 号 09:00）| `last_daily_report_date` / `last_weekly_report_iso` / `last_monthly_report_mo` dedupe |
| **L4** 离线检测 | 抓取成功 + F4 `run_status != 0` | 红色「⚠ 电表离线」卡 | 每次 scrape |
| **stale** | scrape failed 兜底 | 黄色「⚠ 数据陈旧」卡（用 fallback body）| 每次 scrape 失败 |

### 2.4 双重 cron + 双重 lock（`deploy/dorm-cron.txt`）

```
*/10 * * * *   flock -n /var/lock/dorm-scrape.lock   dorm_power.py
*  * * * *    flock -n /var/lock/dorm-tick.lock    dorm_power.py --tick
```

**为什么两个 cron**：
- `*/10` 抓取 + L1/L2 推送（重型，每次 ~10s）
- `* * * * *` L3 cadence gate（轻量，~ms 级，只判断时间戳）

**flock `-n`（non-blocking）**：两个 cron 都用不同 lock 文件，可以并行跑；如果前一次还没结束（理论不会，scrape 最多 10s），新 cron 直接 skip。

### 2.5 OOBE Wizard（`web.py` 内嵌 `OOBE_HTML`）

6 步流程（用户SQL skip绕过）：
1. 欢迎页 → 2. 学校 URL 导入（自动解析 `roomId` + `openid` + `eqprice`） → 3. 飞书 webhook → 4. 推送偏好（L1/L2/L3 enable + 时间）→ 5. 测试推送（`raise_on_error=True`）→ 6. 完成

**安全保护（R37）**：
- B1 step1 按钮永远卡死的 bug：改 `goNext()` 替代 `location.reload()`
- B2 test push 静默吞异常：加 `raise_on_error` kwarg
- B4 step 2/3 缺失必填字段 → server 返回 400 + 错误 toast
- B5 SSRF：host 白名单 + IP 字面量拒绝 + `allow_redirects=False`
- B8 SQL skip 后 `/admin` 死锁：新增 `/admin/set-password` 绕过认证

### 2.6 仪表盘卡片（`web.py:_build_round2_context`）

5 个 stat card + 2 个趋势图：
- 剩余电量（`remainEq`）
- 过去一小时耗电（`hourly_used`，差值算法，≥3500s）
- 抄表时间（`read_time` fallback `ts`）
- 日均耗电（窗口期 168h）
- **本月预计电费**（`used_kwh × eqprice + avg_daily × days_left × eqprice`，R41 算法用 `last_zong - first_zong`）

---

## 3. 安全性

### 3.1 凭据分层

| 凭据 | 位置 | 泄露影响 |
|---|---|---|
| `DORM_OPENID`（学校）| `.env`，`chmod 600` | 学校侧踢号 → 重新抓 DevTools |
| `FEISHU_APP_SECRET`（飞书 App）| `.env` | 假冒 bot → 在飞书开发者平台重置 |
| `FEISHU_VERIFICATION_TOKEN`（飞书事件验证）| `.env` | 伪造事件 → 在飞书开发者平台重置 |
| `FEISHU_ENCRYPT_KEY`（飞书事件解密）| `.env` | 解密用户消息 → 在飞书开发者平台重置 |
| `FEISHU_WEBHOOK` URL | `.env` | 往群里发假消息 → 重新创建机器人 |
| `ADMIN_PASSWORD`（web.py admin）| `.env` 或 `meta.admin_password` | 改 scraper 配置 → 重设 |

**当前状态**：以上 6 个 secrets 全部泄露到本会话 chat buffer。**已知但不阻塞功能**；TODO #23 待用户手动轮换。

### 3.2 系统级 hardening（`deploy/dorm-web.service`）

| Directive | 防护作用 |
|---|---|
| `User=www-data` / `Group=www-data` | 进程非 root（避免容器逃逸）|
| `NoNewPrivileges=true` | 禁止 setuid / setgid 提权 |
| `ProtectSystem=full` | `/usr`, `/boot`, `/efi` 只读 |
| `ProtectHome=true` | `/home`, `/root`, `/run/user` 不可访问 |
| `PrivateTmp=true` | `/tmp` 隔离（防止 /tmp 信息 泄露）|
| `PrivateDevices=true` | 除 null, full, zero, random, urandom 外不可访问 |
| `ProtectKernelTunables=true` | `/proc/sys`, `/sys` 只读 |
| `ProtectKernelModules=true` | 禁止加载内核模块 |
| `ProtectKernelLogs=true` | 禁止读 kernel ring buffer |
| `ReadWritePaths=` | 仅允许写入项目目录 + DB 目录（详见 service 文件）|
| `Restart=always` / `RestartSec=5` | crash 后 5 秒自动重启 |

### 3.3 Web 层认证

| 路径 | 认证 | 备注 |
|---|---|---|
| `/` `/api/live` `/api/data` | **无认证** | 个人 dashboard，假设 localhost + HTTPS |
| `/api/refresh` | `_check_internal_token` | `meta.api_internal_token` 或 `env.API_INTERNAL_TOKEN`；为空则跳过（个人 dashboard 默认关闭）|
| `/admin/*` | `_admin_required` HTTP Basic | `admin` + `meta.admin_password` 或 `env.ADMIN_PASSWORD`；`oobe_completed != '1'` 时 OOBE 阶段 bypass |
| `/admin/set-password` | **无认证**（R37 B8 修复）| SQL skip OOBE 后 bootstrap admin password 用 |
| `/admin/oobe` | **无认证** | OOBE 阶段任何时候都允许访问 |
| `/feishu/event` | 飞书签名验证（`_verify_lark_signature`）| v2 + url_verification 严格；v1 兼容 legacy fail-open |

### 3.4 SSRF 防护（R37 B5，`web.py:_parse_school_url`）

```python
# 4 层防御
1. host 白名单：target_host == config.DORM_BASE_URL.hostname
2. IP 字面量拒绝：ipaddress.ip_address(target).is_global == False
3. scheme 白名单：http/https only
4. 拒绝 follow redirect：requests.get(..., allow_redirects=False)
```

**目的**：阻止 admin 误粘贴 `http://169.254.169.254/`（AWS metadata）或 `http://127.0.0.1:6379/`（redis）触发 SSRF。

### 3.5 SQLite 数据隔离

- DB 单文件 `/var/lib/dorm-power-monitor/dorm.db`，不暴露网络接口
- 测试用 in-memory `file:test_roundXX_db_N?mode=memory&cache=shared`，不污染生产 DB
- 进程崩溃时不丢数据：sqlite3 默认 WAL mode（`db.py:_init_db` 显式设置）

### 3.6 日志与审计

- 日志输出 `/opt/dorm-power-monitor/dorm.log`（Round 34D 加 `/var/log/dorm-power-monitor.log` via logrotate）
- 保留 7 天 daily rotate + copytruncate
- 日志中**绝不打印** `DORM_OPENID` / `FEISHU_*` / `WEBHOOK` 完整值（仅前 6 + 后 4 mask）
- exception 链保留原始 `RequestException` 在 `__cause__`，但 logger 输出走 sanitized message

### 3.7 部署期（`deploy/`、`nginx/`、`setup.sh`）

- `setup.sh` 自动建 `.venv` + `pip install -r requirements.txt` + 创建 systemd unit
- 部署用 `unzip -o` 覆盖（不留 stale 文件），`.env` 从 backup 恢复，`chown -R www-data:www-data`
- **硬规则（README）**：**永远不用 `cp -n`** 升级；用 `unzip -o` + `.env` 恢复
- Nginx 反代 + gzip + 1 MB 上限 + 建议 TLS（Let's Encrypt）
- 可选 HTTP Basic（注释启用）

### 3.8 已知安全风险 + 缓解

| 风险 | 状态 | 缓解 |
|---|---|---|
| 6 个 secrets 泄露 chat buffer | **未修复**（TODO #23）| 用户手动轮换 |
| `/admin/set-password` 无认证（R37 B8）| 设计 trade-off | OOBE 设过密码后 `meta.admin_password != ""` → 自动 302 到 `/admin` 走 HTTP Basic |
| `_check_internal_token` 默认关闭 | 个人 dashboard | nginx 反代 + 路径隐藏 + 不暴露端口 |
| Cron 配置 `/tmp/dorm-cron.txt` 含 hardcoded `flock` | 设计如此 | 用户按 README 操作 |

---

## 4. 部署运维速查

### 4.1 关键路径

```
/opt/dorm-power-monitor/         ← Flask + scraper 项目
  ├── web.py                     ← Flask 主进程
  ├── dorm_power.py              ← scraper / 卡片生成
  ├── feishu_bot.py              ← 飞书 bot 命令处理
  ├── db.py                      ← SQLite 操作
  ├── config.py                  ← env 加载 + lazy getter
  ├── .env                       ← 凭据（chmod 600）
  └── assets/fonts/              ← Pillow 用字体

/var/lib/dorm-power-monitor/
  └── dorm.db                    ← SQLite 单文件

/var/log/dorm-power-monitor.log  ← logrotate 目标
/opt/dorm-power-monitor/dorm.log ← 主日志
```

### 4.2 日常运维命令

```bash
# 健康检查
curl -fsS http://127.0.0.1:5000/healthz && echo

# 看最近抓取状态
sudo sqlite3 -header -column /var/lib/dorm-power-monitor/dorm.db \
  "SELECT key, value FROM meta WHERE key IN ('last_scrape_at','last_scrape_status','last_room_id');"

# 手动触发抓取（测试）
cd /opt/dorm-power-monitor
sudo -u www-data .venv/bin/python dorm_power.py 2>&1 | tail -10

# 看 cron 是否在跑
systemctl list-timers --all | grep dorm

# 重启 web
sudo systemctl restart dorm-web
```

### 4.3 升级 zip 流程（标准 7 步）

```bash
1. cp -r /opt/dorm-power-monitor /opt/dorm-power-monitor.bak.<旧版本>
2. sha256sum <新 zip>     # 期望值在 zip 构建脚本输出里
3. cd /opt && unzip -o /tmp/dorm-power-monitor.zip
4. cp /opt/dorm-power-monitor.bak.<旧版本>/.env /opt/dorm-power-monitor/.env
5. chown -R www-data:www-data /opt/dorm-power-monitor/
6. find /opt/dorm-power-monitor -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
   systemctl restart dorm-web; sleep 3
7. curl -fsS http://127.0.0.1:5000/healthz && echo
```

---

## 5. 测试覆盖

| Round | 测试文件 | 用例数 | 覆盖 |
|---|---|---|---|
| R34 | `test_round34a/b/c/d.py` | 106 | scraper 5F + L1/L2/L3 + db + config + web 集成 |
| R35 | `test_round35.py` | 17 | admin roomId 字段映射 + lazy getter + UI 美化 |
| R36 | `test_round36.py` | 9 | db.insert 2 参数调用方 |
| R37 | `test_round37.py` | 25 | OOBE B1-B5+B8 修复 + 前端文案 + admin 路由 |
| R39 | `test_round39.py` | 9 | backfill robustness + monthly_projection UI |
| R41 | `test_round41.py` | 9 | _compute_monthly_projection 月内累计表码 vs 日均 |
| R42 | `test_round42.py` | 10 | _read_eqprice fallback chain |

**总计**：185 用例，**仅 server 上跑**（按用户偏好不跑用户设备测试）。

---

## 6. 已知遗留项（低优先级）

- **TODO #23**：轮换飞书 4 secrets + 2 openid（chat buffer 泄露）
- **TODO #38**：cron 配置 `/var/lock/dorm-scrape.lock` 在用户本地可能有 stale 文件
- **TODO #39**：`/api/refresh` 默认无 token 保护（个人 dashboard 设计如此）
- **TODO #40**：cron 自动清理 `meta.last_*_alert_at` dedupe keys（当前无 TTL，靠手动）
- **TODO #41**：Nginx TLS 没自动续期（需要 certbot timer 或外部监控）

---

**Last updated**: 2026-09-16 (R42 zip SHA256 `7af026f9eb68ae3b69dab7e1733c62ae14626b38426a0b11af73a29c6ae70833`)