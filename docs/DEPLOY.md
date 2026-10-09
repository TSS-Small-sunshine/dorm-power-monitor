# 部署指南

三种路径，按你的环境选一种：

| 路径 | 适合 | 需要 | 耗时 |
|---|---|---|---|
| [Docker](#一docker推荐) | 所有人（含 armv7 / 群晖 / 软路由） | Docker + Compose | ~2 分钟 |
| [离线包](#二离线安装断网机器) | 拉镜像慢、机器不通外网 | Docker | ~3 分钟 |
| [裸机脚本](#三裸机安装debianubuntu) | 想在宿主机上直接跑（amd64/arm64） | root + Python 3.10+ | ~5 分钟 |

装完都是同一个流程：**取初始密码 → 登录 → 被强制改密 → 走 OOBE 向导**。

---

## 一、Docker（推荐）

```bash
# 1. 拉 compose 文件（也可以直接从 Release 下载）
curl -fsSLO https://raw.githubusercontent.com/TSS-Small-sunshine/dorm-power-monitor/main/docker-compose.yml

# 2. 起服务（不用写 .env，变量都有默认值）
docker compose up -d

# 3. 取初始密码（**只打印这一次**）
docker compose logs starwatt | grep 初始密码
```

> **镜像从哪来？** compose 里同时写了 `image:` 与 `build:`：
> * 本地没有那个镜像 → **现场构建**（第一次约 3–8 分钟，需要能访问 npm / PyPI）
> * 本地已有（或你 `docker compose pull` 过）→ 直接用，不重新构建
>
> 所以即使某个 tag 在 registry 上不存在，这条命令也能跑通。
> 想只看构建过程：`docker compose build`；想强制用发布版：`docker compose pull`。

**已发布的镜像**（ghcr.io，公开可匿名拉取）：

| Tag | 含义 |
|---|---|
| `2.0.0-starcore` | 具体版本（compose 的默认值，**推荐固定用它**） |
| `2.0.0` | 同一主次版本的最新补丁 |
| `latest` | 最新稳定版 |
| `2.0.0-starcore-armv7` | armv7（32 位 ARM）专用 |

```bash
docker pull ghcr.io/tss-small-sunshine/starwatt:2.0.0-starcore
```

多架构清单覆盖 `linux/amd64`、`linux/arm64`、`linux/arm/v7` —— `docker pull`
会按你的机器自动选。

打开 <http://127.0.0.1:5000>，用 `admin` + 上面的密码登录。

**指定版本**：

```bash
STARWATT_TAG=2.0.0-starcore docker compose up -d
```

**直接暴露到局域网**（默认只绑 127.0.0.1，前面应挂 nginx）：

```bash
BIND_ADDR=0.0.0.0 docker compose up -d
```

### 数据放在哪

默认是命名卷 `starwatt-data`（Docker 会自动处理属主，不需要 chown）：

```bash
docker volume inspect starwatt-data
```

想用宿主目录（例如放到 NAS 的共享盘），把 compose 里的

```yaml
      - starwatt-data:/data
```

换成

```yaml
      - ./data:/data
```

⚠️ 换成宿主目录后要自己给权限，容器里是 **非 root（uid 10001）**：

```bash
sudo mkdir -p ./data && sudo chown -R 10001:10001 ./data
```

### 从旧版本迁移现有数据（零迁移）

老版本的 `records.db` 可以直接用 —— 表结构没变。**先自检一下**（1 分钟）：

```bash
python -m scripts.check_db /path/to/old/records.db --fix
# 它会在副本上试一遍升级，报告数据是否一行不少；不会动你的原库
```

然后：

```bash
sudo mkdir -p ./data
sudo cp /path/to/old/records.db ./data/records.db
sudo chown -R 10001:10001 ./data
# compose 里改成 ./data:/data 后
docker compose up -d
```

> 迁移后**历史数据、图表、日报都还在**。唯一变化：老的管理员密码不再适用
> （密码哈希算法换了），用 `BOOTSTRAP_ADMIN_PASSWORD` 建一个新管理员，
> 或走「忘记密码」流程。

---

## 二、离线安装（断网机器）

Release 里有两个包：

| 文件 | 内容 |
|---|---|
| `starwatt-<版本>-amd64.tar.gz` | 只有镜像（amd64） |
| `starwatt-<版本>-offline.tar.gz` | amd64 + arm64 镜像 + compose + 文档 + install.sh |

```bash
tar -xzf starwatt-2.0.0-starcore-offline.tar.gz
cd starwatt-2.0.0-starcore-offline
cat 离线安装.txt          # 步骤都写在这个文件里

# 看架构
uname -m                  # x86_64 → amd64 包；aarch64 → arm64 包

docker load -i images/starwatt-2.0.0-starcore-amd64.tar.gz
docker compose up -d
docker compose logs starwatt | grep 初始密码
```

离线包在 CI 里**真起过容器**（`docker load` → `docker run` → `/healthz` 通 →
首页是 SPA → 日志里有初始密码横幅），所以「导入即用」是验证过的。

---

## 三、裸机安装（Debian/Ubuntu）

```bash
git clone https://github.com/TSS-Small-sunshine/dorm-power-monitor.git
cd dorm-power-monitor
sudo ./install.sh
```

脚本会做：检查 Python ≥ 3.10 → 建 venv 装依赖 → 构建前端 → 生成 `.env`
（含随机 `FLASK_SECRET_KEY` 与随机初始密码）→ 迁移已有 `records.db` →
按实际路径生成 systemd 单元 → 启动服务 → **打印初始密码一次**。

| 选项 | 作用 |
|---|---|
| `--upgrade` | 升级：拉代码 + 重建依赖与前端，**保留 `.env` 与数据** |
| `--no-frontend` | 跳过前端构建（只有 API，或之后再补） |
| `--dir PATH` | 安装目录（默认 `/opt/dorm-power-monitor`） |
| `--data-dir PATH` | 数据目录（默认 `/var/lib/dorm-power-monitor`） |
| `--user NAME` | 运行服务的系统用户（默认 `www-data`） |
| `--port N` | 监听端口（默认 5000，仅绑 127.0.0.1） |

常用命令：

```bash
systemctl status dorm-web
journalctl -u dorm-web -f          # 实时日志
sudo systemctl restart dorm-web
```

> **armv7（树莓派 2/Zero 等）**：请走 Docker。裸机脚本只支持 amd64 / arm64
> —— armv7 上 `pillow` / `pycryptodome` 需要本地编译，脚本不替你装工具链。
> 如果 armv7 镜像构建失败（RK5 已预期，QEMU 下很慢），Release 里仍会有
> amd64/arm64 的镜像与离线包。

---

## 四、放到公网：nginx + HTTPS

服务本身只监听 `127.0.0.1:5000`（Docker 与裸机都是），由 nginx 做 TLS 与转发。

```nginx
server {
    listen 443 ssl http2;
    server_name dorm.example.com;

    ssl_certificate     /etc/letsencrypt/live/dorm.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/dorm.example.com/privkey.pem;

    # 图片卡片、日志导出可能有点大
    client_max_body_size 8m;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_http_version 1.1;

        # ⚠️ 这三个头必须传：审计日志要记录真实 IP，
        #    而 Host 影响 cookie 的域判断
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_read_timeout 120s;
    }
}

server {
    listen 80;
    server_name dorm.example.com;
    return 301 https://$host$request_uri;
}
```

证书用 certbot：`sudo certbot --nginx -d dorm.example.com`。

> ⚠️ **别把服务直接暴露到公网**。它设计成「宿舍内网 / 个人小规模」使用，
> 没有做暴力破解之外的限流与 WAF；要公网访问请务必上 HTTPS + nginx。

---

## 五、备份与恢复

要备份的只有**两样**（其余都是可重建的）：

| 对象 | 位置（Docker） | 位置（裸机） |
|---|---|---|
| 数据库 | 卷 `starwatt-data` 里的 `records.db` | `$DORM_DATA_DIR/records.db` |
| 会话/加密密钥 | 卷里的 `.flask_secret_key` | `.env` 里的 `FLASK_SECRET_KEY` |

```bash
# Docker：整卷备份（最简单）
docker run --rm -v starwatt-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/starwatt-backup-$(date +%F).tar.gz -C /data .

# 裸机
sudo tar -czf starwatt-backup-$(date +%F).tar.gz \
  -C /var/lib/dorm-power-monitor records.db \
  -C /opt/dorm-power-monitor .env
```

> 🔴 **密钥比数据更关键**。`FLASK_SECRET_KEY` 同时用于加密配置中心里的
> 凭据（openid / 飞书 / QQ 密钥）。丢了以后：数据库还能读，但那些**密文
> 永久解不开**，只能重新填一遍凭据。
> 备份脚本请把密钥一起存，并且**分开存放**（别只存在同一台机器上）。

恢复：把 `records.db` 放回数据目录、密钥放回原处，重启即可。

---

## 六、上线前检查清单

- [ ] `curl -fsS http://127.0.0.1:5000/healthz` 返回 `{"status":"ok",...}`
- [ ] 网页能打开，`admin` 登录后被**强制改密**
- [ ] OOBE 向导填完 openid，首页出现真实电量
- [ ] 「管理 → 配置中心 → 测试连通」通过
- [ ] 飞书群机器人收到一张测试卡片
- [ ] 私聊机器人 `/状态` 有回复
- [ ] 「管理 → 功能开关」里把不需要的告警层关掉（默认 L3 日报是开的）
- [ ] 「管理 → 日志」确认级别（默认 INFO），必要时开文件日志
- [ ] nginx + HTTPS 生效，`http` 会 301 到 `https`
- [ ] 备份任务已设置（见上一节），并**验证过一次恢复**
- [ ] 防火墙只开 80/443，5000 端口不对外

## 七、卸载

```bash
# Docker
docker compose down          # 数据保留在卷里
docker volume rm starwatt-data   # 连数据一起删（不可恢复）

# 裸机
sudo systemctl disable --now dorm-web
sudo rm /etc/systemd/system/dorm-web.service && sudo systemctl daemon-reload
sudo rm -rf /opt/dorm-power-monitor /var/lib/dorm-power-monitor
```
