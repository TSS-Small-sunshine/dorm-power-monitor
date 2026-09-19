# R68 — .env 迁移指南

> 从 **R51c** 升级到 **R60-R67** bundle 时,`.env` 文件需要补充若干新变量。
> 本指南列出所有新增项、默认值、强制项,以及 R51c 已废弃项。

---

## 1. 新增变量(R63 + R65)

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `FLASK_SECRET_KEY` | ✅ **必填** | _(无)_ | Flask session cookie 签名密钥。**32 字节随机 hex**(64 字符)。R63 用它签 `itsdangerous` session token;没有它,登录态无法持久化,OOBE 也会卡在第 1 步。 |
| `AUTH_SESSION_HOURS` | ❌ | `24` | Session 有效期。过期后用户需重新登录。 |
| `AUTH_LOCKOUT_MINUTES` | ❌ | `15` | 用户被锁定的持续时间。15 分钟内 5 次失败即锁定。 |
| `AUTH_LOCKOUT_THRESHOLD` | ❌ | `5` | 触发锁定的失败次数。 |
| `AUTH_INITIAL_ADMIN_USERNAME` | ❌ | `admin` | OOBE 兜底用户名。当 DB `users` 表为空 + OOBE 未完成时,此 env 给 R65 提供默认显示值。 |
| `AUTH_INITIAL_ADMIN_PASSWORD` | ⚠️ **deploy 后用一次就删** | _(无)_ | OOBE bootstrap 密码。仅当 `users` 表为空时生效;一旦用户通过 OOBE 创建了第一个管理员,此 env 就被忽略。**生产环境永远不要保留明文密码在 .env**。 |

---

## 2. 如何生成 `FLASK_SECRET_KEY`

服务端跑一次:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

输出形如 `a3f9b2...`(64 个十六进制字符),粘到 `.env`:

```bash
FLASK_SECRET_KEY=a3f9b2c4d5e6f7...  # 你的 64 字符 hex
chmod 600 .env
```

**不要**用:

- 任何固定字符串(如 `"my-secret"`)— 可被爆破。
- `os.urandom(32)` 直接存 — 不可读,运维难调试。
- git 历史里的任何示例 — 会被扫到。

**不要 commit 到 git**:`.env` 在 `.gitignore` 里,但 deploy 模板(`.env.example`)里只放**占位字符串**(`change-me` / `<random-32-bytes-hex>`),永远不放真值。

---

## 3. R51c 已废弃项

| 旧 env | 状态 | 替换 |
|--------|------|------|
| `ADMIN_PASSWORD` | ❌ R63 删除 | 不再使用。R63 把密码移到 `users.password_hash`(bcrypt 散列),`ADMIN_PASSWORD` 不再被 `web.py` / `auth.py` 读。**保留在 .env 不会出错**(被忽略),但建议删除以减少混淆。 |
| `meta.admin_password` (DB row) | ❌ R63 删除 | schema 仍然有 `meta` 表,但 `admin_password` 这个 key 在 R63 之后不再被任何代码读。可以保留,会逐渐过期。 |
| `FLASK_SECRET_KEY` 的旧 fallback | ❌ R63 删除 | R51c 之前如果用了固定 `"dev-secret-key"` 之类,R63 启动时会拒绝并报 `RuntimeError: FLASK_SECRET_KEY required`。必须显式设置真随机 hex。 |

---

## 4. R65 OOBE 流程(为什么需要 `AUTH_INITIAL_ADMIN_PASSWORD`)

R65 强制首次启动走 6 步 OOBE 向导,所以正常路径是:

1. **第一次 deploy** → 用户访问 `/` → 被重定向到 `/oobe` → 浏览器里填用户名 + 密码。
2. 用户填好后,密码 bcrypt 散列写入 `users` 表 → OOBE 完成。
3. 此后 `AUTH_INITIAL_ADMIN_PASSWORD` **不再被读**,可以安全删除。

但有 3 个边缘情况需要这个 fallback env:

- **运维恢复**:DB 损坏,`users` 表清空,需要重启 OOBE。如果 `.env` 里有 `AUTH_INITIAL_ADMIN_PASSWORD`,OOBE 跳过第 2 步(用户名)、第 3 步(密码),直接用 env 里的值创建 admin。
- **CI / 自动化测试**:跑 `pytest` 时不想走浏览器 OOBE,直接 env bootstrap。
- **批量部署**:同一份 .env 模板 deploy 到 N 台服务器,每台用相同管理员凭据。

如果你的部署流程**不**需要这些,`AUTH_INITIAL_ADMIN_PASSWORD=` 留空就行。

---

## 5. 推荐的完整 .env(R68 之后)

