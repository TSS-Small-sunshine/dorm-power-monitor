# StarWatt 星瓦 · 稳定性 / 可靠性 / 可维护性审计

**审计对象**：`dorm-power-monitor`（Flask + SQLite + APScheduler + Gunicorn 单容器，部署于阿里云 ECS）
**审计方式**：只读静态审计 + 用仓库自带解释器 `.venv\Scripts\python.exe` 实跑测试与探针脚本；未联网、未请求真实学校站点、未修改任何源码/测试/配置。
**审计人员与时间**：stability 审计员，2026-10-11 12:20–13:0x
**证据基线（重要）**：本次审计在一个**被并发修改的工作区**中进行。`git status --short` 显示 **47 个文件处于修改状态**（`M docker-compose.yml`、`M docker-entrypoint.sh`、`M install.sh`、`A starwatt-docker.sh`、`M starwatt/scraper/client.py`、`M starwatt/scraper/service.py`、`M starwatt/auth/service.py`、前端大量文件…），且 `docs/audit/security.md`（61427B, 12:23:51）与 `docs/audit/usability.md`（48553B, 12:24:59）由另外的审计者在同一时段写入；`tests/unit/test_finduser_openid.py` 于 12:33:30 被改写。**本文所有 `文件:行` 锚点均以 2026-10-11 12:3x 的工作区状态为准**，HEAD = `ff9e30d`。

## 实跑证据（命令与结果）

| 命令 | 结果 |
| --- | --- |
| `.\.venv\Scripts\python.exe -m pytest -q --no-header -p no:cacheprovider` | **exit code 0**，连跑 4 次均通过，无 F/E |
| 同上 `--collect-only -q` | **34 个测试文件 / 1564 条用例** |
| `.\.venv\Scripts\python.exe -c "import sys;sys.path.insert(0,'.');from scripts import secret_scan;print(secret_scan.scan())"` | `findings: 0` |
| `.\.venv\Scripts\python.exe -c "...apscheduler 版本与 astimezone(ZoneInfo('Asia/Shanghai'))..."` | `ver 3.11.3` / `ok ZoneInfo Asia/Shanghai` |
| `git status --short` / `git diff -- docker-compose.yml` | 47 项改动；见上文基线说明 |

**审计中途唯一一次测试失败（须记录）**：`pytest -q tests/unit/test_guard_scripts.py` 曾 exit=1，
`FAILED tests/unit/test_guard_scripts.py::TestSecretScan::test_repo_is_clean`，
断言信息为 `tests/unit/test_finduser_openid.py:45: OPENID = "<36 字符 UUID>"`（仓库内硬编码凭据，此处脱敏）。
该文件随后被并发写入者改写（第 45 行已变为 `EMPTY_HTML = ...`），此后 `secret_scan.scan()` 返回 0 findings、完整套件与定向运行均 exit 0 → **该失败已不再复现，不列为发现**，仅作为「工作区在移动」的证据。

---

# 发现

## S1 · 飞书群机器人 webhook 只校验 HTTP 状态码，不校验业务 code —— 告警丢失却打「推送成功」日志

- **严重度**：blocker
- **位置**：`starwatt/notify/transport.py:136-147`（唯一 webhook 出口 `_post`）；对照同仓库已正确实现的 `starwatt/notify/qq.py:187-188`、`qq.py:239-247`
- **证据**（`starwatt/notify/transport.py:136-147` 原文）：
  ```python
  http = session or requests
  try:
      response = http.post(webhook, json=body, timeout=timeout_sec)
      response.raise_for_status()
  except requests.RequestException as exc:
      logger.error("飞书推送失败：%s", exc)
      if raise_on_error:
          raise
      return False

  logger.info("飞书推送成功（msg_type=%s）", payload.get("msg_type", "?"))
  return True
  ```
  全程不读 `response` 的 body。同仓库自己已经在 QQ 侧写明了判定规则（`qq.py:23-24`）：
  > ⚠️ 失败时 **HTTP 仍是 200**，错误在 body 的 ``code`` 里 —— 所以判成功要看 ``code``，不能只看状态码（官方文档明确警告过）。

  并且 QQ 侧真的实现了该判定（`qq.py:187-188 if not isinstance(payload, dict) or payload.get("code"):` → 报错），飞书群 webhook 侧没有。
- **触发条件与后果**：飞书自定义机器人 webhook 在下列情况下**返回 HTTP 200 且 body 内 `code != 0`**：被限流（`code=9499`/`frequency limited`）、机器人被移出群/被停用、签名校验失败（`code=19021`）、消息体超限。此时：
  1. 日志打印 `飞书推送成功（msg_type=...）`，运维看到的是一次成功的告警；
  2. `WebhookNotifier.send_card/send_text`（`transport.py:225-237`）吞掉异常，**无重试、无二次告警、无 dead-man switch**；
  3. 于是「低电量 / 违规 / 离线 / 报表」四类通知与 L3 门禁报表可以**在日志显示成功的状态下长期全部丢失**——这正是「跑久了会静默坏」的典型形态，且恰好发生在产品最核心的告警通路上。
- **修复建议**：在 `starwatt/notify/transport.py:139` 之后、`:146` 之前插入业务码校验（照抄 `qq.py:239-247` 的写法）：
  ```python
  payload_json = response.json() if response.content else {}
  if not isinstance(payload_json, dict) or payload_json.get("code"):
      logger.error("飞书推送业务错误：code=%s msg=%s",
                   payload_json.get("code") if isinstance(payload_json, dict) else "?",
                   payload_json.get("msg") if isinstance(payload_json, dict) else payload_json)
      if raise_on_error:
          raise RuntimeError(f"feishu webhook code={payload_json.get('code')}")
      return False
  ```
  同时把 `:146` 的 `logger.info("飞书推送成功…")` 移到校验之后。

## S2 · 配置中心暴露「自动备份」开关与保留天数，但全仓库没有任何备份实现

