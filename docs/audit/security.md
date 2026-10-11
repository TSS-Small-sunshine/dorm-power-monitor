# StarWatt 星瓦 —— 安全性审计报告

审计对象：`D:\MiniMax_Workstation\Creative_Workstation\dorm-power-monitor`
审计方式：**纯只读**静态审计 + 用仓库自带解释器 `.venv\Scripts\python.exe` 运行**离线**纯函数验证（未对学校站点发任何请求，未联网安装任何依赖）。
部署背景（用户提供）：单容器、只读根、非 root（uid 10001）、数据 `/data/records.db`、暴露在阿里云 ECS。
审计日期：本次会话。

---

## 一、结论摘要

| # | 严重度 | 位置 | 问题 |
|---|---|---|---|
| F-01 | **high** | `starwatt/services/setup_service.py:210-234` / `starwatt/scraper/service.py:518-545` | roomId 与 openid 从不做归属绑定，学校侧越权被固化进配置并长期轮询 |
| F-02 | **high** | `starwatt/web/factory.py:166-174` / `starwatt/web/blueprints/auth_api.py:53` | 会话 Cookie 从未设置 `Secure`；注释声称的 `ProxyFix` 全仓库不存在 |
| F-03 | **high** | `starwatt/web/blueprints/admin.py:110-115` | `GET /api/admin/config?include_secrets=1` 明文吐全部凭据且**不写审计** |
| F-04 | medium | `starwatt/auth/service.py:117-130` / `starwatt/db/repositories.py:723-734` | 登录锁定按 (username, IP) 计数：反代下全站互锁；换 IP 即绕过 |
| F-05 | medium | `starwatt/auth/service.py:239-243` | 用户名枚举时序侧信道（用户不存在时跳过 scrypt） |
| F-06 | medium | `starwatt/web/factory.py`（无 `after_request`） | 全站零安全响应头（无 CSP / X-Frame-Options / HSTS / nosniff） |
| F-07 | medium | `starwatt/scraper/ssrf.py:196-219`＋`config_registry/registry.py:166` | `allow_private_hosts` 开关一开，4 层 SSRF 防护只剩 L1 |
| F-08 | medium | `starwatt/scraper/ssrf.py:104` / `:168-174` | SSRF 具体绕过面：L2 放过十进制/八进制/`127.1`；L3 拦不住 `::ffff:` 映射地址 |
| F-09 | medium | `requirements.txt:16-33` / `Dockerfile:40,63,24` | 依赖与基础镜像全部未固定版本（供应链） |
| F-10 | medium | `docker-entrypoint.sh:46-58` / `starwatt/auth/secret.py:96-118` | 绕过 entrypoint 启动时密钥每次重启重生成 → 密文永久解不开 |
| F-11 | low | `starwatt/web/blueprints/auth_api.py:152-154` | Cookie 寿命与 DB 会话寿命取自两个不同配置源，永不相等 |
| F-12 | low | `starwatt/web/blueprints/auth_api.py:59-62` `:88-94` | 注销清 Cookie 属性不匹配；注销不入审计 |
| F-13 | low | `starwatt/services/setup_service.py:148` | base_url 用 `netloc` 拼接，`user:pass@` 会被持久化并作为 Referer/Origin |
| F-14 | low | 见正文 | 审计覆盖面缺口（登出、明文读凭据、refresh、CSRF 失败…） |
| F-15 | low | `starwatt/web/blueprints/health.py:27-30` | `/healthz` 匿名公开版本号（可接受，属信息最小暴露） |
| F-16 | low | `docker-entrypoint.sh:80-97` | 初始管理员密码被打印进容器日志（保留期 = 日志保留期） |
| F-17 | low | `starwatt/web/security.py:92-121` | `X-Internal-Token` 是静态永久凭据，且可被 `include_secrets=1` 读出 |
| F-18 | low | `starwatt/notify/qq.py:339-368` / `notify/crypto.py:194-233` | 平台回调验签**不校验时间戳新鲜度** → 可重放 |
| F-19 | low | `starwatt/notify/commands.py:362-392` | 机器人命令不看发送者身份；仅查询，无写操作 |

---

## 二、详细发现

### F-01 · 学校接口越权的「固化路径」确实存在（复核用户实测结论）

- **严重度**：high
- **位置**：
  - `starwatt/services/setup_service.py:210`：`ctx = ScrapeContext(client=http, openid=credential, room_id=(room_id or "").strip())`
  - `starwatt/services/setup_service.py:212-223`：`if not ctx.room_id:` 才走 `discover_room`
  - `starwatt/services/setup_service.py:225-234`：拿该 room_id 跑 `F1Live().fetch(ctx)`，成功即 `"ok": True`
  - `starwatt/services/setup_service.py:283-294`：`payload["dorm_room_id"] = room_id` → `admin_service.config_update` → `set_state("last_room_id", room_id)`
  - `starwatt/scraper/service.py:526-545`：`room_id = get_str("dorm_room_id").strip()`；只在为空时才 `discover_room`，之后 `set_state(STATE_LAST_ROOM_ID, room_id)`
  - `starwatt/scraper/client.py:76`：`FINDUSER_PATH = "/campus/webchat/dormEmRealRead/finduser"`
  - `starwatt/scraper/service.py:199-202`：`ScrapeContext.url()` 把 openid 拼进查询串（`f"{path}{sep}openid={quote(self.openid, safe='')}"`）
- **证据**（关键代码摘录）：
  ```python
  # setup_service.py:210-226
  ctx = ScrapeContext(client=http, openid=credential, room_id=(room_id or "").strip())
  label: str | None = None
  if not ctx.room_id:                       # ← 只要调用方给了 room_id，finduser 这一步被完全跳过
      info = discover_room(ctx.get(FINDUSER_PATH))
      ...
  rows = F1Live().fetch(ctx)                # ← 该请求只带 roomId+openid，学校不校验归属
  ```
  ```python
  # scraper/service.py:526-545
  room_id = get_str("dorm_room_id").strip()
  ...
  if not room_id:
      info = discover_room(ctx.get(FINDUSER_PATH))
      ...
  set_state(STATE_LAST_ROOM_ID, room_id)    # ← 写进 meta，此后每轮抓取直接复用
  ```
  结合用户实测（`POST /campus/webchat/dormEm*/...` 只认 `roomId`、不校验 `openid`；`GET .../finduser?openid=`（空）会返回不属于请求者的房间 ID）：
  1. `verify` 在「调用方显式传 room_id」时**根本不会调用 finduser**，所以它的 `ok=True` 只证明「这个 roomId 能读到电量」，**完全不证明这个 room 属于该 openid**。
  2. `commit` 把该 room_id 写进 `dorm_room_id`；`run_once` 之后每轮抓取都用它（`set_state` 后长期复用，见 `scraper/service.py:545`）。
  3. `parse_url` 从粘贴的 H5 URL 里取 `roomId`（`setup_service.py:142-149`），也不做归属校验。
  → **本项目确实存在「把不属于本用户的 roomId 写进配置并长期使用」的代码路径**，且这条路径是正常业务流程（粘贴任意含 `roomId` 的链接 → verify 通过 → commit 落库），不需要任何漏洞利用技巧。
- **影响**：
  - 别人（真实宿舍住户）的剩余电量 / 每日用电 / 违规记录 / 缴费历史会被当作「本宿舍数据」抓取、绘图、推送到飞书/QQ 群，并被用于阈值告警。属**跨用户数据泄露**，且数据一旦落库就在 `/data/records.db` 中长期保存。
  - 反向：本项目自己的 roomId 也只需泄露一次即可被任意人读（上游缺陷，客户端无法完全修复）。
  - 若用户是「照着别人的链接配置的」，则整个部署的数据主体都是错的，而界面上没有任何提示。
- **修复建议**：
  1. `setup_service.verify()`：**无条件**调用 `discover_room(ctx.get(FINDUSER_PATH))`，把返回的 `info.room_id` 与入参 `room_id` 比对；不一致时返回 `{"ok": False, "error": "该房间号与当前 openid 不匹配（学校接口不校验归属，请重新粘贴本人页面的链接）"}`。把 `setup_service.py:212` 的 `if not ctx.room_id:` 改成先发现、后比对。
  2. `setup_service.commit()`：`dorm_room_id` 只允许写入「第 1 步发现/比对通过」的那个值；手工覆盖需显式传 `force=true` 并写一条 `write_audit("setup.room_override", ...)`。
  3. `scraper/service.py:530-540`：同样先 `discover_room` 再比对，不一致时 `raise ScrapeError`（而不是静默使用配置值），让问题在日志/卡片上可见。
  4. 文档层面：在 `docs/` 与配置页明确写「学校接口不校验 openid，roomId 即读取凭据；roomId 泄露 = 数据泄露」，并把该风险登记为**上游依赖的已知风险**。

