# StarWatt 审计整改与交付说明

> 时间：2026-10-11 · 来源：`docs/audit/security.md`（19 条）、`docs/audit/usability.md`（24 条）、`docs/audit/stability.md`（20 条），共 63 条。
> 本文只记**状态与决策**，逐条证据在各自的审计报告里。
> 本次整改对应的发布版本：**v2.0.3-starcore**。

## 一、验收基线（调度者独立复跑，不是子代理自述）

| 检查 | 命令 | 结果 |
|---|---|---|
| 后端全量测试 | `.\.venv\Scripts\python.exe -m pytest -o addopts='' -p no:cacheprovider` | **1707 passed，exit 0**（整改前基线 1564 用例 / 1588 通过） |
| 前端类型检查 | `cd frontend && npm run typecheck` | exit 0（`Dockerfile` 的 `npm run build` 内含这一步，是 CI 的硬门） |
| 前端生产构建 | `cd frontend && npm run build` | exit 0（产物落 `static/`，已被 `.gitignore` 忽略，不入库） |
| 凭据扫描守卫 | `scripts.secret_scan.scan()` | `[]`（无命中） |

> `-o addopts=''` 是必需的：仓库 `addopts` 自带 `-q`，命令行再加 `-q` 等于 `-qq`，pytest 在 `-qq` 下**不打印统计尾行**（容易被误读成「没结果」）。

## 二、已修（已全部落地，共 12 项）

