# 常见问题（FAQ）

## 账号与登录

### 我手上那个 `records.db` 是哪一版？能直接用吗

不需要知道版本 —— 让工具告诉你（在**新代码**目录里跑）：

```bash
python -m scripts.check_db /path/to/records.db          # 只读体检
python -m scripts.check_db /path/to/records.db --fix    # 在副本上试升级

# Docker 用户（镜像里带着脚本）
docker compose exec starwatt python -m scripts.check_db /data/records.db
```

它会报告：`schema_version`、缺哪些表/列、每张表多少行，以及结论
（能不能直接用、启动时会自动补什么）。**它不会改你的库**：默认只读打开，
`--fix` 也是先复制再动。

从 1.x 到 2.0，唯一的结构差异是 `users` 表多 2 列
（`disabled`、`must_change_password`），启动时自动补 —— 数据一行不动。
完整流程见 [M7_CUTOVER.md](M7_CUTOVER.md#第-0-步先确认你的数据库能用1-分钟)。

### 忘记管理员密码了怎么办

**故意没有**「网页上输入用户名就重置」的入口 —— 这个系统没有短信/邮件通道，
那种入口等于把后门挂在公网上。请在**服务器上**执行：

```bash
# Docker
docker compose exec starwatt python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码", role="admin")
print("已创建新管理员 admin2，登录后在「管理 → 用户」里处理旧账号")
PY

# 裸机
cd /opt/dorm-power-monitor && sudo -u www-data .venv/bin/python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码", role="admin")
PY
```

密码强度要求：≥8 位，且**同时**含大写字母、数字、特殊字符
（`!@#$%^&*()-_=+[]{};:'",.<>/?\|`~` 之一）。

### 为什么一登录就让我改密码

这是有意的（Q19）：初始密码是安装时随机生成并**只打印一次**的，
首次登录强制改密，避免有人装完就一直用那串打印在终端里的密码。

### 登录提示「密码错误」但我确定没输错

连续失败 5 次会锁定 15 分钟（可在「配置中心 → 认证」调整
`lockout_threshold` / `lockout_minutes`）。锁定期间即使密码正确也会拒绝。
另外系统时间错乱会导致会话异常，见下面的「时间不对」。

---

## 数据抓取

### 首页一直显示「暂未抓到数据」

按顺序排查：

1. **配置中心 → 数据采集 → 测试连通**（`POST /api/admin/config/test`）——
   它会用当前配置真的去请求一次学校接口，把错误直接告诉你。
2. **openid 过期了**：学校系统的 openid 通常几周到几个月就失效，重新抓一次
   浏览器请求里的 `openid` 填进去即可。
3. **房间号**：`dorm_room_id` 必须是学校系统里的房间标识，不是门牌号文字。
4. **接口地址**：不同学校/校区路径不同（`dorm_base_url`）。
5. **内网地址被拦**：如果学校接口在内网 IP 上，需要打开
   「允许访问内网地址」（`allow_private_hosts`）—— 这会跳过 SSRF 防护的
   L2/L3 检查，**确认你信任那个地址**再开。
6. **看日志**：把「管理 → 日志」里的 `scrape` 类别调成 `DEBUG`，再触发一次抓取。

### 抓取会不会被学校发现 / 封号

默认抓取间隔 10 分钟（`scrape_interval_sec`），且 F2/F3/F5 这类重接口另有
**节流**（默认 24h / 1h / 24h），比人手动刷新网页的频率还低。
不建议把间隔调到 1 分钟以内。

### 数据会不会因为重装而丢

`records.db` 是普通 SQLite 文件，表结构从 1.x 到 2.0 **没有变**
（Q9/Q10 的零迁移），拷过去就能用。但**加密的凭据需要同一个
`FLASK_SECRET_KEY`**，见下面的「换服务器」。

---

## 推送与机器人

### 飞书群收不到卡片

1. 「管理 → 功能开关」看**群推送总开关**与对应的**告警层**是否都开着 ——
   页面会显示抑制原因（父开关关闭 / 静默时段）。
2. 静默时段（默认 23:00–07:00）只压 **L1 低电** 与 **L2 摘要**，
   日报周报不受影响；如果你在 23:30 等 L1，那就是被静默了。
3. webhook 与签名密钥：飞书群里「添加机器人 → 自定义机器人」时如果开了
   **签名校验**，必须把密钥填进 `feishu_secret`，否则会被飞书拒收（401）。
4. 用「管理 → 配置中心 → 测试连通」或手动触发一次推送验证。

### 私聊机器人不回我

1. 需要填齐：`feishu_app_id` / `feishu_app_secret`（拿 token）、
   `feishu_verification_token`（校验事件来源）、
   `feishu_encrypt_key`（若开放平台开了加密）。
2. 飞书开放平台的**事件订阅地址**要填 `https://你的域名/feishu/event`
   —— 必须是公网可访问的 HTTPS，飞书不接受自签证书。
3. 「管理 → 功能开关」里 **私聊机器人总开关** 与对应 **命令开关** 都要开。
4. 群里必须 **@机器人** 才会响应（这是设计如此，避免刷屏）；私聊直接发命令。

### QQ 机器人不工作

QQ 走的是官方机器人平台：填 `qq_app_id` / `qq_app_secret` / `qq_bot_secret`，
事件地址填 `https://你的域名/qq/events`。
注意 QQ 侧的签名校验更严格，**时间偏差超过几分钟就会验签失败**。

### 能不能再加个通知渠道（微信 / 邮件 / 钉钉）

可以，这是设计好的扩展点：实现一个 `Notifier` 再注册一行即可，
见 [EXTENDING.md 第 2 节](EXTENDING.md#2-加一个通知渠道微信邮件2-个文件)。

---

## 部署与运行

### 换服务器 / 迁移数据

要带走的只有两样：

1. 数据目录里的 `records.db`
2. **密钥**：Docker 是卷里的 `.flask_secret_key`；裸机是 `.env` 里的
   `FLASK_SECRET_KEY`

只带数据库不带密钥 → 首页电量能看，但配置中心里那 9 项加密凭据会变成
「解密失败」，需要重新填一遍。

### 升级后所有凭据都「解密失败」

说明 `FLASK_SECRET_KEY` 变了（重新生成了 `.env` / 换了卷）。
把旧密钥填回去重启即可恢复。**这也是为什么升级时绝不能覆盖 `.env`**
（`install.sh --upgrade` 已经保证这一点）。

### 切换/升级后，「历史 / 违规 / 缴费 / 电表」是空的，数据丢了吗

**没丢。** 这四段按**房间号**过滤，而房间号来自 state 键 `last_room_id` ——
它由**成功的抓取**发布（学校接口返回的房间号，不是你填的那个）。
旧库里没有这个键，所以新版本第一次启动时还不知道房间。

处理：**点一次首页的「刷新」，或等一个抓取周期（默认 10 分钟）**，
旧数据会自动出现。

验证数据确实还在（直接数库里的行）：

```bash
sqlite3 /var/lib/dorm-power-monitor/records.db \
  "SELECT COUNT(*) FROM records; SELECT COUNT(*) FROM daily_elec;"
```

概览（剩余电量 / 趋势图）不按房间过滤，所以它**立刻**就有数据 ——
如果连概览都是空的，那才是真的没抓到过。

### 时间不对 / 日报时间错乱

系统统一用 `Asia/Shanghai`。容器里 `tzdata` 已随 Python 依赖一起装上
（`python:*-slim` 镜像本身没有时区数据）。宿主机如果时间漂移，
会影响飞书签名与 QQ 验签，请开 NTP：`sudo timedatectl set-ntp true`。

### 端口 5000 被占用

```bash
# Docker：改宿主机映射端口
FLASK_PORT=8080 docker compose up -d
# 裸机：改 .env 里的 FLASK_PORT 后重启
sudo systemctl restart dorm-web
```

### 网页打开是空白页 / 资源 404

`static/` 产物不在仓库里，需要构建过：

```bash
cd frontend && npm ci && npm run build     # 产物落到 ../static
```

Docker 镜像里已经构建好了；裸机安装如果当时没装 Node，
用 `sudo ./install.sh --upgrade` 再跑一次（或先 `apt install nodejs npm`）。

### 日志太多 / 太少

「管理 → 日志」：全局级别默认 `INFO`。想安静一点就调到 `WARNING`；
排查问题就把具体类别（如 `scrape`）调到 `DEBUG`，只影响那一类。
文件日志默认**关闭**（避免无脑占盘），打开后可设保留天数（默认 7 天）。

### 一个实例能给多个宿舍用吗

设计上是**每个宿舍自己搭一套**（Q2 确认过），不是多租户：
一个实例对应一个房间（`dorm_room_id`）。想给隔壁宿舍用，让他们照
[DEPLOY.md](DEPLOY.md) 自己装一套 —— 两分钟的事。

### 能一键部署到 Vercel / Netlify / EdgeOne 吗

**前端能，后端不能。**

* 前端（SPA）可以托管在这三家，配置已经在仓库里（`vercel.json` /
  `netlify.toml` / `edgeone.json`），反代 `/api` 到你的后端即可，后端一行不用改
* **后端跑不了**：它需要①持久可写磁盘（SQLite）②常驻进程（调度器每 10 分钟抓一次）
  ③单实例；serverless 三样都不提供
* EdgeOne 还有额外限制：它的重写**只对静态资源有效**，不能反代外部后端

想要「一键部署全部功能」，用支持容器 + 持久卷的平台（Fly.io / Render 的配置也
已经备好），或者干脆用自己的机器。完整对比与逐步操作见
[DEPLOY_PAAS.md](DEPLOY_PAAS.md)。

> ⚠️ 最容易踩的坑：某些平台的免费档**闲置会休眠**。实例一停，调度器就停了 ——
> 界面看着正常，数据却是几小时前的，而且**不会有任何报错**。

### 树莓派 / armv7 能跑吗

能，但走 Docker（镜像预编译好 `pillow` / `pycryptodome`，用户不用编译）。
裸机脚本只支持 amd64 / arm64。若 armv7 镜像在 CI 里构建超时（QEMU 下很慢，
RK5 已预期），Release 里仍会提供 amd64/arm64 的镜像与离线包。

### 怎么完全关掉某个告警

两级都能关：**层开关**（例如「L3 日报」）关掉就是不发；
**总开关**关掉则整类不发。关掉后行为是**静默降级**（记一条 NOTICE），
不会报错、也不会在界面上变成红色告警。

---

## 还有问题

1. 先看日志：「管理 → 日志」调 `DEBUG`，或 `journalctl -u dorm-web -f` /
   `docker compose logs -f starwatt`
2. 再查 [DEPLOY.md](DEPLOY.md) 的上线检查清单与 [CONFIG.md](CONFIG.md) 的配置说明
3. 都不行就带上下面的信息提 Issue：版本（`/healthz` 返回的 version）、
   部署方式、复现步骤、相关日志片段（**记得把 openid 等敏感值涂掉**）