- **严重度**：high
- **位置**：`starwatt/config_registry/registry.py:482`、`starwatt/config_registry/registry.py:486-487`（定义）；实现缺失（`starwatt/` 全树 grep `backup_enabled` 仅命中这两处）
- **证据**：
  ```
  starwatt/config_registry/registry.py:482: "backup_enabled", "自动备份", ADVANCED, T.BOOL, True,
  starwatt/config_registry/registry.py:486: "backup_retention_days", "备份保留天数", ADVANCED, T.INT, 7,
  starwatt/config_registry/registry.py:487: validate="1..365", depends_on="backup_enabled",
  ```
  全仓库 `grep -rn "backup_enabled"` 只有以上两行（+ `starwatt-docker.sh` 里的手工 `cmd_backup`）。
  `starwatt/scheduler.py:158-189 build_scheduler()` 只注册 2 个 job（`JOB_SCRAPE`、`JOB_L3_GATE`），**没有任何备份/清理/归档 job**。
- **触发条件与后果**：管理员在配置中心看到「自动备份 = 开启（默认 True）、保留 7 天」，据此认为数据库有兜底；实际唯一备份手段是人工执行 `./starwatt-docker.sh backup`。一旦出现 SQLite 损坏、误删、卷故障，或用户以为「反正有自动备份」而长期不手工备份，数据将不可恢复。**这是「用户以为存在的安全网其实不存在」，属于典型的可维护性陷阱。**
- **修复建议**：二选一，不要留在「有开关没实现」的状态。
  1. 实现：在 `starwatt/scheduler.py:173` 之后追加
     ```python
     scheduler.add_job(_backup_job, CronTrigger(hour=4, minute=10, timezone=CST), id=JOB_BACKUP, replace_existing=True)
     ```
     `_backup_job()` 内部 `sqlite3.connect(db).backup(dst)` 到 `settings.data_dir/"backups"/f"records-{today}.db"`，并按 `get_int("backup_retention_days", 7)` 删旧份；异常按现有 `scrape_job` 的模式 `logger.exception` 不外抛。
  2. 或删除 `registry.py:482` 与 `:486-487` 两个配置项，并在 `starwatt-docker.sh backup` 的文档里明确「本项目**不含**自动备份，请自行配置 cron / 云盘快照」。

## S3 · 容器日志无任何上限：compose 未配 logging 驱动、应用日志默认不写文件

- **严重度**：high
- **位置**：`docker-compose.yml`（无 `logging:` 段，全文 grep `logging:` / `max-size` / `max-file` / `json-file` **0 命中**）；`starwatt/logging_setup.py`（`_FALLBACK` 中 `"file_enabled": False`）；`gunicorn.conf.py`（`accesslog="-"`、`errorlog="-"`）
- **证据**：
  - `docker-compose.yml` 全文无 `logging:` 段 → 使用 Docker 默认 `json-file` 驱动，**默认无 `max-size`**，日志无限增长。
  - `starwatt/logging_setup.py` 的默认配置 `file_enabled=False`（`_FALLBACK`），即**默认情况下应用日志只进 stdout**（`_make_handler` 的 StreamHandler → `sys.stderr`）；`gunicorn.conf.py` 把 access/error log 都设为 `"-"`（stdout/stderr）。
  - 因此所有日志最终都落在 Docker 的 `json-file` 里，**没有任何一方做轮转**。
- **触发条件与后果**：抓取失败时 `logger.exception` 会打完整堆栈（`scraper/service.py` 多个路径、`scheduler.py:106`），学校接口挂掉/字段变更时会周期性打印长堆栈；每条 HTTP 访问日志同样累积。默认 `json-file` 无上限 → **数周到数月尺度上把宿主 `/var/lib/docker/containers/*/*-json.log` 写到撑满磁盘** → SQLite 写失败（`database or disk is full`）、容器被 `restart: unless-stopped` 反复重启。
- **修复建议**：两处都补上（互为冗余）。
  1. `docker-compose.yml` 的 `starwatt` 服务下增加：
     ```yaml
     logging:
       driver: json-file
       options:
         max-size: "10m"
         max-file: "5"
     ```
  2. 把 `starwatt/logging_setup.py` 的 `_FALLBACK` 里 `"file_enabled"` 改为 `True`（日志文件写在 `/data/logs/starwatt.log`，属**数据卷内**、`TimedRotatingFileHandler(backupCount=retention_days)` 自带轮转，见 `logging_setup.py:406-423`），或至少在 `docs/CONFIG.md` 里写明「容器日志需宿主自行配置 logrotate」。

## S4 · 没有任何周期清理任务：records / audit_log / failed_attempts / sessions 单调增长

- **严重度**：high
- **位置**：`starwatt/scheduler.py:158-189`（只有 2 个 job，无维护 job）；`starwatt/services/dashboard_service.py:384-390`（`cleanup_before`，**唯一的 `delete_before` 调用点**）；`starwatt/db/repositories.py:157`（`delete_before` 定义）、`starwatt/db/repositories.py:623`（`purge_expired` 定义）、`starwatt/db/repositories.py:748`（`purge_before` 定义）
- **证据**：全仓库 grep 结果——
  - `delete_before`：`tests/unit/test_db.py:174/177`、`starwatt/db/repositories.py:157`（定义）、`starwatt/services/dashboard_service.py:387`（调用）。
  - `purge_expired`：`tests/unit/test_auth.py:290/293`、`starwatt/auth/session.py:125`、`starwatt/db/repositories.py:623`。**无生产调用者**。
  - `purge_before`：`tests/unit/test_db.py:323/326`、`starwatt/db/repositories.py:748`。**只有定义**。
  - `records_retention` / `data_retention`：**0 命中**（连配置项都不存在）。
  - 即：**清理代码存在、测试存在，但生产路径从不调用它**（`cleanup_before` 没有被任何 scheduler job 或启动钩子调用）。
- **触发条件与后果**（按默认配置 `scrape_interval_sec=600`、CST）：
  - `records`：144 行/天 ≈ **5.3 万行/年**（单行约 80–120B，约 5–6 MB/年）；若把间隔下调到下限 60s，则 **52 万行/年，约 50–60 MB/年**。
  - `audit_log`：`repositories.py:639-642` 注释声明「**只追加**（不可篡改语义）」，因此设计上没有删除路径，仅随登录/管理操作增长。
  - `failed_attempts`：一次登录失败一行，**且由未认证请求即可写入**（`starwatt/auth/service.py` 的 `login.failed` 审计路径 → `web/blueprints/auth_api.py`）。任何人对 `/api/login` 的爆破都会**直接放大数据库体积**，且永远不会被清理。
  - `sessions`：每次登录一行，`purge_expired` 无生产调用者。
  - `DELETE` 之后不做 `VACUUM`、不做 `PRAGMA wal_checkpoint(TRUNCATE)`（全仓库 `VACUUM`/`wal_checkpoint` **0 命中**），即使将来开始删除，**文件体积也只涨不缩**。
  - 后果：磁盘占用单调增长、无告警、无上限；`VACUUM` 从未执行意味着长期运行后库文件的空洞会停留在卷里并被打进备份。