```bash
# ---- Feishu 群机器人 ----
FEISHU_WEBHOOK=https://open.feishu.cn/open-apis/bot/v2/hook/<your-id>
FEISHU_SECRET=<your-secret>

# ---- Feishu 私聊机器人应用 ----
FEISHU_APP_ID=cli_xxxxxxxxxxxxxxxx
FEISHU_APP_SECRET=your_app_secret_here
FEISHU_VERIFICATION_TOKEN=
FEISHU_ENCRYPT_KEY=

# ---- 宿舍电费系统 ----
DORM_OPENID=<your-openid>
DORM_BASE_URL=http://ybhqcz.fjny.edu.cn
DORM_ROOM_ID=

# ---- Storage ----
DB_PATH=/var/lib/dorm-power-monitor/records.db
SCRAPE_INTERVAL_MINUTES=60

# ---- Flask 仪表盘 ----
FLASK_HOST=127.0.0.1
FLASK_PORT=5000
FLASK_DEBUG=0

# ---- R63 新增:Flask session 签名密钥(必填,64 字符 hex) ----
FLASK_SECRET_KEY=<paste secrets.token_hex(32) output here>

# ---- R63 新增:auth 行为调优(可不填,走默认) ----
AUTH_SESSION_HOURS=24
AUTH_LOCKOUT_MINUTES=15
AUTH_LOCKOUT_THRESHOLD=5
AUTH_INITIAL_ADMIN_USERNAME=admin

# ---- R63 新增:OOBE bootstrap(用一次就删) ----
# ⚠️ 部署完成后,首次走 OOBE 后立即删掉这一行
AUTH_INITIAL_ADMIN_PASSWORD=
```

---

## 6. 验证清单

deploy 完之后,在服务端跑:

```bash
# 1. 确认 FLASK_SECRET_KEY 设置了
grep -E '^FLASK_SECRET_KEY=[a-f0-9]{64}$' /opt/dorm-power-monitor/.env
#    -- 应该有一行 64 字符的 hex,不是空、不是 placeholder

# 2. 确认服务起来了
systemctl status dorm-power-monitor-web
#    -- active (running)

# 3. 确认新 endpoints 暴露了
curl -fsSL http://127.0.0.1:5000/api/auth/csrf | jq
#    -- {"csrf_token": "..."} 或类似 JSON

# 4. 走 OOBE
curl -fsSL http://127.0.0.1:5000/oobe | grep -E 'oobe-step|step-1'
#    -- HTML 里出现 OOBE 第 1 步

# 5. (可选) 首次 OOBE 完成后,从 .env 删掉 AUTH_INITIAL_ADMIN_PASSWORD
sudo sed -i '/^AUTH_INITIAL_ADMIN_PASSWORD=/d' /opt/dorm-power-monitor/.env
```

---

## 7. 常见问题

**Q1: deploy 之后访问 `/` 直接跳到 `/login` 而不是 `/oobe`?**

A: 说明 DB 里已经有用户(从 R51c 继承的 `meta.admin_password` **不算**用户),或者 OOBE 已被某个早期的测试触发并完成。检查:

```bash
sqlite3 /var/lib/dorm-power-monitor/records.db \
    "SELECT key, value FROM meta WHERE key IN ('oobe_step','oobe_completed');"
```

如果想强制重跑 OOBE:

```bash
sqlite3 /var/lib/dorm-power-monitor/records.db \
    "DELETE FROM meta WHERE key IN ('oobe_step','oobe_completed');"
```

然后访问 `/oobe`。

**Q2: deploy 之后 OOBE 第 3 步(密码)报 "weak password"?**

A: R63 的强度校验要求 ≥ 8 字符 + 至少 1 数字 + 至少 1 字母。如果用户密码简单,OOBE 会拒绝。这是**预期行为**,不是 bug。

**Q3: 锁定了 15 分钟,想立即解锁?**

A:

```bash
sqlite3 /var/lib/dorm-power-monitor/records.db \
    "DELETE FROM login_attempts WHERE success=0 AND ts > datetime('now','-1 day');"
```

下次登录成功会清空失败计数,失败也会因为时间过期自然解锁。

**Q4: deploy 后 `flask --app web.py routes` 看到的路由比 R51c 多?**

A: 是的。R63-R67 加了 `/api/auth/login`、`/api/auth/logout`、`/api/auth/csrf`、`/api/admin/users`、`/api/admin/config`、`/api/admin/test`、`/api/admin/audit`、`/oobe` 及其 6 个步骤子路由、`/login`、`/logout`。总路由数从 R51c 的 ~20 涨到 ~45。

---

## 8. 一句话总结

- **`FLASK_SECRET_KEY`** 是唯一**强制**新增项。
- **`AUTH_INITIAL_ADMIN_PASSWORD`** 是给运维留的兜底,正常 OOBE 流程不需要。
- **`ADMIN_PASSWORD`** 是 R51c 时代的化石,从 .env 删掉以减少混淆。
- 其余 4 个 `AUTH_*` 变量都有合理默认,可以不填。

deploy 完之后,运维只需:

```bash
# 一次性
echo "FLASK_SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
    >> /opt/dorm-power-monitor/.env
chmod 600 /opt/dorm-power-monitor/.env
chown www-data:www-data /opt/dorm-power-monitor/.env

# 走 OOBE
# 浏览器打开 http://school.tssplus.top/  → 6 步 → 完成

# 收尾
sudo -u www-data sed -i '/^AUTH_INITIAL_ADMIN_PASSWORD=/d' /opt/dorm-power-monitor/.env
```