---

### F-02 · 会话 Cookie 从未设置 `Secure`；`ProxyFix` 并不存在

- **严重度**：high（在「HTTP 直连暴露」的部署下等同于会话可被窃听）
- **位置**：
  - `starwatt/web/factory.py:166-174`
  - `starwatt/web/blueprints/auth_api.py:53`
  - `starwatt/web/blueprints/auth_api.py:40-42`（错误注释）
- **证据**：
  ```python
  # factory.py:166-174
  # Q21：SESSION cookie 只走 HttpOnly + SameSite，且密钥来自启动配置
  app.config.update(
      SECRET_KEY=settings.flask_secret_key,
      SESSION_COOKIE_HTTPONLY=True,
      SESSION_COOKIE_SAMESITE="Lax",
      JSON_AS_ASCII=False,
      MAX_CONTENT_LENGTH=4 * 1024 * 1024,
  )   # ← 没有 SESSION_COOKIE_SECURE
  ```
  ```python
  # auth_api.py:47-55
  response.set_cookie(SESSION_COOKIE_NAME, token, max_age=max_age, httponly=True,
                      samesite="Lax",
                      secure=bool(current_app.config.get("SESSION_COOKIE_SECURE", False)),
                      path="/")
  ```
  全仓库 grep `SESSION_COOKIE_SECURE` 仅命中 2 处：`auth_api.py:53`（读，默认 False）与 `docs/M7_CUTOVER.md:284`（文档）。**没有任何代码写入该键**，因此 `secure` 恒为 `False`。
  同理，grep `ProxyFix` 全仓库只命中 `auth_api.py:41` 的**注释**：「真实客户端 IP（`ProxyFix` 已处理 `X-Forwarded-For`）」，但 `create_app()` 里没有任何 `ProxyFix` 装配。
- **影响**：
  - 只要用户通过 `http://<ecs-ip>:5000` 访问（compose 的 `BIND_ADDR` 改成 `0.0.0.0`、或直接用镜像 ENV `GUNICORN_BIND=0.0.0.0:5000` + `docker run -p5000:5000`），登录口令与 `dorm_session` Cookie 全程明文；同一链路上的任何一跳（云 VPC 抓包、同 Wi-Fi）都能直接接管管理员会话。
  - 无 HSTS（见 F-06）时，攻击者一次 `http://` 诱导即可降级。
  - `_client_ip()` 的注释是错的：没有 ProxyFix ⇒ `request.remote_addr` 是**直连对端**（反代时是 127.0.0.1）。这本身不产生伪造 XFF 的漏洞，但会让锁定逻辑退化（见 F-04），并误导后续维护者「XFF 已可信」。
- **修复建议**：
  1. `factory.py:169` 之后加一行：
     `SESSION_COOKIE_SECURE = os.environ.get("DORM_INSECURE_COOKIE", "") != "1"`（默认 True；本地 HTTP 调试才置 `DORM_INSECURE_COOKIE=1`）。
  2. `auth_api.py:41` 的 docstring 改为「反代场景下需自行装配 `ProxyFix` 并限定可信代理；当前实现直接使用 `request.remote_addr`」；如确实要支持反代，在 `create_app()` 用 `ProxyFix(app.wsgi_app, x_for=<可信跳数>)` 并同时按 F-04 修锁定维度。
  3. 部署层：在 ECS 安全组只放 443，前面挂 nginx/Caddy 终止 TLS；`docker-compose.yml:59` 的 `BIND_ADDR` 保持默认 `127.0.0.1`。

---

### F-03 · `GET /api/admin/config?include_secrets=1` 明文返回全部凭据，且不写审计

- **严重度**：high（凭据一次性全量泄露；无痕迹）
- **位置**：
  - `starwatt/web/blueprints/admin.py:110-115`（`config_get`）
  - `starwatt/config_registry/store.py:338-342`（`decrypt(text)` 明文返回）
  - 对照：`starwatt/web/blueprints/admin.py:141-148`（`config_export` 会写审计）
- **证据**：
  ```python
  # admin.py:110-115
  @bp.get("/config")
  @admin_access
  def config_get():
      include_secrets = request.args.get("include_secrets") in ("1", "true", "yes")
      return jsonify(admin_service.config_values(include_secrets=include_secrets))
      # ← 没有 _audit(...)
  ```
  ```python
  # store.py:336-342
  if include_secrets:
      out[key] = decrypt(text) if text else ""
  else:
      out[key] = mask(decrypt(text)) if text else ""
  ```
  被这条路径一次性带出的 `secret=True` 项（`config_registry/registry.py`）：`dorm_openid`（:172）、`feishu_app_secret`（:391）、`api_internal_token`（:478）、`qq_app_secret`（:520）、`qq_bot_secret`（:524），以及 `feishu_encrypt_key` / `feishu_verification_token` 等同级项。对照 `admin.py:147`，`config/export` 会写 `_audit("config.export", details={"include_secrets": include_secrets})`，而 `config_get` 一行审计都没有。
- **影响**：
  - 任何一次管理员会话（Cookie 窃取、CSRF 之外的内鬼、误开的 `public_readonly` 之前的旧会话）都能一次 GET 拿走 openid + 飞书 AppSecret + QQ AppSecret/BotSecret + 内部 token；用这些可以直接冒充应用给全校群/QQ 发消息、以应用身份调飞书开放接口。
  - **没有任何审计记录**：事后无法回答「谁在什么时候读走了全部凭据」。
  - `GET` 无法被 CSRF（跨站读不到响应），所以不是 CSRF 问题；是**权限粒度 + 可审计性**问题。
- **修复建议**：
  1. `admin.py:110-115` 增加 `_audit("config.read", details={"include_secrets": include_secrets})`（无条件写，包括 `include_secrets=False` 的普通读取可只记 `include_secrets` 值）。
  2. 明文读取改为 `POST /api/admin/config/secrets` + `@require_csrf`（POST 才有 CSRF 保护），或要求 `X-Internal-Token`/二次口令。
  3. 更彻底：让 `include_secrets` 永不返回 `api_internal_token`（见 F-17），凭据类字段改为「只写不读」（写新值可，读只能读掩码）。

---

### F-04 · 登录失败锁定按 (username, IP) 计数 —— 反代下全站互锁，换 IP 即绕过

- **严重度**：medium
- **位置**：
  - `starwatt/auth/service.py:117-124`（`is_locked`）、`:127-130`（`record_failed`）、`:235-243`
  - `starwatt/db/repositories.py:723-734`（`count_since`）、`:736-745`（`clear`）
  - `starwatt/web/blueprints/auth_api.py:40-42`（`ip = request.remote_addr`）
- **证据**：
  ```python
  # repositories.py:723-731
  sql = ("SELECT COUNT(*) AS n FROM failed_attempts "
         "WHERE username = ? AND attempted_at >= ?")
  params = [username, since]
  if ip:
      sql += " AND ip = ?"        # ← 计数被 IP 切分
      params.append(ip)
  ```
  ```python
  # auth/service.py:117-124
  return FailedAttemptRepo.count_since(username, _window_start(), ip) >= lockout_threshold()
  # _window_start() = now - lockout_minutes()   ← 「锁 15 分钟」其实是「15 分钟滑动窗口内 ≥5 次」
  ```
  配合 `auth_api.py:42` 的 `return request.remote_addr`（无 ProxyFix ⇒ 反代后恒为 `127.0.0.1`）。
- **影响**：
  1. **分布式爆破不受限**：攻击者用 N 个出口 IP 时，每 IP 每 15 分钟可试 5 次，总速率不限；慢速爆破（≤4 次/15 分钟 ≈ 384 次/天）永不触发锁定。密码策略仅「≥8 位 + 1 大写 + 1 数字 + 1 特殊字符」（`auth/password.py:145-161`），在线爆破可行。
  2. **反代下变成全站互锁（DoS）**：nginx 反代时所有用户共享同一个 `remote_addr`，任何人故意错 5 次即可把 `admin` 用户名对**所有人**锁 15 分钟 —— 而 `admin` 是 bootstrap 唯一账号（`auth/service.py:354`）。
  3. 响应确实不区分「用户不存在/密码错」，但见 F-05 的时序问题。
- **修复建议**：
  1. 计数维度拆成两条并取最严：`FailedAttemptRepo.count_since(username, since)`（忽略 ip，防单账号爆破）**且** `count_since_by_ip(ip, since)`（防撞库/喷洒）。给 `FailedAttemptRepo` 加一个 `count_since_ip(ip, since)`（新 SQL，参数化）。
  2. 真正的「锁定」与「窗口」分离：失败达阈值后写一条 `locked_until`（或复用 `meta` 键），到期前直接拒绝，而不是继续依赖滚动窗口。
  3. 反代部署：装配 `ProxyFix(app.wsgi_app, x_for=1)` 并**同时**限定只有可信代理能连到容器（ECS 安全组只放反代内网 IP），否则 `remote_addr` 恒为代理 IP，锁定的可用性依然不成立。
  4. 顺带：`auth/service.py:236` 的 `login.locked` 与 `:242` 的 `login.failed` 已入审计（正面），修复后应保留。