- **修复建议**：在 `starwatt/scheduler.py:173` 之后增加一个每日维护 job：
  ```python
  scheduler.add_job(_maintenance_job, CronTrigger(hour=3, minute=30, timezone=CST),
                    id="maintenance", replace_existing=True)
  ```
  `_maintenance_job()` 依次调用：`dashboard_service.cleanup_before(get_int("records_retention_days", 180))`、`SessionRepo.purge_expired()`、`FailedAttemptRepo.purge_before(to_stamp(now_cst()-timedelta(days=30)))`，最后 `conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")`；全部包在 `except Exception: logger.exception(...)` 里（与 `scrape_job` 同模式）。并在 `starwatt/config_registry/registry.py` 补 `records_retention_days`（T.INT，默认 180，validate `30..3650`）。**注意**：`audit_log` 若要保持「只追加」语义，请显式在配置文档中写明它的增长不受保留策略约束。

## S5 · 恢复流程与容器加固冲突：`cap_drop: [ALL]` 下 `chown` 会失败，且脚本在 `cp` 之前就把旧库挪走

- **严重度**：high
- **位置**：`starwatt-docker.sh:419-431`（`cmd_restore` 的一次性容器命令）、`starwatt-docker.sh:422`（`mv`）、`docker-compose.yml`（`cap_drop: [ALL]`）
- **证据**（`starwatt-docker.sh:419-431` 关键片段）：
  ```sh
  ... --user root --entrypoint sh -v "$stage:/restore:ro" "$SERVICE" -c "set -e; \
    if [ -f /data/records.db ]; then mv /data/records.db /data/records.db.bak; fi; \
    cp /restore/records.db /data/records.db; \
    chown $CONTAINER_UID:$CONTAINER_UID /data/records.db; ...
  ```
  `docker-compose.yml` 的服务加固块含 `cap_drop: [ALL]`（与 `read_only: true`、`security_opt: [no-new-privileges:true]` 同组），且**没有 `cap_add: [CHOWN]`**。
- **触发条件与后果**：
  1. `set -e` + `cap_drop: ALL` → `chown` 返回非 0，脚本在该行中止；此时 `records.db` **已经被 `mv` 成 `records.db.bak`**，新库虽拷入但属主仍是 root（uid 0）。
  2. 数据卷里留下 `uid 0` 的 `records.db`，而容器以 `uid 10001` 运行 → SQLite 打开失败（`unable to open database file`）→ `create_app()` 的 `db_init()`（`starwatt/web/factory.py:191`，无 try/except）抛错 → worker 启动失败 → `restart: unless-stopped` 下**无限崩溃循环**，`/healthz` 永不返回。
  3. 恢复失败后用户看到的是「服务起不来」，而真实原因是权限，排查成本高；同时卷里只剩 `records.db.bak` 这一份可用数据。
  4. 脚本**只拷主库，不处理 `-wal` / `-shm`**：若旧容器是被强杀（WAL 未 checkpoint），残留的 `/data/records.db-wal` 与刚拷入的主库不匹配，SQLite 可能重放旧帧或报 `database disk image is malformed`。
- **修复建议**：
  - `starwatt-docker.sh:419` 的一次性容器加 `--cap-add CHOWN`（或整段改用 `--user 0:0 --cap-add CHOWN`），使 `chown` 有权限；
  - 把 `mv` 后置：先 `cp /restore/records.db /data/records.db.new && chown ... /data/records.db.new && mv /data/records.db.new /data/records.db`，并在同一命令里 `rm -f /data/records.db-wal /data/records.db-shm`；
  - 恢复后主动 `docker compose exec ... python -c "import sqlite3;print(sqlite3.connect('/data/records.db').execute('pragma quick_check').fetchone())"`，把校验结果打印出来，失败则提示回滚 `.bak`。

## S6 · 部分失败静默：F2/F3/F5 全部失败时 `last_scrape_status` 仍然是 `ok`

- **严重度**：medium
- **位置**：`starwatt/scraper/service.py:469`（`report.ok = "F1" in report.ran`）、`starwatt/scraper/service.py:452-482`（`_finish`）、`starwatt/scraper/service.py:344-364`（`_fetch`/`_persist` 只 warning）、`starwatt/scraper/service.py:390-397`（失败也写节流时间戳）
- **证据**：
  ```python
  starwatt/scraper/service.py:469:        report.ok = "F1" in report.ran
  ```
  `_fetch()` 单端点 `except Exception → 记 errors + logger.warning + return None`（`service.py:344-352`）；`_persist()` 同理（`service.py:355-364`）；`_run_endpoint` 在 `rows is None` 且非 `forced` 时，**若该端点配了节流就照样 `set_state(spec[1], to_stamp(moment))` 打点**：
  ```python
  # starwatt/scraper/service.py:390-397（注释为原文）
  # legacy 冻结行为：普通节流端点失败也打点，避免打爆学校接口
  ```
  告警钩子只有 4 个：`stale / low_battery / violations / offline`（`service.py:309-329`），**没有任何钩子表达「端点抓取失败」**。
- **触发条件与后果**：学校侧对 F2（日用电，`daily_elec` 唯一来源）、F3（违规）、F5（缴费）任一接口返回错误/改字段/401 时：
  1. 该轮 `_finish` 仍写 `last_scrape_status=ok`、`logger.info` 成功摘要；
  2. 失败端点在默认 24h 节流窗口内不会被重试；
  3. 页面「日用电」曲线缺一段、违规/缴费列表过期，**用户不会收到任何推送**（`/healthz` 也不反映，见 S7）。
  **缓解**：F2/F3 使用 30 天窗口（`starwatt/scraper/endpoints.py:319`、`:390`），F5 用学期起始至今（`endpoints.py:497`），因此**下次成功抓取会自愈**——这也是本条定为 medium 而非 high 的原因。
