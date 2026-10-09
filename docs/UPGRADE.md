# 升级与回滚

## 一、升级前（每次都要做，一分钟）

**第 0 件事：确认你的库能用**（不知道版本也没关系）：

```bash
python -m scripts.check_db /path/to/records.db --fix
# 只读体检 + 在副本上试一遍升级；原库一个字节都不会动
```

然后备份数据与密钥：

```bash
# 1. 备份数据 + 密钥（见 DEPLOY.md 第五节）
#    Docker：
docker run --rm -v starwatt-data:/data -v "$PWD:/backup" alpine \
  tar -czf /backup/starwatt-$(date +%F).tar.gz -C /data .
#    裸机：
sudo tar -czf ~/starwatt-$(date +%F).tar.gz \
  -C /var/lib/dorm-power-monitor records.db -C /opt/dorm-power-monitor .env

# 2. 记下当前版本（回滚时要它）
curl -fsS http://127.0.0.1:5000/healthz
# {"status":"ok","version":"2.0.0","time":"..."}
```

> 备份是为了回滚数据，**不是为了回滚代码** —— 2.0 的数据库结构向后兼容
> （Q9/Q10 零迁移），新旧代码可以读同一个 `records.db`。这正是「秒级回滚」
> 成立的前提。

## 二、升级

### Docker

```bash
docker compose pull                     # 拉新镜像
STARWATT_TAG=2.0.0-starcore docker compose up -d   # 或直接 up -d
docker compose ps                        # 看健康状态
```

`.env`（如果建过）与数据卷都**不受影响**。

### 离线包

```bash
tar -xzf starwatt-2.0.0-starcore-offline.tar.gz
cd starwatt-2.0.0-starcore-offline
docker load -i images/starwatt-2.0.0-starcore-amd64.tar.gz
docker compose up -d
```

### 裸机

```bash
cd /opt/dorm-power-monitor      # 或你的安装目录
sudo ./install.sh --upgrade
```

`--upgrade` 会：`git pull --ff-only` → 重建依赖 → 重建前端 →
**保留 `.env` 与数据** → 重启服务。它**永远不会**覆盖已存在的 `.env`
（里面的 `FLASK_SECRET_KEY` 丢了会让配置中心那 9 项加密凭据永久解不开）。

## 三、升级后验证（9 项）

```bash
# 1. 探活
curl -fsS http://127.0.0.1:5000/healthz
# 2. 首页是 SPA（不是空壳）
curl -fsS http://127.0.0.1:5000/ | grep -q 'id="app"' && echo OK
# 3. 进程/容器健康
docker compose ps          # 或 systemctl status dorm-web
```

界面上逐项确认：

4. 登录后首页四个板块都有数据（今日 / 本月 / 实时电表 / 趋势图）
5. 「管理 → 配置中心」能看到 9 个分组，值都在
6. 「管理 → 功能开关」显示 18 个开关及其真实状态
7. 「管理 → 日志」有效级别表正常，能改级别
8. 飞书群 / 私聊机器人各发一条命令，有回复
9. 等一个抓取周期（默认 10 分钟），首页数字**有更新**

> 9 项里最容易忽略的是第 9 项 —— 界面能开、但调度器没起来的情况
> 只有等一个周期才看得出来。看日志里的 `scheduler` 类别最直接。

## 四、回滚（30 秒演练）

### Docker：真的只要 30 秒

```bash
# 0. 记下旧版本号（上一步的 /healthz）
docker compose down                                  # ~3 秒
STARWATT_TAG=<旧版本> docker compose up -d            # ~10 秒（本地已有镜像）
curl -fsS http://127.0.0.1:5000/healthz              # 通 → 完成
```

数据库**不用动**：同一个 `records.db` 旧代码直接能读。

### 裸机：1–3 分钟（要重建前端）

```bash
cd /opt/dorm-power-monitor
sudo git fetch --tags
sudo git checkout <旧版本标签>          # 例如 v2.0.0-rc1
sudo .venv/bin/pip install -q -r requirements.txt
(cd frontend && sudo npm ci --no-fund --no-audit && sudo npm run build)
sudo systemctl restart dorm-web
```

> 想更快：平时就把 `static/` 目录备份一份（例如 `static.bak/`），
> 回滚时 `cp -a static.bak static` 跳过前端构建，可压到 30 秒内。

### 回滚失败怎么办

用第一节的备份恢复数据，然后回到**上一个已知可用的版本**。
最坏情况：清空数据目录重新走 OOBE（历史数据从备份里捞）。

## 五、数据库结构变更说明

* 2.0 与 1.x 的**表结构一致**，升级不需要迁移脚本，也不需要停机窗口。
* 后续版本如果加字段，走 `starwatt/db/migrations.py`，规则是**只增不删**
  （旧库能平滑升上来，新库也不怕旧代码读）。
* 升级过程中**不要**手动改表结构 —— 版本判断会因此失效。

## 六、升级常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 配置中心的凭据显示「解密失败」 | `FLASK_SECRET_KEY` 变了 | 把旧密钥填回 `.env`（或卷里的 `.flask_secret_key`）后重启 |
| 网页白屏、资源 404 | 前端产物没重建 | `cd frontend && npm ci && npm run build`，或用镜像部署 |
| 容器起来但外面连不上 | 绑到了 127.0.0.1 | 容器必须 `GUNICORN_BIND=0.0.0.0:5000`（compose 已设） |
| 启动即退出，日志有 `RuntimeError` | 多进程下调度器守卫触发 | 确认 `workers=1` 且用 `gunicorn -c gunicorn.conf.py web:app` |
| 升级后端口不通 | `.env` 里的 `FLASK_PORT` 与映射不一致 | 两边对齐后重启 |

更多问题见 [FAQ.md](FAQ.md)。