---

### F-05 · 用户名枚举时序侧信道

- **严重度**：medium
- **位置**：`starwatt/auth/service.py:239-243`；`starwatt/auth/password.py:99-127`
- **证据**：
  ```python
  # auth/service.py:239-243
  user = UserRepo.get_by_username(username)
  if user is None or not pwd.verify_password(password, user.password_hash):
      record_failed(username, ip)
      write_audit("login.failed", ...)
      return AuthResult(ok=False, error="invalid")
  ```
  短路求值：`user is None` 时不执行 `pwd.verify_password`。而 `verify_password` 会跑 `hashlib.scrypt(n=2**14, r=8, p=1, dklen=32)`（`password.py:85-87`，注释自称 ~100ms）。因此「不存在的用户名」响应时间在毫秒级，「存在的用户名」在 ~100ms 级 —— 差两个数量级，无需统计技巧即可区分。
- **影响**：攻击者可先枚举出有效用户名（本项目用户名空间小，`admin` 几乎必然存在），再集中爆破；也让 F-04 的锁定策略可以只打有效账号。
- **修复建议**：`auth/service.py:239` 改为恒定工作量：
  ```python
  user = UserRepo.get_by_username(username)
  stored = user.password_hash if user is not None else _DUMMY_HASH   # 启动时 pwd.hash_password("x") 生成一次的常量
  ok = pwd.verify_password(password, stored)
  if user is None or not ok:
      ...
  ```
  （`_DUMMY_HASH` 建议放 `auth/password.py`，用真实 scrypt 参数生成，保证耗时一致。）

---

### F-06 · 全站没有任何安全响应头

- **严重度**：medium
- **位置**：`starwatt/web/factory.py`（`create_app()` 全文无 `after_request`/`before_request` 注入）
- **证据**：grep 全仓库 `CSP|X-Frame-Options|Strict-Transport-Security|X-Content-Type-Options|after_request`，**唯一命中**是 `docs/DEPLOY_PAAS.md:103`「后端不需要开 CORS ✓」——即代码里一处都没有。
- **影响**：
  - 无 `X-Frame-Options`/`frame-ancestors` → 可被任意站点 iframe 嵌套，配合「刷新」「删除用户」「恢复默认配置」这类无确认的 POST 按钮构成点击劫持（CSRF token 是自动带的，点击即生效）。
  - 无 `Strict-Transport-Security` → 与 F-02 叠加构成明文降级。
  - 无 `X-Content-Type-Options: nosniff` → 若某天 `static/` 下出现可上传/可控文件，MIME 嗅探可导致 XSS；当前无上传接口（见「已检查未发现」），属预防性。
- **修复建议**：在 `create_app()` 末尾加：
  ```python
  @app.after_request
  def _security_headers(resp):
      resp.headers.setdefault("X-Content-Type-Options", "nosniff")
      resp.headers.setdefault("X-Frame-Options", "DENY")
      resp.headers.setdefault("Referrer-Policy", "no-referrer")
      resp.headers.setdefault("Content-Security-Policy",
                              "default-src 'self'; img-src 'self' data:; "
                              "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
      if request.is_secure:
          resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
      return resp
  ```
  （`style-src 'unsafe-inline'` 先保留：前端若用内联样式会被 CSP 打掉，需先在 `frontend/` 里确认——见「未能验证」。）

---

### F-07 · SSRF 逃生舱 `allow_private_hosts` 一旦打开，防护只剩 L1

- **严重度**：medium（默认关；但它是**WebUI 里可点的开关**）
- **位置**：
  - `starwatt/scraper/ssrf.py:196-219`（`assert_public_url`：`if allow_private: return hostname`）
  - `starwatt/config_registry/registry.py:166`：`"allow_private_hosts", "允许访问内网地址", SCRAPE, T.BOOL, False`
  - 调用方：`starwatt/services/setup_service.py:119`、`:196`；`starwatt/scraper/client.py:199`、`:273`
- **证据**：
  ```python
  # ssrf.py:206-218
  hostname = check_scheme(url)          # L1
  if allow_private:
      logger.warning("allow_private_hosts 已开启 —— 跳过内网地址检查（%s）", hostname)
      return hostname                   # ← L2/L3 全部跳过，且 L4 也用同一个 flag
  check_host_shape(hostname)            # L2
  for ip in resolve_ips(hostname):      # L3
      if is_blocked_ip(ip):
          raise SsrfError(...)
  ```
  `client.py:199` 与 `:273` 传的是同一个 `allow_private=self.allow_private_hosts` ⇒ 重定向逐跳复检也被关闭（L4 失效）。
- **影响**：开关打开后，管理员（或任何拿到 `PUT /api/admin/config` 的人）可以把 `dorm_base_url` 指向 `http://127.0.0.1:<port>/`、`http://192.168.x.x/`、`http://169.254.169.254/latest/meta-data/…`，服务端会主动请求并把响应体写进 DB / 日志 / 飞书卡片（`ScrapeError` 里还会截取响应前 120 字符，见 `client.py:159-162`）⇒ 内网端口探测 + 云元数据窃取。阿里云元数据 `100.100.100.200` 恰好落在 `100.64.0.0/10` 内（`ssrf.py:70`）——**只有开关关闭时**才被拦，开启后即刻可达。
- **修复建议**：
  1. 把 `allow_private` 从「跳过 L2/L3」改为「允许私网但仍拒绝云元数据/链路本地」：新增 `METADATA_NETWORKS = (169.254.0.0/16, 100.64.0.0/10, fd00:ec2::254/128)`，在 `assert_public_url` 的 `allow_private` 分支里仍然检查这些网段。
  2. `registry.py:166` 的默认值保持 `False`（已是），但把该开关从 WebUI「设置」页移出（或要求环境变量 `ALLOW_PRIVATE_HOSTS=1` 才生效，避免一次误点长期生效）。
  3. `client.py:163-175` 的 `_headers()` 会把 `self.base_url` 作为 `Referer`/`Origin` 发出去（该 URL 来自配置），在私网场景下等于把内网地址告诉对面 —— 属附带信息泄露，可接受但需知晓。

---

### F-08 · SSRF 各层的具体可达/不可达判定（已用 .venv 实测）