- **修复建议**：把「核心端点（F1）成功」与「全部端点成功」分开表达，并让后者可告警：
  1. `_finish` 中新增：`if report.errors: logger.warning(...)` **并且** `if not fetch_only and report.errors: _safe_push(hooks.get("partial_failure"))`；
  2. 在 `starwatt/notify/policies.py` 增加 `partial_failure` 策略（建议：连续 3 轮有 errors 才推送，避免抖动）。
  3. 若要保持 legacy 的「失败也打点」，请把节流窗口改为**失败时缩短**（例如失败后 1 小时允许重试），而不是沿用 24h。

## S7 · `/healthz` 不反映调度器与数据库状态 —— 调度停了、磁盘满了，容器仍然 healthy

- **严重度**：medium
- **位置**：`starwatt/web/blueprints/health.py:27-30`；消费者为 `docker-compose.yml` 与 `Dockerfile` 的 `HEALTHCHECK ... curl -fsS http://127.0.0.1:${FLASK_PORT}/healthz`
- **证据**：
  ```python
  starwatt/web/blueprints/health.py:27: @bp.get("/healthz")
  starwatt/web/blueprints/health.py:28: def healthz():
  starwatt/web/blueprints/health.py:29:     """无认证、无 IO 的探活响应。"""
  starwatt/web/blueprints/health.py:30:     return jsonify(status="ok", version=APP_VERSION, time=stamp())
  ```
  不读 DB、不查调度器、不看 `last_scrape_at`。同时 `starwatt/web.py:142 scheduler_running()` 已经存在但没有任何调用者。
- **触发条件与后果**：以下任意一种情况下容器都会被 Docker 判定为健康，运维不会收到任何信号：
  - APScheduler 线程因未捕获异常退出（`scheduler.py:106/154` 目前都兜住了，但线程级故障不在覆盖范围）；
  - `scrape_interval_sec` 被配置成极大值、或调度器根本没启动（`master_pid` 判定误触发时 `web.py:100-104` 会抛 `RuntimeError`，而 `post_fork` 的异常处理未被确证，见「未能验证」）；
  - `/data` 卷写不进去（磁盘满、挂载失效）→ 抓取全部失败，但 `/healthz` 照旧 200。
  结果是「服务在跑、数据已停」，只能靠人肉看页面发现。
- **修复建议**：把 `/healthz` 拆成存活/就绪两层：
  - `GET /healthz?deep=1`（或新增 `/readyz`）返回 `{"status":..., "scheduler_running": bool, "last_scrape_age_sec": int}`；当 `not scheduler_running()` 或 `last_scrape_age_sec > 3 * interval` 时返回 **503**；
  - HEALTHCHECK 改指向 `deep`，而把「进程活着」交给 Docker 自身的进程状态判断。
  这样 S6 的静默失败也会被 Docker/orchestrator 直接暴露。

## S8 · 配置「抓取间隔（秒）」改完不生效，且 UI 不提示需要重启

- **严重度**：medium
- **位置**：`starwatt/config_registry/registry.py:184-186`（定义，**无 `requires_restart`**）；`starwatt/scheduler.py:158-189`（间隔只在 `build_scheduler()` 读一次）；对照 `registry.py:139-142`（`site_timezone` 有 `requires_restart=True`）
- **证据**：
  ```
  starwatt/config_registry/registry.py:184: "scrape_interval_sec", "抓取间隔（秒）", SCRAPE, T.DURATION, 600,
  starwatt/config_registry/registry.py:141: requires_restart=True（仅出现在 site_timezone）
  ```
  全仓库 `requires_restart` 只被 `site_timezone` 使用；`reschedule_job` / `modify_job` 全仓库 **0 命中**。
- **触发条件与后果**：用户在网页把 600 改成 60，页面显示保存成功，`starwatt/scheduler.py:116-131 _interval_seconds()` 的 docstring 甚至写着「用户改完配置重启即生效」——但配置中心**没有**把这项标记为需要重启。用户不会重启，于是永远保持旧间隔（或反之：为了降频而改成 3600 却仍按 600 高频抓取 → **封禁风险**）。排障时极难发现。
- **修复建议**：在 `starwatt/config_registry/registry.py:184` 的 `Setting(...)` 调用中加 `requires_restart=True`（一行改动，与 `site_timezone` 一致）；若希望免重启，则在配置保存回调里 `_scheduler.reschedule_job(JOB_SCRAPE, trigger=IntervalTrigger(seconds=new, timezone=CST))`（`starwatt/web.py` 已有 `_scheduler` 句柄）。

## S9 · 429/403 不重试、不读 `Retry-After`；失败也写入节流时间戳 → 被限流后最长 24h 停摆

- **严重度**：medium
- **位置**：`starwatt/scraper/client.py:286-287`（4xx 立刻失败）、`starwatt/scraper/client.py:71`（`_RETRY_STATUS` 只有 `{500,502,503,504}`）、`starwatt/scraper/service.py:390-397`（失败也打点）
- **证据**：
  ```
  starwatt/scraper/client.py:71:  _RETRY_STATUS = frozenset({500, 502, 503, 504})
  starwatt/scraper/client.py:286:  # 4xx：客户端错误，重试无意义 —— 立刻失败
  ```
  `Retry-After` 全仓库 0 命中。
- **触发条件与后果**：学校接口风控/限流（返回 429 或 403 带 `Retry-After`）时：
  1. 本轮立刻失败（合理）；
  2. 但因为 S6 的「失败也打点」，该端点在 `scrape_f2_throttle_hours`（默认 24）/`scrape_f5_throttle_hours`（默认 24）内**不会重试** → 最长 24 小时的数据空洞（F3 默认 1h，影响较小）；
  3. 更糟的是：如果是**我们自己**请求过频导致被限流，代码不会降频，下个窗口继续以原频率打过去 → 有升级为长期封禁的风险。
