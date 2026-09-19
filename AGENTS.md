# AGENTS.md — dorm-power-monitor

## 项目入口

- **主入口**：`web.py`（Flask 仪表盘 + Admin API + Feishu webhook）
- **抓取器**：`dorm_power.py`（cron 10 分钟触发）
- **机器人**：`feishu_bot.py`（被 `web.py` `/feishu/event` 路由调用）
- **数据库**：`db.py`（SQLite,6 张表 + 元数据 KV）
- **配置**：`config.py`（中心化读 `.env`）

## 核心模块

| 模块 | 职责 |
|------|------|
| `config.py` | 中心化配置：读 `.env`、暴露 `FEISHU_*` / `DORM_*` 常量 |
| `db.py` | SQLite 助手：6 张表（records/meter/finance/violation/login_attempts/meta）+ UNIQUE(ts) + INSERT OR REPLACE |
| `dorm_power.py` | 抓取器：F1/F2/F3/F4/F5 fetcher + L1/L2/L3 push |
| `feishu_bot.py` | 飞书私聊机器人：8 个 slash 命令 + 7 种卡片（PNG 渲染 + 上传） |
| `web.py` | Flask：仪表盘 4 段（overview/history/finance/meter）+ OOBE 6 步向导 + Admin HTTP Basic + session bootstrap + Login（`/admin/set-password` recovery）|

## 部署环境

- **服务器**：Ubuntu 24.10 VPS `iZn4acqrskey97uuyo31m1Z`（阿里云）
- **公网域名**：`school.tssplus.top`
- **运行栈**：systemd（`deploy/dorm-web.service`）+ nginx（`nginx/dorm.conf`）+ cron（`deploy/dorm-cron.txt`，每 10 分钟抓一次）+ logrotate（`deploy/dorm-power-monitor.logrotate`）
- **Python**：3.12（推荐 3.10+）

## 测试

- **modern 测试**（git 入库）：`tests/modern/test_round35..50.py`，AST guard 静态校验
- **legacy 测试**（git 忽略）：`tests/legacy/test_round5..34d.py`，运行时单测保留以备回溯
- **手动跑 modern**：
  ```bash
  python -m py_compile config.py db.py web.py feishu_bot.py dorm_power.py
  python -c "import ast; [ast.parse(open(f).read()) for f in __import__('glob').glob('tests/modern/*.py')]"
  ```

## 开发硬约束（适用所有 contributor / agent）

1. **不要 SSH 到生产服务器** `iZn4acqrskey97uuyo31m1Z`——所有运维操作走本地脚本 + 部署脚本（`scripts/deploy/r4*_deploy.sh`）。
2. **不要 `systemctl restart/start/stop`**——仅维护单元和 cron 配置，不在开发机直接触发。
3. **不要 `curl localhost:5000`**——开发机环境不全，单元测试以 AST guard 为主，运行时验证留给用户在生产环境跑。
4. **不要硬编码密钥**——所有 Feishu App Secret / Webhook / DORM_OPENID 走 `.env`（`.env` 已在 `.gitignore`）。
5. **不要 commit `tests/legacy/`、`_tmp_*`、`*.zip`、`records.db`、`.venv/`**——已在 `.gitignore`。
6. **不要改 `web.py` / `db.py` 的 SQLite schema 字段名**——下游抓取器、Feishu 渲染、Admin API 都依赖稳定字段名。
7. **不要往 commit message 里加 R60+ 之外的 Claude / 工具水印**——保持纯净 release 标记。

## 文档占位（R60 不写具体内容）

详细文档待 R61+ 补齐：

- `docs/ARCHITECTURE.md` —— 详细架构图、数据流、模块依赖关系（R61）
- `docs/DEPLOY.md` —— 完整部署手册（systemd / nginx / cron / logrotate / 反向代理 SSL）（R62）
- `docs/SECURITY.md` —— 凭据管理、`.env` 权限、`.gitignore` 保护、openid 轮换流程（R63）
- `docs/OPERATIONS.md` —— 故障排查 SOP、日志位置、备份恢复（R64）

当前可用文档：

- `docs/TECHNICAL.md` —— 技术细节（数据库 schema + fetch 流程 + 安全策略）
- `docs/Round25_dorm_id_research.md` —— R25 dorm_id 反查记录
- `docs/reports/` —— 各 round 工作记录（r41 / r42）

## Round → Branch 约定

| Round | 范围 | Branch |
|-------|------|--------|
| R51c | 当前 baseline（已 push main） | `main` |
| R60 | GitHub 仓库初始化 + v1.0.0-r51c-baseline release | `main` |
| R61-R67 | 后续功能开发 | `develop` |

## CI / CD

- `.github/workflows/ci.yml` —— push / PR 到 `main` / `develop` 触发 AST guard + py_compile + secret scan
- 不跑实际 build / runtime 测试（用户机器环境不全，留给生产验证）