- **严重度**：medium（结论：**没有无需 DNS 控制的完整绕过**，但有两个真实缺口与一个已自认的边界）
- **位置**：`starwatt/scraper/ssrf.py:104`（`_IPV4_LITERAL`）、`:138-162`（L2）、`:168-174`（`is_blocked_ip`）、`:177-193`（`resolve_ips`）
- **证据**（实跑命令：`@'…'@ | .\.venv\Scripts\python.exe -`，纯 stdlib、无网络访问；以下为原始输出要点）：
  ```
  === check_host_shape ===
    '127.0.0.1'        -> BLOCK （请填写域名而不是 IP 地址）
    '10.0.0.1'         -> BLOCK
    'LOCALHOST'        -> BLOCK ('localhost' 精确匹配前先 lower())
    'localhost.'       -> BLOCK (rstrip(".") 生效)
    '::1'              -> BLOCK （":" in host 分支）
    '::ffff:127.0.0.1' -> BLOCK
    '[::1]'            -> BLOCK
    'metadata.google.internal' -> BLOCK
    'x.internal'       -> BLOCK（后缀 .internal）
    '127.1'            -> PASS(L2)
    '2130706433'       -> PASS(L2)      ← 十进制 IP
    '0x7f.0.0.1'       -> PASS(L2)      ← 十六进制
    '0177.0.0.1'       -> PASS(L2)      ← 八进制
    '0'                -> PASS(L2)
    '127.0.0.1.nip.io' -> PASS(L2)      ← 通配 DNS 指回环
    'evil.com.'        -> PASS(L2)      ← 尾点
  === urlparse().hostname ===
    'http://user:pass@127.0.0.1/'  hostname='127.0.0.1'          → L2 拦下（userinfo 不构成绕过）
    'http://127.0.0.1@evil.com/'   hostname='evil.com'           → 指向 evil.com，无越权
    'http://[::ffff:127.0.0.1]/'   hostname='::ffff:127.0.0.1'   → 含 ":" → L2 拦下
    'http://evil.com@169.254.169.254/' hostname='169.254.169.254' → L2 拦下
  === is_blocked_ip ===
    '127.0.0.1' True / '0.0.0.0' True / '169.254.169.254' True
    '100.100.100.200' True      ← 阿里云元数据，靠 100.64.0.0/10 命中
    '::1' True / '::' True / 'fd00::1' True / 'fe80::1' True / '2001:db8::1' True
    '::ffff:127.0.0.1' False    ← ⚠️ 缺口
    '::ffff:7f00:1'    False    ← ⚠️ 缺口
    '64:ff9b::7f00:1'  False    ← ⚠️ NAT64 缺口
    '0x7f000001' True           ← ip_address() 抛 ValueError → 判为不安全（fail-closed，好）
    '0177.0.0.1'  True          ← 同上
  === ipaddress 语义 ===
    '::ffff:127.0.0.1' in 127.0.0.0/8 -> False   （Python 的 v6-in-v4 包含判断恒为 False）
  ```
  **判定汇总**：
  - **不能触发**（被拦）：`127.0.0.1` / `10.0.0.1` / `192.168.1.1` / `localhost` / `LOCALHOST` / `localhost.` / `x.internal` / `metadata.google.internal` / `http://[::1]/` / `http://[::ffff:127.0.0.1]/` / `http://evil.com@169.254.169.254/` / `http://user:pass@127.0.0.1/`。
  - **L2 放行但 L3 会拦**（前提是解析到内网）：`127.1`、`2130706433`、`0x7f.0.0.1`、`0177.0.0.1`、`0`、`127.0.0.1.nip.io` —— 只要 `getaddrinfo` 把它们解析成 `127.0.0.1`，`is_blocked_ip` 返回 True ⇒ `SsrfError`，**不构成绕过**；若解析失败则 `resolve_ips` 抛 `SsrfError`（fail-closed），同样不构成绕过。
  - **真实缺口 A（IPv4-mapped / NAT64）**：`BLOCKED_NETWORKS`（`ssrf.py:67-89`）没有 `::ffff:0:0/96`、`64:ff9b::/96`，而 `ipaddress` 的跨版本包含判断返回 False ⇒ `is_blocked_ip("::ffff:127.0.0.1") == False`。**只能**通过「域名 AAAA 记录解析出 `::ffff:7f00:1`」这条路径到达 L3（URL 字面量会被 L2 的 `":" in host` 拦下）。门槛：需要攻击者控制 DNS 或在校内做 AAAA 投毒；一旦成功即可打到 `127.0.0.1`。
  - **真实缺口 B（DNS rebinding TOCTOU）**：`resolve_ips()` 解析一次，`requests` 随后**自己再解析一次**（`client.py:252-260` 直接交给 `requests`），两次结果可以不同。`ssrf.py:34-36` 的 docstring **已如实承认**这个边界（「严格的 DNS rebinding 防护还要『用解析出的 IP 连接、但带原 Host 头』……这里如实记录这个边界」）。
  - **尾点/大小写/userinfo**：均**不构成**绕过（实测）。
- **修复建议**：
  1. `ssrf.py:67-89` 追加两个网段：`ipaddress.ip_network("::ffff:0:0/96")`、`ipaddress.ip_network("64:ff9b::/96")`；并加断言测试 `is_blocked_ip("::ffff:127.0.0.1") is True`。
  2. 消除 TOCTOU：在 `SchoolClient._request_once` 里 `assert_public_url` 返回 hostname 后，用 `resolve_ips(hostname)` 取一个 IP，把 URL 的 host 换成该 IP、同时显式设置 `Host: <hostname>` 头（`requests` 支持 `headers["Host"]`），或退一步：`allow_redirects=False` 已做，至少把「DNS 结果在本次请求内固定」这件事记进注释与测试。
  3. 把 `check_host_shape` 的裸 IP 判定从正则升级为 `ipaddress.ip_address()` 尝试解析（对 `2130706433` Python 也会抛 ValueError，但可先试 `int(host)`）。

---

### F-09 · 依赖与基础镜像未固定版本

- **严重度**：medium
- **位置**：`requirements.txt:16-33`；`Dockerfile:24`（`FROM node:22-alpine`）、`:40`/`:63`（`FROM python:3.12-slim`）
- **证据**：
  ```
  flask>=3.0            gunicorn>=21.0        requests>=2.31
  python-dotenv>=1.0    APScheduler>=3.10,<4.0   tzdata>=2024.1
  pycryptodome>=3.18    pillow>=10.0
  ```
  全部为下限约束，无 `==`、无 `--hash`；两个基础镜像均为浮动 tag（无 `@sha256:`）。`regex`：`Dockerfile:29` 用的是 `npm ci`（基于 `frontend/package-lock.json`）——前端这条链是**好**的。
- **影响**：同一份代码在不同时间构建出不同依赖；上游任一包被投毒或引入不兼容变更都会**直接进入生产镜像**，且无法通过版本号复现或回滚到「上次构建用的那套」。容器只读根 + 非 root 缓解了运行时提权，但不缓解构建期依赖投毒。
- **修复建议**：
  1. 生成锁文件：`pip-compile --generate-hashes requirements.in`（或 `pip freeze > requirements.lock`），`Dockerfile:57` 改为 `pip install --require-hashes --prefix=/install -r requirements.lock`。
  2. 基础镜像固定 digest：`FROM python:3.12-slim@sha256:<...> AS runtime`（builder 与 node 阶段同理）。
  3. 在 CI/`scripts/` 加一条「锁文件与 requirements 不同步则失败」的检查。

---

### F-10 · 绕过 entrypoint 启动时，`FLASK_SECRET_KEY` 每次重启都会重新生成

- **严重度**：medium
- **位置**：
  - `docker-entrypoint.sh:46-58`（正常路径：把密钥持久化到 `/data/.flask_secret_key`）
  - `starwatt/auth/secret.py:96-118`（兜底：缺密钥则生成并**尝试写回 `.env`**，失败只 warning）
  - `starwatt/config_registry/store.py:95-101` / `starwatt/config_registry/secrets.py:150-157`（解密失败抛 `SecretDecryptError`）
- **证据**：
  ```python
  # secret.py:105-118
  generated = secrets.token_hex(32)
  os.environ[ENV_SECRET_KEY] = generated
  if _persist_to_env(ENV_SECRET_KEY, generated):
      logger.warning("... 已自动生成并写入 .env（请备份 ...）")
  else:
      logger.warning("... 缺失且无法写回 .env，本次运行使用临时密钥 —— 重启后所有已签发的会话将失效")
      # ← 只 warning，继续启动
  ```
  容器 `read_only: true`（`docker-compose.yml:69`）⇒ `/app/.env` 写不进去 ⇒ 走 else 分支。`docker-entrypoint.sh` 会兜住这条路径，但只要用户用 `docker compose run --entrypoint gunicorn ...`、改成 systemd 直跑、或在 `docker-compose.yml` 里覆盖 `entrypoint`，就完全没有保护。
- **影响**：每次重启换密钥 ⇒ (a) 所有 `enc:v1:` 密文（openid / 飞书凭据 / QQ 凭据 / 内部 token）**永久解不开**（`decrypt` 抛 `SecretDecryptError`，不是静默降级 —— 这点是设计上的优点，但用户会看到配置页/接口 500 或报错）；(b) 所有已签发会话立刻失效；且用户很难从「只读文件系统」这个 warning 联想到后果。
- **修复建议**：把「临时密钥」从 warning 升级为**启动即失败**（fail-fast）：
  ```python
  # secret.py:113-118 改为
  raise RuntimeError(
      "FLASK_SECRET_KEY 缺失且无法持久化（只读文件系统？）。"
      "要么设置环境变量 FLASK_SECRET_KEY，要么让 /data 可写并由 entrypoint 生成密钥。"
      "继续启动会导致配置中心里所有加密凭据永久无法解密。")
  ```
  同时在 `web.py`/`create_app()` 的启动日志里打印密钥来源（env / 数据卷文件 / 临时——后者不允许出现）。

---

### F-11 · Cookie 寿命与 DB 会话寿命取自两个不同配置源

- **严重度**：low
- **位置**：`starwatt/web/blueprints/auth_api.py:152-154`；`starwatt/auth/secret.py:50-52`；`starwatt/auth/constants.py:35`
- **证据**：
  ```python
  # auth_api.py:152-154
  def _session_max_age() -> int:
      return int(current_app.config.get("SESSION_HOURS", 24)) * 3600
  ```
  `SESSION_HOURS` 从未被写入 `app.config`（与 F-02 同一处 `app.config.update`）⇒ 恒为 24h。而 DB 侧过期时间由 `session.create_session()` 使用 `starwatt.auth.secret.session_hours()`，读取的是**环境变量** `AUTH_SESSION_HOURS`（`constants.py:48`），默认 24（`constants.py:35`）。
