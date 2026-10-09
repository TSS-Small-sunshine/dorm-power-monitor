## 安装

### Docker（推荐）

```bash
curl -fsSLO https://raw.githubusercontent.com/TSS-Small-sunshine/dorm-power-monitor/v2.0.0-starcore/docker-compose.yml
STARWATT_TAG=2.0.0-starcore docker compose up -d
docker compose logs starwatt | grep 初始密码   # 只打印一次
```

打开 <http://127.0.0.1:5000> ，用 `admin` + 上面的密码登录，
会被**强制改密**。

### 离线安装（断网机器 / 拉镜像慢）

下载 `starwatt-2.0.0-starcore-offline.tar.gz`（内含 amd64 + arm64 镜像与文档）：

```bash
tar -xzf starwatt-2.0.0-starcore-offline.tar.gz
cd starwatt-2.0.0-starcore-offline && cat 离线安装.txt
```

### 裸机（Debian / Ubuntu，amd64 / arm64）

```bash
git clone https://github.com/TSS-Small-sunshine/dorm-power-monitor.git
cd dorm-power-monitor && git checkout v2.0.0-starcore
sudo ./install.sh
```

## 产物

| 文件 | 说明 |
|---|---|
| `starwatt-2.0.0-starcore-amd64.tar.gz` | amd64 镜像，`docker load` 即用 |
| `starwatt-2.0.0-starcore-arm64.tar.gz` | arm64 镜像，`docker load` 即用 |
| `starwatt-2.0.0-starcore-offline.tar.gz` | 离线包：**上面两个镜像** + compose + 文档 + install.sh |

镜像 `ghcr.io/tss-small-sunshine/starwatt:2.0.0-starcore` 是多架构 manifest（amd64 / arm64；armv7 是单独的
`2.0.0-starcore-armv7` 标签）。离线包在 CI 里**真起过容器**：load → run →
`/healthz` 通 → 首页是 SPA → 日志里有首启随机密码。