- **修复建议**：
  1. `starwatt/scraper/client.py:71` 的 `_RETRY_STATUS` 增加 429；在 `:286` 之前单独处理 `429`：读 `Retry-After`（`response.headers.get("Retry-After")`），按秒数 sleep（上限如 300s）后重试；
  2. 引入「最近 429 次数」状态，连续 429（如 ≥3）时把 `scrape_interval_sec` 的实际生效值临时翻倍（自适应退避），并 `_safe_push` 一条告警；
  3. 配合 S6 的修复，让失败不再消耗 24h 节流窗口。

## S10 · `/api/refresh` 无节流、与定时抓取无互斥 —— 上游请求翻倍与同秒记录覆盖

- **严重度**：medium
- **位置**：`starwatt/web/blueprints/dashboard.py:136-155`（端点）、`starwatt/web/security.py:71-100`（`refresh_access`，无节流）、`starwatt/services/dashboard_service.py:473-497`（`refresh()`，无锁）、`starwatt/scraper/endpoints.py:290` + `starwatt/db/repositories.py:157` 附近的 `INSERT OR REPLACE`
- **证据**：
  ```python
  starwatt/services/dashboard_service.py:473: def refresh() -> dict[str, Any]:
  starwatt/services/dashboard_service.py:486:     report = scraper.run_once(notify=False)
  starwatt/web/security.py:96:  if internal_token_ok():
  ```
  全仓库 `rate`/`throttle`/`cooldown` 在 `starwatt/web/` 下 0 命中（节流只存在于抓取端点的 24h 窗口，F1 的 `interval_sec=None` 表示**每轮必抓**）。
- **触发条件与后果**：
  1. 管理员连点刷新、或脚本循环调用（`X-Internal-Token` 泄漏时则任何人可调）→ F1 被**无下限**地打向学校接口，封号风险；
  2. 手动刷新与 scheduler 的 `scrape_job` 可以**同时执行**（无进程内锁；`job_defaults.max_instances=1` 只约束调度器内部，不管 HTTP 请求）→ 同一时刻对上游双倍请求；
  3. F1 的落库时间戳是 `now_cst()` 的**秒级**格式（`endpoints.py:290 "ts": to_stamp(timeutil.now_cst())`），且写入是 `INSERT OR REPLACE INTO records (ts,...)`（`repositories.py:157` 附近）→ 同一秒内的两次抓取**互相覆盖**，静默丢一条（对电量曲线影响很小，但属于不可解释的数据缺口）。
- **修复建议**：在 `starwatt/services/dashboard_service.py:473 refresh()` 里加一个模块级 `threading.Lock()`（`with _REFRESH_LOCK:` 包住 `run_once`）与最小间隔判断（例如 `last_manual_refresh` 距今 < 30s 则直接返回 `{"ok":False,"error":"too frequent"}`）；同时给 `/api/refresh` 加按用户/IP 的限流。

## S11 · 只读根下 `FLASK_SECRET_KEY` 自举写盘必然失败 → 用临时密钥加密用户 secret，重启后密文永久不可解密；warning 文案只说「会话失效」

- **严重度**：medium
- **位置**：`starwatt/auth/secret.py:96-119`（`flask_secret_key`）、`starwatt/auth/secret.py:65-93`（`_persist_to_env`，`except OSError → warning` 于 `:91`）、`starwatt/config_registry/secrets.py`（`:13` 派生 AES 密钥、`:33` 与 `:155` 说明不可解密后果）
- **证据**：
  ```python
  starwatt/auth/secret.py:107:        if _persist_to_env(ENV_SECRET_KEY, generated):
  starwatt/auth/secret.py:115:            "%s 缺失且无法写回 .env，本次运行使用临时密钥 —— "
  ```
  `_persist_to_env` 写的是 `PROJECT_ROOT/.env`，容器内即 `/app/.env`；`docker-compose.yml` 设了 `read_only: true` → 写入必然 `OSError`（`:91` 打 warning「（只读文件系统？）」）。而 user secret（`dorm_openid`、飞书/QQ 凭据）的加密密钥正是由 `flask_secret_key()` 经 HKDF 派生（`config_registry/secrets.py:82 material = flask_secret_key().encode("utf-8")`，`:33 FLASK_SECRET_KEY 丢了 → 所有密文永久无法解密`）。
- **触发条件与后果**：默认 compose 路径下 `docker-entrypoint.sh` 一定会在启动时 export 一个持久化的 `FLASK_SECRET_KEY`（写入 `/data/.flask_secret_key`），所以**默认部署不触发**。但以下情形会触发且后果不可逆：手工 `docker run` 未传该变量、或 `FLASK_SECRET_KEY` 被显式设为空串（`docker-compose.yml:47 ${FLASK_SECRET_KEY:-}`）。此时应用以临时密钥运行 → 管理员在配置中心保存的 openid/凭据用临时密钥加密落库 → **容器重启后这些密文永久无法解密**（`secrets.py:155` 会打「解密失败 —— 通常是 FLASK_SECRET_KEY 变了」），而当时日志只警告「重启后所有已签发的会话将失效」——**严重低估后果**。
- **修复建议**：
  1. `starwatt/auth/secret.py:113-117`：写盘失败时把 warning 文案改为明确后果，例如「本次运行使用临时密钥 —— 重启后已加密的配置中心 secrets 将**永久无法解密**，且所有会话失效」；
  2. 更彻底：`flask_secret_key()` 在无法持久化时**拒绝启动**（`raise RuntimeError`），或提供 `STARWATT_ALLOW_EPHEMERAL_SECRET=1` 显式逃生开关——静默降级是这里最危险的部分；
  3. `starwatt/config_registry/secrets.py:155` 的解密失败路径应升级为**可推送告警的事件**，而不是仅落日志。

## S12 · `create_app()` 里的 `db_init()` 无异常保护 —— 库损坏/卷不可写时 worker 直接崩溃循环

- **严重度**：medium
- **位置**：`starwatt/web/factory.py:189-191`
- **证据**：
  ```python
  starwatt/web/factory.py:189:        from starwatt.db.connection import init as db_init
  starwatt/web/factory.py:191:        db_init()
  ```
  （注释说明这是为了「旧库 + 新代码 → 登录 500」而在应用工厂内补的幂等 `db.init()`；同文件 `:117-148` 的 `first_run_maintenance()` 反而包在 `except Exception: logger.exception("启动维护失败（忽略，服务继续启动）")` 里。）