- **影响**：把 `AUTH_SESSION_HOURS=720`（配置中心/文档暗示可调）后，服务端会话活 30 天而浏览器 Cookie 24h 就丢，用户被莫名登出；反过来设 `AUTH_SESSION_HOURS=2` 时浏览器会长期携带一个已失效 Cookie（多一次 401 往返）。两个「同一个东西」的配置源不一致，属可维护性 + 安全预期（会话时长可被误解）问题。
- **修复建议**：`auth_api.py:152-154` 改为 `from starwatt.auth import secret as secret_mod` → `return secret_mod.session_hours() * 3600`；并删掉 `app.config` 里对 `SESSION_HOURS` 的隐式期待（或在 `factory.py` 显式 `SESSION_HOURS=settings.session_hours`）。

---

### F-12 · 注销清 Cookie 属性不匹配 + 注销不入审计

- **严重度**：low
- **位置**：`starwatt/web/blueprints/auth_api.py:59-62`、`:88-94`
- **证据**：
  ```python
  # auth_api.py:59-62
  def clear_session_cookie(response):
      response.delete_cookie(SESSION_COOKIE_NAME, path="/")   # 未带 secure/samesite/httponly
  ```
  ```python
  # auth_api.py:88-94
  @bp.post("/logout")
  @require_auth()
  @require_csrf
  def logout():
      result = auth_service.logout(getattr(g, "session_token", None))
      return clear_session_cookie(jsonify(result))           # ← 没有任何 write_audit
  ```
  服务端会话行确实被删除（`session.revoke_session`），所以安全性主要依赖服务端；但 `delete_cookie` 不带与 `set_cookie` 相同的属性，部分浏览器/代理会留下一个同名残留 Cookie（值相同、已失效），在 F-11 的时长不一致场景下更易混淆。审计方面：`write_audit` 的全部调用点见 F-14，`logout` 不在其中。
- **修复建议**：`delete_cookie(SESSION_COOKIE_NAME, path="/", secure=<同 set>, samesite="Lax", httponly=True)`；在 `logout()` 返回前加 `auth.write_audit("logout", user_id=user.id, target=user.username, ip=request.remote_addr)`（需先取 `current_user()`）。

---

### F-13 · `base_url` 用 `netloc` 拼接 —— `user:pass@` 会被持久化并作为 Referer/Origin 发出

- **严重度**：low
- **位置**：`starwatt/services/setup_service.py:148`；下游 `starwatt/scraper/client.py:171-176`
- **证据**：
  ```python
  # setup_service.py:148
  "base_url": f"{parsed.scheme}://{parsed.netloc}",
  ```
  实测：`urlparse("http://user:pass@127.0.0.1/").netloc == 'user:pass@127.0.0.1'`。host 白名单用的是 `parsed.hostname`（`setup_service.py:115`、`:87`），所以白名单判断本身正确；但 `userinfo` 会被原样写进 `dorm_base_url`（经 `commit` → `config_update`），随后在 `client.py:173-175` 成为 `Referer` 与 `Origin` 请求头发给学校服务器（以及任何被 L4 允许的重定向目标）。
- **影响**：用户在 H5 链接里带 Basic-Auth userinfo 时，凭据会被写进 `meta` 表（并可能被 `?include_secrets=1` 导出，见 F-03）并对外发送；属信息泄露，非直接越权。
- **修复建议**：`setup_service.py:148` 改为
  ```python
  host = parsed.hostname or ""
  port = f":{parsed.port}" if parsed.port else ""
  "base_url": f"{parsed.scheme}://{host}{port}",
  ```
  并在 `parse_url` 末尾对 `parsed.username`/`parsed.password` 非空时记一条 warning（该链接可能包含凭据）。

---

### F-14 · 审计覆盖面：补了哪些、缺了哪些

- **严重度**：low（但直接影响事后追溯能力）
- **位置**（`write_audit` 的全部调用点，已用 grep 穷举）：
  - `starwatt/auth/service.py:236` `login.locked`、`:242` `login.failed`、`:246` `login.disabled`、`:263` `login.success`、`:364` `bootstrap.admin_created`
  - `starwatt/web/blueprints/admin.py:73`（`_audit` 助手，被 `config.update` `:125`、`config.test` `:137`、`config.export` `:147`、`config.import` `:159`、`config.reset` `:176`、`user.create` `:218`、`user.role` `:241`、`user.disabled` `:246`、`user.password_reset` `:262`、`user.delete` `:274` 调用）
  - `starwatt/web/blueprints/auth_api.py:114` `password.change_failed`、`:127` `password.changed`
  - `starwatt/web/blueprints/setup.py:43`（`setup.parse_failed` `:61`、`setup.parsed` `:64`、`setup.verify` `:79`、`setup.commit` `:101`）
  - `starwatt/web/blueprints/oobe.py:70` `oobe.advance`
- **缺口**（都在代码里核对过，对应函数内确实没有 `_audit`/`write_audit`）：
  1. **明文读凭据**：`admin.py:110-115` `GET /api/admin/config?include_secrets=1`（对应 F-03，最严重的一条缺口）。
  2. **注销**：`auth_api.py:88-94`。
  3. **触发抓取**：`dashboard.py:136` `POST /api/refresh`（会真访问学校接口并写库，属写操作）。
  4. **CSRF 失败**：`auth/decorators.py:171` 只有 `logger.warning("CSRF 校验失败：...")`，不进 `audit_log` —— 导致「有人在尝试 CSRF」这件事无法被长期检索。
  5. **读审计本身**：`admin.py:278-288` `GET /api/admin/audit`；以及 `admin.py:190-194` `GET /api/admin/logging`、`:183-187` `GET /api/admin/flags`。
  6. **`audit_log` 无保留策略/无防篡改**：`starwatt/db/schema.py:173-185` 只有索引；`FailedAttemptRepo.purge_before`（`repositories.py:747-752`）有清理，`AuditRepo` 没有对应清理，但也**没有任何 API 能删审计**（正面）。DB 文件本身可被有 `/data` 写权限的进程直接改写（属部署信任边界，不是应用缺陷）。
- **修复建议**：在 `admin.py:115` 前加 `_audit("config.read", details={"include_secrets": include_secrets})`；`auth_api.py:94` 前加 `logout` 审计；`dashboard.py` 的 `api_refresh` 内加 `_audit("scrape.manual")`；`decorators.py:171` 改为同时 `write_audit("csrf.failed", ip=request.remote_addr, target=request.path)`（注意避免在这里引入循环导入，用函数内 import）。

---

### F-15 · `/healthz` 匿名公开版本号

- **严重度**：low
- **位置**：`starwatt/web/blueprints/health.py:27-30`
- **证据**：`return jsonify(status="ok", version=APP_VERSION, time=stamp())`，路由无任何装饰器（`health.py:19` 的 blueprint 无 `url_prefix`）。
- **影响**：未认证的版本指纹，便于攻击者按版本挑 CVE。属可接受的运维取舍（Docker HEALTHCHECK 与 systemd 都要匿名探活），但**不必**把版本暴露给公网。
- **修复建议**：公网入口（nginx/Caddy）只把 `/healthz` 暴露给本机/内网，或把 `version` 字段改为仅当 `?verbose=1` 且来自 `127.0.0.1` 时返回。

---

### F-16 · 初始管理员密码被打印进容器日志

- **严重度**：low
- **位置**：`docker-entrypoint.sh:80-97`（heredoc 里直接 `初始密码 ：${BOOTSTRAP_ADMIN_PASSWORD}`）；对照 `:73-78`（落盘用 `umask 077` + `chmod 600`，这部分是**好**的）
- **影响**：`docker compose logs` / journald / 日志采集（阿里云 SLS 之类）会长期保存明文管理员密码，直到用户首次改密（`auth/service.py:301-323` 会删掉密码文件，但**日志删不掉**）。
- **修复建议**：`docker-entrypoint.sh:86` 改为不打印密码本体，只打印「初始密码已写入 `/data/.initial-admin-password`（`./starwatt-docker.sh password` 可读取）」；`docs/`/compose 注释里的 `docker compose logs | grep 初始密码` 也要一并改掉（`docker-compose.yml:13`）。

---

### F-17 · `X-Internal-Token` 是静态永久凭据，且可从 `include_secrets` 读出

- **严重度**：low
- **位置**：`starwatt/web/security.py:92-121`；`starwatt/config_registry/registry.py:478`；可读路径 `starwatt/web/blueprints/admin.py:110-115`
- **证据**：
  ```python
  # security.py:92-98
  guarded = require_admin(require_csrf(view))
  def wrapper(*args, **kwargs):
      if internal_token_ok():
          return view(*args, **kwargs)     # ← 绕过 admin 与 CSRF
      return guarded(*args, **kwargs)
  ```
  `internal_token_ok`（`:111-121`）用 `hmac.compare_digest`（**常量时间，正确**），未配置时 `return False`（**fail-closed，正确**）。
