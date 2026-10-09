# StarWatt 星瓦 · 前端（M5）

Vue 3 + Vite + TypeScript + Tailwind。构建产物落到仓库根的 `static/`，
由 Flask（开发）或 nginx（生产）直接送出 —— **不引入第二个运行时**。

## 快速开始

```bash
# 1. 装依赖（Node ≥ 20）
cd frontend && npm install

# 2. 起后端（另开一个终端；需要已配好 .env）
python web.py            # 或 gunicorn -c gunicorn.conf.py web:app

# 3. 起前端开发服务器（5173，/api 已代理到 5000）
npm run dev

# 4. 出产物（会先做类型检查）
npm run build
```

## 目录约定

```
src/
├── api/         client.ts（唯一 fetch 出口）+ types.ts（对齐冻结契约）
├── components/  AuthLayout / BaseButton / BaseField / ThemeToggle
├── router/      路由 + 两条守卫（未登录 → /login；未改密 → /password）
├── stores/      Pinia（auth：会话 + CSRF + 品牌）
├── styles/      tokens.css（设计令牌）+ main.css（Tailwind 层）
├── theme.ts     品牌色注入 + 深浅色切换
└── views/       页面
```

## 四条硬约束（改代码前先读）

1. **产物路径固定** `../static/`，且 `base: '/static/'` —— 后端
   `web/factory.py` 的 `static_url_path` 是 `/static`，写别的会 404。
2. **无公网 CDN**：Chart.js 走 npm 依赖并打进产物；字体用仓库里的
   `static/fonts/`（MiSans / NotoEmoji）。
3. **颜色只写在 `tokens.css`**，Tailwind 配置里只做映射。这样
   「配置中心 → 站点 → 主题色」改完**不用重新构建**（见 `theme.ts`）。
4. **接口字段名不能改**：它们来自冻结契约
   `tests/regression/fixtures/api.json`（M0 在旧代码上真实运行产出）。

## 与后端的两条链路约定

| 约定 | 说明 |
|---|---|
| 会话 | Cookie `dorm_session`（HttpOnly）→ 每个请求带 `credentials: 'same-origin'`，前端**不存** token |
| CSRF | 写操作带 `X-CSRF-Token`，值取自 `GET /api/auth/me` 的 `csrf_token`；登录/改密后要重取 |

强制首登改密（B4/Q19）：后端对 `must_change_password=1` 的会话会 403 掉
除 `/api/auth/{password,logout,me}` 外的所有接口 —— 前端守卫据此把用户
先送到 `/password`，**顺序不能反**（否则用户看到满屏「请求失败」）。