- **触发条件与后果**：`/data/records.db` 损坏（半写、`malformed`）、卷被摘掉/只读、磁盘满、或属主错误（见 S5）时，`db_init()` 抛 `sqlite3.DatabaseError/OperationalError` → 每个 worker 在 `load_wsgi` 阶段就失败 → Gunicorn 无法启动 → `restart: unless-stopped` 下**无限重启循环**，`/healthz` 从不返回。用户只看到「容器一直重启」，日志需要翻很久才能定位；且没有任何「已检测到数据损坏、拒绝写入」的保护，进程一旦侥幸起来会继续往损坏库写。
- **修复建议**：把 `starwatt/web/factory.py:189-191` 包进 try/except，失败时记 `logger.critical` 并把 app 标记为 degraded（`app.config["DEGRADED"]=str(exc)`），让 `/healthz` 返回 503 且带原因，同时**不阻止进程存活**以便查看页面与日志；另建议在 `starwatt/db/connection.py:37-49 get_conn()` 首次连接后执行 `PRAGMA quick_check`，失败时明确报「数据库文件损坏」并给出从 `starwatt-docker.sh backup` 归档恢复的指引。

## S13 · `site_timezone` 是死配置：注册表声明「全项目时间语义依赖它」，但没有任何代码读取

- **严重度**：low
- **位置**：`starwatt/config_registry/registry.py:139`（唯一出现处，`:141 requires_restart=True`）；`starwatt/timeutil.py`（硬编码 `CST = ZoneInfo("Asia/Shanghai")`）
- **证据**：`grep -rn "site_timezone"` 全仓库仅 `registry.py:139`；help 文案为「🔒 锁定只读 —— 全项目时间语义依赖它（Q10）」。
- **触发条件与后果**：管理员若按 help 的字面意思理解，会以为改它能让全项目时区变化；实际 `timeutil.now_cst()` 永远用 `Asia/Shanghai`。排障时产生误导（例如误以为凌晨 3 点的维护任务会在别的时区跑）。
- **修复建议**：二选一——(a) 让 `starwatt/timeutil.py` 从注册表读该键并在导入时缓存（注意循环导入，建议由 `setup_logging`/启动钩子注入）；(b) 直接从注册表删除该项，或在 help 里改成「仅展示用途，改动无效（代码硬编码 Asia/Shanghai）」。

## S14 · 第三方库日志没有任何 handler：APScheduler / urllib3 的告警只能无格式落到 stderr

- **严重度**：low
- **位置**：`starwatt/logging_setup.py`（`logger.propagate=False`，仅配置 `starwatt` 子树）；全仓库 `basicConfig` **0 命中**
- **证据**：`setup_logging()` 只给名为 `starwatt` 的 logger 挂 handler 并 `propagate=False`；没有 root handler，也没有为 `apscheduler` / `urllib3` 配置 handler → 它们的记录只能经 `logging.lastResort`（WARNING 以上、无格式、无脱敏）进 stderr；INFO 级别（如 APScheduler 的 job 执行记录）**完全丢弃**。
- **触发条件与后果**：排障「某轮抓取没跑」时看不到 APScheduler 的 `Run time of job ... was missed by ...` / `maximum number of running instances reached` / `Execution of job ... skipped`，也看不到 urllib3 的连接池告警；且这些消息不过 `RedactingFilter`，可能把 URL 里的敏感参数原样写进日志。
- **修复建议**：在 `logging_setup.py` 的 `setup_logging()` 里为 `apscheduler`、`urllib3`、`werkzeug` 显式挂同一个（已带 `RedactingFilter` 的）handler，或给 root 挂一个同规格 handler 并去掉 `starwatt` 的 `propagate=False`（需注意重复输出）。

## S15 · 重启后不立即抓取，最长有一个完整间隔的数据空窗

- **严重度**：low
- **位置**：`starwatt/scheduler.py:173-179`（`add_job(..., IntervalTrigger(seconds=interval, timezone=CST), id=JOB_SCRAPE, ...)`，未提供 `start_date`/`next_run_time`）
- **证据**：`IntervalTrigger` 未指定 `start_date` → 首次触发在 `now + interval`；`starwatt/web.py:121 scheduler.start()` 之后没有任何立即触发。
- **触发条件与后果**：每次容器重启/升级（`starwatt-docker.sh upgrade` 会 `compose up -d` 重建容器）后，默认 **10 分钟**内没有新数据；`upgrade` 流程还会先 `compose stop`，因此实际空窗 = 停机时长 + 10 分钟。对 SNMP 式观测而言可接受，但会与「升级后立即验证」的运维习惯冲突。
- **修复建议**：在 `starwatt/web.py:121` 之前加 `scheduler.add_job(scrape_job, "date", run_date=datetime.now(CST), id="scrape_boot", replace_existing=True)`，或在 `add_job` 传 `next_run_time=datetime.now(CST)`。

## S16 · 全库无显式事务：`isolation_level=None`（autocommit）+ 多行 `executemany` 逐条提交

- **严重度**：low
- **位置**：`starwatt/db/connection.py:45`（`isolation_level=None`）、`starwatt/db/connection.py:10`（docstring 声明「需要事务时由调用方显式 `BEGIN`」）；全仓库 `BEGIN` 仅出现在该 docstring
- **证据**：
  ```
  starwatt/db/connection.py:10: * **``isolation_level=None``**（autocommit）—— 与旧代码一致；
  starwatt/db/connection.py:45: conn = sqlite3.connect(settings.db_file, timeout=30, isolation_level=None)
  ```
  `DailyElecRepo.upsert_many` / `ViolationRepo.upsert_many` / `PayRepo.upsert_many` 都在一个 `with connect() as conn:` 里 `executemany`，但处于 autocommit。
- **触发条件与后果**：(a) F2 的 30 行 `daily_elec` 写到一半进程被杀（`docker stop` 超时、OOM kill）→ 只落一部分且无法回滚；本轮视为「成功」还是「失败」取决于调用方，S6 的语义会误导。(b) WAL 下逐行提交意味着每行一次 fsync，写入放大。单机低并发下不致命，故为 low。
- **修复建议**：在 repositories 的批量写入外层包 `conn.execute("BEGIN IMMEDIATE")` / `conn.execute("COMMIT")`（异常时 `ROLLBACK`），例如给 `starwatt/db/connection.py` 增加 `transaction()` contextmanager 并在 `upsert_many`/多行 `insert` 中使用。