- **影响**：该 token 是静态的、无过期、无轮换机制；一旦泄露（F-03 的 `include_secrets=1`、配置导出、日志、备份）即可在不持有任何会话的情况下反复触发 `POST /api/refresh`（真实访问学校接口 + 写库），而且**无法在不改配置的情况下吊销**。
- **修复建议**：
  1. 在 `store.all_values()`（`config_registry/store.py:315-342`）里对 `api_internal_token` 强制走 `mask()` 分支（即使 `include_secrets=True` 也不返回明文）。
  2. `security.py` 增加「token 轮换」：记录 `api_internal_token_last_used`（`meta` 状态键）+ 允许在 WebUI 一键重新生成；使用该 token 时写审计（便于发现滥用）。

---

### F-18 · 平台回调验签不校验时间戳新鲜度 → 可重放

- **严重度**：low
- **位置**：
  - `starwatt/notify/qq.py:339-368`（`verify_signature`：只验 Ed25519 签名，`timestamp` 仅作为签名体的一部分，**未做窗口校验**）
  - `starwatt/notify/crypto.py:194-233`（`verify_lark_signature`：同上，`ts`/`nonce` 只参与摘要计算）
  - 入口：`starwatt/web/blueprints/feishu.py:57-76`（`/qq/events`）、`:79-100`（`/feishu/event`）
- **证据**：
  ```python
  # qq.py:355-368
  signature = (headers or {}).get(SIGNATURE_HEADER, "") or ""
  timestamp = (headers or {}).get(TIMESTAMP_HEADER, "") or ""
  if not (signature and timestamp):
      return False                       # fail-closed（好）
  ...
  public = ed25519.public_key_from_seed(ed25519.seed_from_secret(secret))
  return ed25519.verify(public, timestamp.encode("utf-8") + body, raw_signature)
  # ↑ 没有任何 time.time() - int(timestamp) < WINDOW 的判断
  ```
  两条 fail-closed 路径都在（未配置 `qq_bot_secret` → 拒绝，`qq.py:351-353`；缺签名头 → 拒绝）；`feishu.py:84` 调 `dispatcher.verify_request(headers, raw)` **未传** `allow_unsigned=True`，`dispatcher.py:114` 的默认值是 `False` ⇒ 不存在「忘了开验签」的降级。
- **影响**：任何被记录下来的合法回调（同网络抓包、日志中的请求体）可被无限次重放。目前 `notify/commands.py` 只做**查询**（`cmd_remain`/`cmd_meter`/`cmd_today`/`cmd_history`/`cmd_pay`/`cmd_violations`，见 `commands.py:183-306`），没有配置写入或管理员动作，所以重放的直接后果是「重复回复消息」+ 无谓的 DB 查询（可被用来做放大/骚扰）。
- **修复建议**：两处都加时间窗校验，例如 `if abs(time.time() - int(timestamp)) > 300: return False`（`qq.py:357` 之后、`crypto.py:226` 之后）；并对 `nonce`（飞书）做一个有界的已用集合（内存 LRU 1000 条足够）以防窗口内重放。

---

### F-19 · 机器人命令不校验发送者身份（仅查询，无写操作）

- **严重度**：low
- **位置**：`starwatt/notify/commands.py:362-392`（`dispatch_text` / `dispatch_menu`）、`starwatt/notify/qq.py`、`starwatt/notify/dispatcher.py` 的事件分发
- **证据**：grep `set_value|config_update|write_audit|ADMIN|allowed` in `starwatt/notify/` → **No matches found**；命令层只 `get_str("last_room_id")`（`commands.py:141-146`）并读库。
- **影响**：企业/群内任何能给机器人发消息的人都能拉到本宿舍的电量、用电、违规、缴费数据（数据本身与被审计的 Web 端同一份）。因为命令层没有写能力，风险面仅限于**读取**；但「谁能读」= 「机器人所在会话的所有成员」，与 Web 端「默认需登录」的假设不同。
- **修复建议**：明确这是刻意取舍还是遗漏。若要收敛：在 `commands.py:362` 的入口按 `sender_open_id` 白名单（新增 `bot_allowed_open_ids` 配置项）过滤，非白名单只回 `cmd_help()`。

---

## 三、已检查但未发现问题的清单（覆盖度说明）

**认证与授权**
- `starwatt/auth/session.py`：session token 用 `secrets.token_urlsafe(32)` 生成，DB 只存 `sha256(token)`（`schema.py:149-150` 注释与 `repositories.py:566-614` 一致）；`_is_expired` 同时判空闲过期与绝对上限（720h，`session.py` 的 `ABSOLUTE_MAX_HOURS`）；过期行会被删除；用户被禁用/删除/改密后 `revoke_all_for_user` 全部踢下线（`auth/service.py:179/195/208/213`）。**未发现会话固定（session fixation）问题**：登录时 `create_session` 生成全新 token，不复用请求中带来的旧 token。
- `starwatt/auth/password.py`：`hashlib.scrypt(N=2^14, r=8, p=1, dklen=32)` + 16 字节随机盐 + 自描述格式（`:90-96`）；`verify_password` 用 `hmac.compare_digest`（`:127`）；未知格式（含旧 bcrypt `$2b$`）一律返回 False 不抛异常（`:108-120`）；越界参数不崩（`:124-126`）。**未发现弱哈希/明文/可预测盐**。
- `starwatt/auth/csrf.py`：`HMAC-SHA256(secret_key, session_token)`（`:49-53`），`validate` 用 `hmac.compare_digest`（`:60`），任一侧缺失即 False（`:58-59`）；GET/HEAD/OPTIONS/TRACE 放行（`:39`）。**未发现 CSRF token 可预测/可跨会话复用**（token 绑定 session token，登录换 session 即换 CSRF token）。
- 装饰器顺序：`blueprints/admin.py:118-120`、`setup.py:52-54`、`oobe.py:50-52`、`auth_api.py:88-90` 均为「`admin_access`/`require_auth` 在外、`require_csrf` 在内」⇒ 鉴权先于 CSRF 执行，**顺序正确**。
- `must_change_password` 拦截：`decorators.py:106-112` 用**精确路径**比对（`request.path.rstrip("/") in MUST_CHANGE_ALLOWED_PATHS`），而不是前缀匹配 —— 已刻意规避 `/api/auth/me/../../admin/config` 这类绕过；放行集合只有 3 条（`:65-71`）。
- 「最后一个管理员」护栏：`services/auth_service.py:148-153/166-171/196-201`（降级/禁用/删除都拦）+ `admin.py:235-236/271-272`（不能禁用/删除自己）。**未发现管理员自锁或权限提升路径**。
- 前端 CSRF 装配：`frontend/src/api/client.ts:115` 会带 `X-CSRF-Token`，token 来自 `frontend/src/stores/auth.ts:72`（`GET /api/auth/me`）；`tests/unit/test_web_api.py:336` 也验证了「不带 token 的 `POST /api/refresh` → 403」。