| # | 缺陷 | 关键改动 | 行为变化 |
|---|---|---|---|
| 1 | roomId 自动发现永远拿不到房间号（裸 `finduser` 返回 `value=""`） | `starwatt/scraper/client.py:81-113` `finduser_query()`、`:116-131` `fetch_finduser_page()`；调用点 `starwatt/services/setup_service.py:215`、`starwatt/scraper/service.py:541`；`service.py:206` 对已含 `openid=` 的路径幂等 | 发现请求改为 `…/finduser?openid=<百分号编码>`；openid 为空或全空白时**一个请求都不发**（防止 `?openid=` 空值拿到**别人的**房间号写进本机配置）；`Referer` 仍用不带查询串的路径（学校 WAF 校验它） |
| 2 | OOBE 走完回不到主页，且不刷新永远出不去 | `frontend/src/stores/auth.ts:96`：`oobeCompleted !== null` → `=== true` | 「已完成」是唯一可缓存的正向终态；`false` 每次导航都重新问服务端。改前：守卫把首次读到的 `completed:false` 永久缓存，服务端已置 true 也照旧把用户弹回 `/oobe` |
| 3 | 飞书告警「假成功」（HTTP 200 + 业务 `code≠0`） | `starwatt/notify/transport.py:95-130` `_json_error()`、`:196-209` 业务码判定 | `code`（兼容 `StatusCode`/`status_code`）非 0 或响应体不是合法 JSON → 记失败并打印业务码与 `msg`（原始响应截断 200 字符），不再打印「飞书推送成功」；`raise_on_error=True` 时抛 `requests.HTTPError`（与 HTTP 错误同一异常族） |
| 4 | 数据只涨不删：清理函数无生产调用者、无 WAL/VACUUM 回收 | `starwatt/scheduler.py:213-299` `maintenance_job()`、`:332-348` 注册每日 job；`starwatt/db/repositories.py:764` `wal_checkpoint()`、`:777` `vacuum()`；配置键在 `registry.py:566-591` | 每日 03:30（CST）清理：复用既有 `dashboard_service.cleanup_before()` / `FailedAttemptRepo.purge_before()` / `SessionRepo.purge_expired()`，随后每次 `wal_checkpoint(TRUNCATE)`；累计删除行数 ≥ `vacuum_min_deleted_rows`（默认 2 万）才 `VACUUM`（计数持久化在 `vacuum_pending_rows`，跨重启累计）；`retention_enabled=false` 可整体关闭；逐项兜底、绝不外抛 |
| 5 | OOBE 不校验必需项（blocker）：全程留空也能走完并把 `oobe_completed` 置 True | `starwatt/services/oobe_service.py:177-196`（`_missing_required()` / `_required_message()`，`_is_done` 与门禁共用同一判定）、`:282-295` `advance()` 门禁 | 「下一步」前先查当前步 `step.required`；缺项时 `saved["errors"]` 带「必填项「登录凭据 openid」不能为空」、既不推进也不置 completed；`skip=true` 仍是显式旁路 |
| 6 | `room_label` 从不落库 → 推送标题永远是通用名 | `starwatt/services/setup_service.py:274-305` `_backfill_room_label()`，由 `:236-240` 的 `verify()` 成功分支调用（UI 真实路径），`commit()` 同源复用并注明 UI 不调用 | 发现到房间名且当前值为空时写入 `room_label`，`starwatt/notify/policies.py:452` 与 `card_builder.py:162` 立刻用上；已有值不覆盖；失败只 WARNING，**不改变 verify 的返回结构与 `ok` 语义** |
| 7 | Cookie 恒无 `Secure`、注释里的 `ProxyFix` 全仓库不存在 | `starwatt/config_registry/registry.py:445-460` 新键 `session_cookie_secure` / `trust_proxy`（**默认 false**）、`starwatt/web/factory.py:251-259` 显式写 Cookie 属性、`:203-226` `init_proxy_fix()`（仅 trust_proxy 真时包 `ProxyFix`，幂等）、`web.py:55-61` 模块级再包一次（gunicorn 入口是 `web:app`） | HTTP 部署默认不变（强制 Secure 会让登录失效）；上了 HTTPS 后在配置中心打开 `session_cookie_secure` 即可；`trust_proxy` 打开后锁定与审计才拿到真实客户端 IP |
| 8 | 零安全响应头 | `starwatt/web/factory.py:68-76` `SECURITY_HEADERS`、`:183-200` `@app.after_request` | 每个响应加 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: same-origin`、`Permissions-Policy`；HSTS 只在 `request.is_secure` 时加；不覆盖业务已设的头；**有意不加 CSP**（Vue SPA 需真实浏览器逐页验证后再上） |
| 9 | `?include_secrets=1` 明文返回凭据且不写审计 | `starwatt/web/blueprints/admin.py:83-85` `_wants_secrets()`、`:115-141` `config_get`、`:167-190` `config_export` | 明文读取一律写 `config.read_secrets` 审计 + `Cache-Control: no-store`；导出写 `config.export` 并用 `details.include_secrets` 区分两种导出；功能不变（仍返回明文） |
| 10 | SSRF 黑名单漏 IPv4-mapped / NAT64（`is_blocked_ip("::ffff:127.0.0.1") == False`） | `starwatt/scraper/ssrf.py:97-101`、`:181-202` `_normalize_mapped()`、`:205-224` | 地址按「原样 + 归一化 IPv4」双候选校验，`::ffff:127.0.0.1`、`64:ff9b::/96` 映射的内网地址现在会被拦。**有意偏离**原方案：`::ffff:0:0/96` **整段**拉黑会连 `::ffff:8.8.8.8` 一起误杀，因此只用于归一化 |
| 11 | `/healthz` 只证明进程活着（不碰 DB/调度器） | `starwatt/web/blueprints/health.py:176-200` 新增 `/healthz/deep`、`:78-97` DB 可写探针、`:107-133` 调度器探针、`:136-147` 抓取状态（非必需） | `/healthz` 保持轻量不变；`/healthz/deep` 任一关键探针失败返回 503。**架构约束**：`scripts/ast_guard.py` R4 禁止 `starwatt/**` 回引顶层 `web`，故调度器探针经 `app.extensions[SCHEDULER_PROBE_KEY]` 注入 |
| 12 | 一次性初始密码文件用完不删（磁盘上长期留明文） | `starwatt/auth/service.py` `PASSWORD_FILE_NAME` + `_wipe_password_file()`，`ensure_bootstrap_admin()` 建号成功后调用；`docker-entrypoint.sh:69-79` 改为 `umask 077` 落盘 + `chmod 600` | 建号成功即删文件（删不掉只 WARNING）；写不进只读卷也不触发 `set -e`；没截到日志时可用 `starwatt-docker.sh password` 取回 |

## 三、你的决策（4 条，均已按你的答复执行）

1. **测试里的真实 openid → 已替换成假值**（你选「顺手替换」）：`tests/unit/test_config_registry.py:287/288/342/357/430/434/436/507/512/514` 与 `tests/unit/test_logging_setup.py:24`（该处拆成两段拼接是为避开 `scripts/secret_scan.py` 的自指陷阱，保留原写法只换值）。复核：`git grep "FAKopenid"` 无命中、`secret_scan.scan()` → `[]`、`test_config_registry + test_logging_setup + test_deploy + test_guard_scripts` 共 **272 passed**。**注意：git 历史里仍留有你真实 openid 的旧提交**——这个我改不了，需要你自己判断是否要重写历史或轮换凭据。
2. **保留期默认值 → 按 180 天默认开启**（你选「就按 180 天默认开启」）：`records_retention_days=180`、`failed_attempts_retention_days=30`，每日 03:30 执行；可在配置中心「数据维护」组改小/改大或整体关掉（`retention_enabled=false`）。
3. **`session_cookie_secure` 默认 false**：因为你的部署是 HTTP（容器直接映射端口），强制 `Secure` 会让登录直接失效。**若你按 `docs/DEPLOY.md` 第四章上了 nginx + HTTPS，请在配置中心把它打开**，否则会话 Cookie 在明文信道里裸奔。
4. **容器日志无上限**（compose 无 `logging:` 段 + 应用默认 `file_enabled=False` → 全进 docker json-file 且无轮转）。本次发布**只改了仓库 compose 的默认 tag，没有加日志上限**（避免动你机器上那份部署文件的既有结构）；请在服务器上手工加：
```yaml
services:
  starwatt:
    # …既有配置不动…
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "5"
```

## 四、未做（按优先级，附不做的理由）

**建议下一轮优先做**
- `frontend` 空白页三连：[电表标签整页空白]、[概览趋势卡空白 + 指标卡恒 `—`]、[抓取失败的 error 从不渲染 → 界面显示「一切正常」]。**已按你的决定推迟**：先看新镜像上线后是否还存在（关键取证：你现在浏览器里跑的是 `2.0.2-starcore` 的旧前端产物——「换表/重置」这句文案只存在于 tag 版本里，而你的截图上正好有它）。
- 安全：`setup_service.py:210-212` 保存时**校验 openid↔roomId 归属**（现在粘贴任意含 roomId 的链接就能 verify→commit 并长期轮询别人房间的用电）。审计原判 high，我建议按 medium 处理并在下一轮补上。
- 安全：登录锁定按 `(username, ip)` 且反代下 `remote_addr` 恒为代理 IP → 可互锁 DoS / 换 IP 即绕过。**依赖第 3 条**（`trust_proxy` 打开后才有真实客户端 IP）。
- 稳定性：`/api/refresh` 无节流无互斥、429 不读 `Retry-After`、`create_app` 的 `db_init()` 无 try/except、`scrape_interval_sec` 改了不生效且不提示重启。

**暂不做（需产品判断或风险高于收益）**
- 配置中心的「自动备份」开关是**空壳**（`registry.py:482/486-487` 是全仓库唯一出现，无任何备份代码）：要么实现，要么**删掉这个开关**别骗人。建议先删开关——备份更适合在宿主侧做（`starwatt-docker.sh backup` 就是对的）。
- `starwatt-docker.sh:419-431` 的恢复流程与 compose 的 `cap_drop: [ALL]` 冲突（`chown` 会失败），且先 `mv` 旧库再 `cp`、不清理 `records.db-wal/-shm`：恢复中断会留下半截库 + 属主错。修它需要重新设计恢复步骤，且必须有真实容器验证（开发机无 Docker）。
- `scraper/ssrf.py:34-36` 自认的 DNS rebinding TOCTOU（要彻底解决得 pin 解析出的 IP 再连，改动大）。
- CSP：见第 8 项。

## 五、怎么拿到这些修复（**必须重新构建镜像**）

**关键前提**：`static/` 前端产物**不入库**（`.gitignore` 忽略 `/static/index.html`、`/static/assets/`），Dockerfile 在 frontend 阶段构建它。所以「只更新源码」拿不到任何前端修复（OOBE 弹回、空白页都属于前端产物）。

本次发布已按你的选择执行：**从 `v2.0.2-starcore` 拉干净分支 `release/2.0.3-starcore`，只带本次修复，推标签 `v2.0.3-starcore` → GitHub Actions 构建多架构镜像**。

```bash
git push origin release/2.0.3-starcore          # 只推分支：不触发构建（ci.yml 只监听 main/develop/rewrite）
git push origin v2.0.3-starcore                 # 推标签：触发 release.yml → verify(ruff/ast_guard/pytest/secret_scan) → build → ghcr
```
CI 产出：`ghcr.io/tss-small-sunshine/starwatt:2.0.3-starcore`、`:v2.0.3-starcore`、`:latest`（`starcore` 不含 `-rc`，会打 latest）、`:2.0.3-starcore-armv7`，外加 GitHub Release 与离线包。
**verify job 是 build 的 `needs` —— 测试一红就没有镜像**；本次三个门（pytest 1707 通过 / `npm run typecheck` / `secret_scan`）都已在本机预跑通过。

服务器上（`/root/Docker`）：
```bash
tar -czf starwatt-backup-$(date +%F).tar.gz starwatt/data          # 先备份
STARWATT_TAG=2.0.3-starcore docker compose pull && STARWATT_TAG=2.0.3-starcore docker compose up -d
docker compose logs -f --tail=50 starwatt
```
> ⚠️ **`&&` 不能拆**：compose 里同时有 `image:` 与 `build:`，若 `pull` 失败（tag 不存在）而你接着单独跑 `up -d`，Compose 会**现场构建**出一个同名但内容不同的镜像。
> 也可只改 `.env` 里的 `STARWATT_TAG=2.0.3-starcore`（`starwatt-docker.sh upgrade 2.0.3-starcore` 会自动做这件事）。

本机（开发机）没有 Docker，所以「本地构建后搬运」这条路这次走不通，只能走 CI。

**升级后请确认**：
- `docker compose exec starwatt cat /data/.initial-admin-password` 应**不存在**（建号成功即自删）；
- OOBE 第 2 步（学校接入）留空时点「下一步」应被拦下并提示「必填项「登录凭据 openid」不能为空」；
- 走完 OOBE 点完成后应直接进总览页，**刷新也不会被弹回向导**；
- `docker compose logs starwatt | grep -i "数据清理"` 在 03:30 后应出现一条含删除行数与 DB 大小的 INFO；
- 推送标题里应出现真实房间名（如「6号楼-1-119」）而不是通用名。