## S17 · F1 的落库时间戳用 `now_cst()` 而不是注入的 `moment`，秒精度 + `INSERT OR REPLACE` 会丢同秒记录

- **严重度**：low
- **位置**：`starwatt/scraper/endpoints.py:290`；`starwatt/scraper/service.py`（`run_once(..., now=None)` 的 `moment` 只用于节流打点）
- **证据**：
  ```
  starwatt/scraper/endpoints.py:290:        "ts": to_stamp(timeutil.now_cst()),
  ```
  写入为 `INSERT OR REPLACE INTO records (ts, read_time, remain)`（`starwatt/db/repositories.py` `RecordRepo.insert`），`records.ts` 是 `TEXT NOT NULL UNIQUE`。
- **触发条件与后果**：(a) 测试里传入的 `now` 对落库时间没有影响 → 测试与生产的语义不一致，容易写出假绿测试；(b) S10 的并发/同秒两次抓取会互相覆盖（静默丢一条记录）。
- **修复建议**：把该行改为使用 `run_once` 传入的 `moment`（`endpoints.py` 的 `parse` 已经通过 ctx 拿到 moment 的话直接用；否则把 `moment` 放进 `ctx` 透传），并把 `records.ts` 的时间精度提升到毫秒（`to_stamp` 加 `%f` 截断），或把唯一约束改为 `(ts)` → `(ts, read_time)` 之外的业务键。

## S18 · 备份归档内含明文密钥，无权限设置、也无敏感提示

- **严重度**：low
- **位置**：`starwatt-docker.sh:373-378`
- **证据**（要点）：`:374 compose exec -T "$SERVICE" cat /data/.flask_secret_key >"$stage/flask_secret_key"`；`:376 [ -f "$ENV_FILE" ] && cp "$ENV_FILE" "$stage/env.backup"`；`:378 tar czf "$out" -C "$stage" .` —— 未 `chmod`、未打印敏感提示。
- **触发条件与后果**：备份文件若被同步到云盘/仓库/群聊，`.flask_secret_key` 与 `.env`（可能含 `BOOTSTRAP_ADMIN_PASSWORD` 与 webhook secret）即泄露；凭 `FLASK_SECRET_KEY` 可解密配置中心全部密文、伪造 CSRF/会话（详见 security 审计）。
- **修复建议**：`tar` 之后加 `chmod 600 "$out"`，并打印「归档含明文密钥，请妥善保管、勿提交版本库」；建议同时提供 `--with-secrets/--no-secrets` 选项，默认只备份 `records.db`。

## S19 · 升级流程忽略健康检查失败

- **严重度**：low
- **位置**：`starwatt-docker.sh:243-265`（`cmd_upgrade`：`compose pull` → `compose up -d` → `wait_healthy || true`）
- **证据**：`wait_healthy` 失败被 `|| true` 吞掉，随后只打印回滚提示。
- **触发条件与后果**：新镜像启动即崩（例如 S12 的库不兼容、S5 的权限问题）时，升级脚本仍以成功退出码结束，自动化/人工会误判为升级成功，直到有人发现服务不可用。
- **修复建议**：去掉 `|| true`，失败时打印容器最后 50 行日志 + `docker compose ps` 状态并以非 0 退出；若检测到启动失败，主动提示并可一键回滚 `STARWATT_TAG`。

## S20 · 关键降级路径只落日志、不产生可告警事件（静默失败清单）

- **严重度**：low
- **位置**：`starwatt/scraper/service.py:309/329/348/360`、`starwatt/web/factory.py:146`、`starwatt/scheduler.py:106/126/146/154/200`、`starwatt/services/dashboard_service.py:284/406/487/515`、`starwatt/logging_setup.py:283/454/542/562`
- **证据**：全仓库 `except Exception|except OSError|except ValueError|except sqlite3` 共 **86 处**（含 notify 与 Web 层）。逐处核对后，**抓取/落库/调度这三个关键链路上没有「except 后 pass」的裸吞**（均有 `logger.warning/exception`），但它们的共同问题是：**降级只写日志，不产生任何用户可见的事件**——`web/factory.py:146` 的「启动维护失败（忽略，服务继续启动）」、`scraper/service.py:344-364` 的端点失败、`scheduler.py:106` 的 job 异常、`dashboard_service.py:284/406/487/515` 的展示层降级，全部如此。
- **触发条件与后果**：在默认配置下应用日志不落文件（S3）、也不进 stderr 的持久位置（Docker json-file 会滚动/被清理），因此这些降级**在事后无法复盘**；配合 S1（飞书告警静默）与 S7（健康检查无含义），运维对「数据是否在正常更新」几乎没有独立信号。
- **修复建议**：引入一个统一的 `degradation_event(kind, detail)` 出口：写 `logger.warning` 之外，把最近 N 条降级事件存入 `/data` 下的小状态文件（或在 `meta` 表加 `degraded_events`），并让 `/healthz?deep=1` 暴露「最近一次降级时间」；连续出现时走飞书推送（配合 S4 的维护 job 一并清理）。

---

# 已检查未发现问题（明确记录为「已核对」）