**Web 层**
- **路由鉴权矩阵**（穷举 `@bp.*` 与 `@app.*`，全部 route 都在下表）：

  | 方法 | 路径 | 鉴权 | 备注 |
  |---|---|---|---|
  | GET | `/healthz` | **无** | 有意为之（`health.py:8`、`:27`） |
  | GET | `/` | 无 | 静态 `static/index.html`（`factory.py:95`） |
  | GET | `/<path:path>` | 无 | SPA 回退；保留前缀（`api/ static/ feishu/ qq/ healthz`）先 `abort(404)`（`factory.py:61`、`:99-101`） |
  | POST | `/api/auth/login` | **无** | 登录入口，不应有 CSRF（`auth_api.py:65`） |
  | POST | `/api/auth/logout` | `require_auth` + `require_csrf` | `auth_api.py:88-90` |
  | POST | `/api/auth/password` | `require_auth` + `require_csrf` | `auth_api.py:97-99` |
  | GET | `/api/auth/me` | `require_auth(allow_anonymous_read=True)` | `auth_api.py:135-136` |
  | GET | `/api/data` | `read_access` | `dashboard.py:70-71` |
  | GET | `/api/live` | `read_access` | `dashboard.py:82-83` |
  | GET | `/api/site` | **无** | 只返回 `site_name`/`theme_color`/`app_version`（`dashboard_service.py:500-522`）——已核对**无**数据字段 |
  | GET | `/api/violations` | `read_access` | `dashboard.py:110-111` |
  | GET | `/api/payments` | `read_access` | `dashboard.py:117` |
  | GET | `/api/daily` | `read_access` | `dashboard.py:129` |
  | POST | `/api/refresh` | `refresh_access`（admin+CSRF 或 `X-Internal-Token`） | `dashboard.py:136`，见 F-17 |
  | GET | `/api/admin/config/schema` | `admin_access` | `admin.py:103-104` |
  | GET | `/api/admin/config` | `admin_access` | `admin.py:110-111`，见 F-03 |
  | PUT | `/api/admin/config` | `admin_access` + CSRF | `admin.py:118-120` |
  | POST | `/api/admin/config/test` | `admin_access` + CSRF | `admin.py:130-132` |
  | GET | `/api/admin/config/export` | `admin_access`（无 CSRF） | `admin.py:141-142` |
  | POST | `/api/admin/config/import` | `admin_access` + CSRF | `admin.py:151-153` |
  | POST | `/api/admin/config/reset` | `admin_access` + CSRF | `admin.py:170-172` |
  | GET | `/api/admin/flags` | `admin_access` | `admin.py:183-184` |
  | GET | `/api/admin/logging` | `admin_access` | `admin.py:190-191` |
  | GET | `/api/admin/users` | `admin_access` | `admin.py:200-201` |
  | POST | `/api/admin/users` | `admin_access` + CSRF | `admin.py:207-209` |
  | PATCH | `/api/admin/users/<int:user_id>` | `admin_access` + CSRF | `admin.py:226-228` |
  | POST | `/api/admin/users/<int:user_id>/password` | `admin_access` + CSRF | `admin.py:256-258` |
  | DELETE | `/api/admin/users/<int:user_id>` | `admin_access` + CSRF | `admin.py:266-268` |
  | GET | `/api/admin/audit` | `admin_access` | `admin.py:278-279` |
  | GET | `/api/oobe/state` | `admin_access` | `oobe.py:43-44` |
  | POST | `/api/oobe/advance` | `admin_access` + CSRF | `oobe.py:50-52` |
  | POST | `/api/setup/parse-url` | `admin_access` + CSRF | `setup.py:52-54` |
  | POST | `/api/setup/verify` | `admin_access` + CSRF | `setup.py:68-70` |
  | POST | `/api/setup/commit` | `admin_access` + CSRF | `setup.py:83-85` |
  | POST | `/qq/events` | 平台 Ed25519 签名（fail-closed） | `feishu.py:57-64`，见 F-18 |
  | POST | `/feishu/event` | 平台签名（fail-closed） | `feishu.py:79-86`，见 F-18 |

  **无鉴权路由全集 = `/healthz`、`/`、`/<path:path>`、`GET /api/site`、`POST /api/auth/login`、`POST /qq/events`、`POST /feishu/event`**（后两个有平台签名）。除此之外**没有**遗漏鉴权的路由；`public_readonly=1`（`security.py:54-63`）会把 `/api/data`、`/api/live`、`/api/violations`、`/api/payments`、`/api/daily` 变成匿名可读（默认 `False`，`registry.py:427`）。
- **XSS**：全仓库 grep `innerHTML|outerHTML|insertAdjacentHTML|document.write|v-html` → **No matches**（前端为原生 JS + Vite，`frontend/` 下无此用法）；后端没有任何 `render_template` / `render_template_string`（grep 确认），无 Jinja 模板文件参与渲染 ⇒ **未发现模板注入或 DOM XSS 引入点**。
- **开放重定向**：无任何基于请求参数的 `redirect()`；`factory.py:101` 的 `send_from_directory(static_dir, "index.html")` 里 `index.html` 是**字面量**，路径参数 `path` 只用于 `startswith(RESERVED_PREFIXES)` 判断，**不参与文件路径拼接** ⇒ 未发现开放重定向与路径穿越。
- **上传/下载接口**：**不存在**。grep `send_file|send_from_directory` 仅命中 `factory.py:101`（SPA 首页）；`MAX_CONTENT_LENGTH=4MB`（`factory.py:173`）限制了请求体。因此「日志查看/下载/备份/导出」类路径穿越面的**代码不存在**（`/api/admin/logging` 只返回日志**级别状态**，`admin_service.logging_state`，不返回文件内容）。
- **CORS**：未配置任何 `Access-Control-Allow-*`（grep 全仓库仅 `docs/DEPLOY_PAAS.md:103` 提到「不需要开」）⇒ 跨站无法读取任何 JSON 响应，这对 `GET` 型敏感接口（F-03）是重要的缓解。

**注入**
- SQL：`starwatt/db/repositories.py` 全部走 `conn.execute(sql, params)` 绑定参数；动态片段只有三类，且拼接**只来自模块级常量元组**：`_USER_COLS`（`:448`）、`RunStatusRepo._COLUMNS`（`:355-366`）、`SessionRepo._COLS`（`:566`）、`AuditRepo._COLS`（`:644/689`）；可变部分（`WHERE action = ?`、`AND ip = ?`、`ORDER BY id DESC LIMIT ?`）全部是 `?` 占位（`:689-697`、`:723-734`、`:737-745`）。**未发现 SQL 注入**。
- `starwatt/db/migrations.py:46` 的 `f"PRAGMA table_info({table})"` 与 `:55` 的 `f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"` 是 f-string，但**唯一调用点**是 `_m2_users_disabled`（`:72`）与 `_m3_users_must_change_password`（`:77-79`），传入的是字面量 `"users"` / `"disabled"` / `"must_change_password"` / DDL 字符串，**无用户输入可达** ⇒ 不是注入点（但若将来把表名参数化，需白名单校验）。
- **命令执行**：grep `os.system|subprocess|eval(|exec(|pickle|yaml.load(|__import__|shell=True` in `starwatt/` → **No matches found**。`starwatt/notify/ed25519.py` 是纯 Python 实现的 Ed25519（无外部命令、无 C 扩展）。
- 输入边界：`dashboard.py:56-67` 的 `parse_days` 上限 3650、`parse_hours` 同族（挡住 `?days=99999999` 全表扫描）；`admin.py:282-287` 的 `limit` 解析失败回落 100 并作为绑定参数传给 SQL。

**凭据与密钥**
- `FLASK_SECRET_KEY` 生成：`secrets.token_hex(32)`（`auth/secret.py:105`），256 bit 熵，正确；写回 `.env` 的失败被捕获（`:89-93`），不会崩。
- 配置中心加密：属性隔离的密钥派生 `HKDF-SHA256(master=FLASK_SECRET_KEY, salt=None, info=b"starwatt.config.secret.v1")` → 32 字节 AES 密钥（`config_registry/secrets.py:80-90`，用途隔离用 `context=_INFO`，**未与 Flask 会话签名共用同一密钥**）。
- **AES-GCM nonce 不复用**：每次 `encrypt` 调 `Crypto.Random.get_random_bytes(12)`（`secrets.py:117-120`）—— 96-bit 随机 nonce，未见固定/递增 nonce。**未发现 nonce 复用**。
- 解密失败**不静默降级**：无前缀按明文处理（迁移兼容，`:135-137`），有前缀但认证失败抛 `SecretDecryptError`（`:150-157`）—— 符合 fail-closed。
- 明文与密文可区分（`enc:v1:` 前缀，`:61`），且写入时自动升级旧明文（`store.py:217-218` 登记/注销 secret）。
- 日志脱敏：`logging_setup.py:202-248`（已登记 secret 精确替换 + 5 类形态正则）；`RedactingFilter` 挂在 **handler** 上（`:258`、`:525-526`、`:537-540`），并额外处理 `exc_text`/`stack_info`（`:277-282`）——覆盖了「抓取异常把 `?openid=…` 写进 traceback」这条后门。`register_secret` 在 `store.py:218/236` 于写入/首次读取时登记，`forget_secret` 在 `:217` 于值变更时注销。**未发现 openid / app secret / token 进入日志的路径**（`setup_service.py:150-155` 只记「有/无」）。
- 凭据不进 API 响应：`auth_service.public_user`（`services/auth_service.py:56-65`）只返回 id/username/role/must_change_password，**不含** `password_hash`；`setup_service._sanitize`（`:259-261`）把 openid 从错误消息里替换掉；抓取层 `scraper/service.py:205-214` 同样脱敏。例外是 F-03 的 `include_secrets=1`（需要管理员显式请求）与 `parse_url` 回显用户自己粘贴的 openid（`setup_service.py:143`，只回给该管理员）。
- `.env`（545 B，含真实凭据）与 `records.db`（135 KB，含真实数据）都**存在**于工作树，但：`git ls-files` 输出**只有** `.env.example`（两者均未入库），`git check-ignore -v` 确认 `.gitignore:50` 忽略 `.env`、`.gitignore:54` 忽略 `records.db`，`.dockerignore:28-35` 也排除了 `.env` / `records.db` / `records.db-*` / `backups` ⇒ **未进 git、未进镜像层**。
- `docker-entrypoint.sh:35-43` 的随机串生成只用 `/dev/urandom`（不依赖 openssl），并刻意规避了 `tr … | head -c` 的 SIGPIPE + `pipefail` 坑；`_new_password` 生成的密码满足 `validate_strength` 的四条规则。