1. **调度器单例 / 多 worker 重复调度**：`gunicorn.conf.py:46 workers = 1`（硬编码，无 env 覆盖）+ `preload_app=False` + `post_fork` 中 `start_scheduler_once(master_pid=server.pid)`（`gunicorn.conf.py:62-71`），且 `starwatt/web.py:100-104` 在 `os.getpid() == master_pid` 时抛 `RuntimeError`、`starwatt/web.py` 内 `_scheduler is not None` 即返回 → 默认部署下**只有一个调度器实例**。逐条核对启动位置后未发现第二条启动路径。
2. **APScheduler 与 zoneinfo 兼容性**：实跑 `apscheduler 3.11.3` + `astimezone(ZoneInfo("Asia/Shanghai"))` 成功 → 时区不是故障点（不是只支持 pytz 的旧版本）。
3. **抓取器的失控面**：`starwatt/scraper/client.py` 每跳重定向上限 `MAX_REDIRECTS=3`（`:306-323`）且**每跳都复检 SSRF**、响应体 2 MB 硬上限并 `stream=True` 分块读取、超时 15s（可配 3..120）、重试次数有上限（`retry_count+1` 次）、退避 `min(2**(n-1),8)` 封顶 → 未发现无限重试、无限重定向、无限读取的路径。
4. **requests.Session 的并发安全**：`SchoolClient` 每次抓取都新建 Session（`scraper/service.py build_client()`），没有跨线程/跨进程共享同一个 Session → 未发现 session 复用导致的请求串扰。
5. **SQLite 连接泄漏与锁等待**：`starwatt/db/connection.py:37-49` 打开即设 `timeout=30`（SQLite 的 `timeout` 即 busy timeout，等效于 busy_timeout=30000ms）+ `journal_mode=WAL` + `foreign_keys=ON`；`connect()` 用 contextmanager `finally: close()` → 未发现 fd 泄漏路径；单写者 + WAL 下 `database is locked` 的窗口已被 30s 等待覆盖。
6. **迁移幂等性**：`starwatt/db/migrations.py` 每一步都是 `PRAGMA table_info` 判断后的 `ADD COLUMN`，版本号用 `INSERT OR REPLACE INTO meta` → 重复执行安全；`assert MIGRATIONS[-1].version == SCHEMA_VERSION` 在导入期校验，未发现「升级后忘了写迁移」的路径。
7. **备份脚本对 WAL 的正确处理**：`starwatt-docker.sh:363-369` 用 sqlite3 官方 `backup()` API（WAL 感知）而非 `cp`，并比对两侧行数（`:383-391`）→ 备份动作本身正确（问题在 S5 的恢复路径与 S18 的密钥处理）。
8. **日志脱敏链**：`logging_setup.py` 的 `CategoryFilter` + **最后**挂 `RedactingFilter`（注释明确要求顺序），`setup_logging` 幂等（先 `_detach` 再挂）→ 重复调用不会叠加 handler。
9. **容器内必写路径**：`PYTHONDONTWRITEBYTECODE=1`（Dockerfile ENV）→ 无 `__pycache__` 写入；`starwatt/` 全树 grep `tempfile|NamedTemporaryFile|/tmp` **0 命中**（唯一 `io.BytesIO` 在 `notify/renderer.py:302`，纯内存）；日志文件写在 `$DORM_DATA_DIR/logs`（数据卷内，非只读根）；`starwatt/` 下写文件的代码只有 `auth/secret.py` 与 `auth/service.py` 对 `/app/.env` 的写入（失败已被 `except OSError` 兜住，行为问题见 S11）→ **未发现只读根上的必写路径导致启动失败**。
10. **healthcheck 依赖**：runtime 阶段 `apt-get install ... curl`（Dockerfile），`HEALTHCHECK CMD curl -fsS .../healthz` 的依赖存在。
11. **镜像不携带本地敏感文件**：`.dockerignore` 明确排除 `.env`、`records.db*`、`static/assets`、`static/index.html` → `COPY . .`（Dockerfile:86）不会把开发机的密钥与数据库打进镜像。
12. **推送不会无限阻塞**：`notify/transport.py:55 DEFAULT_TIMEOUT_SEC = 15`（`:138` 使用）、`notify/qq.py:117` 同样 15 → 网络挂起时推送有上限。
13. **审计日志的完整性语义**：`repositories.py:639-642` 的 `AuditRepo` 只追加、无删除方法 → 与 schema 契约一致（其体积增长已在 S4 记录）。

---

# 未能验证的部分及原因

1. **只读根 / tmpfs 的真实写入行为**：本机未安装 Docker，任务亦禁止联网拉镜像，**无法实跑容器**。因此「`read_only: true` 下是否有写失败」「`HEALTHCHECK` 是否真的生效」「`cap_drop: [ALL]` 是否真的让 `chown` 失败（S5）」「恢复后带 `-wal` 残留的后果」均为**静态代码分析结论**，未取得运行期证据。
2. **学校接口的真实行为**：任务禁止对真实站点发请求，因此 429/403 + `Retry-After`、风控阈值、字段变更等外部行为只做了代码路径推演（S9）；未验证 `check_status()` 对数组型响应的实际覆盖度。
3. **数据增长速率**：无生产 `records.db`（仅有仓库根 135 KB 的开发库），S4 的「5.3 万行/年 / 50–60 MB/年」是按「每次抓取 1 行 + 默认 600s 间隔」与典型行宽估算，**未用真实数据核对**；`audit_log`/`failed_attempts` 的增长取决于登录失败次数，无法估算上界。
4. **运行时资源趋势**：内存/fd 的长期增长（任务要求核对项之一）需要长时间 soak 与容器环境，本次只能静态核对连接与句柄的关闭路径（结果见「已检查未发现问题」第 5 条），**未做实测**。
5. **解释器版本差异**：本机 `.venv` 为 **CPython 3.14.6**，镜像用 `python:3.12-slim`；`pytest` 的 1564 条全绿是在 3.14 上得到的结论，镜像内可能存在依赖版本差异（`requirements*.txt` 未逐一比对镜像内实际版本）。
6. **`gunicorn` 的 `post_fork` 异常处理**：`.venv\Lib\site-packages` 内 grep `def init_process|post_fork|except Exception` **0 命中**（未能定位到 gunicorn 26.2.0 的实现文件），因此「`post_fork` 里 `RuntimeError` 会让 worker 带病启动还是让整个 master 挂掉」未确证；S7 对此只做了条件性表述。
7. **未运行的脚本**：`scripts/check_db.py`、`scripts/cutover_check.py` 需要真实库/部署环境，未运行（`scripts/secret_scan.py` 已运行，结果 `findings: 0`）。
8. **工作区在移动**：审计期间工作区被并发修改（47 项改动；`docs/audit/security.md` 与 `usability.md` 由他人于 12:23/12:24 写入；`tests/unit/test_finduser_openid.py` 于 12:33:30 被改写）。**本文所有行号锚点为 2026-10-11 12:3x 的状态**；其中一次 `pytest tests/unit/test_guard_scripts.py` 失败（`test_repo_is_clean` 报 `tests/unit/test_finduser_openid.py:45` 硬编码 openid）在该文件被改写后不再复现，故未列为发现——但这也说明**当前工作区的测试绿灯取决于尚未提交的并发改动**。