**部署加固**
- `docker-compose.yml`：`read_only: true`（`:69`）、`tmpfs: /tmp`（`:70-71`）、`security_opt: no-new-privileges:true`（`:72-73`）、`cap_drop: ALL`（`:74-75`）、`deploy.replicas: 1`（`:87`）、端口默认绑 `127.0.0.1`（`:59`）、命名卷 `starwatt-data:/data`（`:65`）、healthcheck（`:77-83`）—— 与用户描述的加固一致。
- 只读根下的**可写路径核对**：应用只写 **(a)** SQLite（`DB_PATH=/data/records.db`，`Dockerfile:74`）、**(b)** 日志（`logging_setup.py:549-553` → `data_dir/"logs"` = `/data/logs`）、**(c)** `.env` 写回尝试（`auth/secret.py:65-93`，失败被捕获降级）、**(d)** entrypoint 的 `/data/.flask_secret_key` 与 `/data/.initial-admin-password`（`docker-entrypoint.sh:25-28`，均在卷内）。`PYTHONDONTWRITEBYTECODE=1`（`Dockerfile:70`）避免 `__pycache__` 写入 ⇒ **未发现会撞只读根的意外写路径**（(c) 已有 try/except 兜底，见 F-10 的后果讨论）。
- 非 root：`USER starwatt`（uid 10001，`Dockerfile:90-95`），`chown -R starwatt:starwatt /data /app`（`:92`）。`EXPOSE 5000`（`:98`）与 compose 端口映射一致，**没有暴露额外端口**。
- 探活：`HEALTHCHECK … curl -fsS http://127.0.0.1:${FLASK_PORT}/healthz`（`Dockerfile:101-102`）与 compose 的 healthcheck（`docker-compose.yml:79`）一致；`health.py` 确实不碰 DB、不碰网络（`:6-10` 的约束与 `:28-30` 的实现相符）。
- `gunicorn.conf.py`：`workers = 1`（`:46`）、`preload_app = False`（`:49`）、`worker_class = "sync"`、`timeout = 120`、`graceful_timeout = 30`；`post_fork` 用 **arbiter 的 PID** 判定（`:62-71`）+ `web.py:100-104` 的 master 守卫 ⇒ 调度器不会在 master 启动、也不会因 reload 重复启动。**未发现 gunicorn 配置层面的安全缺陷**。
- `docker-compose.yml:47` / `Dockerfile:69-74`：`FLASK_SECRET_KEY` 与 `BOOTSTRAP_ADMIN_PASSWORD` 通过环境变量传入而非硬编码；`.env.example`（59 行）里 `FLASK_SECRET_KEY=` 为空、其余全是非敏感项（DB 路径/数据目录/端口）⇒ **`.env.example` 未泄露任何凭据**。
- `.gitignore` / `.dockerignore` 的「红线」注释与实现一致（`.dockerignore:13-43` 排除 `.venv`、`__pycache__`、缓存目录、`tests`、`docs`、`_*`、`.audit-*`）。

**SSRF 其余部分**
- L4 逐跳复检确实实现且生效：`client.py:251-280` 手动跟随重定向（`allow_redirects=False`，`:258`），每一跳都 `assert_public_url(target, …)`（`:273-275`），`MAX_REDIRECTS = 3`（`:63`）；`303` 正确改成 GET（`:278-279`）。
- 响应体积上限：`_read_capped`（`:292-314`）流式读取，超过 `MAX_BYTES = 2 MB` 立即抛错 ⇒ 防止超大响应耗尽内存。
- 重试策略区分传输层错误（重试）与 4xx（不重试）、5xx（重试）（`:222-232`）；`SsrfError` **不重试**（`:212-213`，安全拒绝不浪费轮次）。
- `same_host`（`:106-108`）与 host 白名单（`setup_service.py:76-92`）都会先判空/占位值，逻辑正确：`dorm_base_url` 仍为出厂占位（`setup_service.py:53`）时采纳粘贴 host（首次 OOBE 必需），一旦配过就要求同源。

**学校接口侧（对应 F-01）**
- 抓取请求**确实携带 openid**：`ScrapeContext.url()`（`scraper/service.py:199-202`）拼 `?openid=<quote(...)>`；`client.py:165-181` 的 `_headers()` 还带微信 UA + `Referer: <base>/campus/webchat/dormEmRealRead/finduser` + `Origin` + `X-Requested-With`（WAF 伪装）。所以「本项目没传 openid」这个假设**不成立** —— 越权成因是**上游不校验**，不是本项目漏传。
- `run_once` 在 openid 缺失时**明确失败**（`scraper/service.py:518-521` raise `ScrapeError`），`setup_service.verify` 在 openid 缺失时返回错误（`:183-192`）⇒ **不存在「空 openid 打到 finduser」的代码路径**（用户实测的空 openid 越权无法由本项目触发，只能由外部直接调用触发）。
- 但 `verify()` 的 `room_id` 显式传入分支（`:210-212`）绕过了唯一的归属信息源（finduser）—— 这正是 F-01 的核心。

---

## 四、未能验证的部分及原因

1. **未启动过应用**（`create_app()` 会调 `db.init()` 写 `records.db`，超出「只读」授权）。因此以下结论是**静态推导**而非运行时观测：实际响应头上的 Cookie 属性、`Set-Cookie` 的 `Secure` 缺失、429 行为、CSP 缺失。F-02/F-06/F-11 的代码证据是穷举 grep（`SESSION_COOKIE_SECURE`、`ProxyFix`、`after_request`、安全头名），置信度高，但**没有运行时抓包证据**。
2. **未对学校真实站点发起任何请求**（硬约束）。F-01 的「学校接口不校验 openid」依赖**用户自述的实测结论**；我只复核了代码侧「会把这个 roomId 写进配置并长期使用」这条路径，未独立复现越权。
3. **未运行 pip-audit / 依赖 CVE 比对**（禁止联网装依赖）。F-09 只评价「未固定版本」这一事实，不评价具体版本的已知漏洞。
4. **DNS 相关行为未在 Linux/容器环境验证**：F-08 中「`2130706433`/`127.1` 是否会被 `getaddrinfo` 解析成 `127.0.0.1`」我**没有实测**（那需要 DNS 查询，且 Windows 与 musl/glibc 语义不同）。我只验证了「若解析到内网则必然被 L3 拦」以及「解析失败必然 fail-closed」这两个分支，因此那些形态**不构成**可用绕过。
5. **容器只读根下的实际写失败未在真实容器里复现**（无 docker 运行授权）。F-10 的后果（`.env` 写回失败 → 临时密钥）是读代码推导的，只在 `docker-compose.yml:69` 的 `read_only: true` 前提下成立。
6. **`frontend/` 只做了源码级 grep**（`innerHTML` 等），未在浏览器里跑前端、未检查 `frontend/node_modules`（构建依赖，`.dockerignore:39` 已排除出镜像）、未检查 `static/assets` 产物（`.dockerignore:41` 排除，镜像内 `npm run build` 生成）。因此 F-06 的 CSP 建议里 `style-src 'unsafe-inline'` 是否必要、以及是否有依赖第三方 CDN 的资源，**未确认**（若前端引用了外部 CDN，`default-src 'self'` 会直接打坏页面 —— 上线 CSP 前必须先跑一遍前端）。
7. **未做 DoS/资源耗尽评估**：`notify/card_builder.py`、`notify/renderer.py`（PNG 渲染）、`notify/reports.py` 的输入规模上限未审计；`/api/oobe/advance` 与 `/api/admin/config/import`（`MAX_CONTENT_LENGTH=4MB`）的解析开销未压测。
8. **未审计 `notify/qq.py`（636 行）与 `notify/transport.py`（355 行）的全部错误分支**：只确认了 (a) 入站验签是 fail-closed（`:351-359`）、(b) 出站日志会打印平台返回的 `body`/`payload`（`qq.py:307`、`transport.py:299/412`）但受 `RedactingFilter` 的 `token|secret|access_token` 形态正则保护。若平台错误响应**原样回显** AppSecret 而字段名不含上述关键词，理论上可能漏网 —— 未能构造/观测到实例。
9. **未验证 `records.db`（135 KB）内容**：未打开该库（避免读到真实用户数据）。已确认它未进 git、未进镜像（见前）；但**当前工作树里它确实存在**，若用户在开发机上另做备份/打包，需自行注意（`.dockerignore:53-56` 只覆盖 `_*` 与 `.audit-*`，不覆盖任意自定义备份文件名）。
10. **审计日志的完整性保护未验证**：`audit_log` 没有触发器/哈希链（`db/schema.py:173-185`），应用层也没有删除接口（正面），但**任何能写 `/data` 的进程都能改它**。属部署信任边界，未做进一步评估。
