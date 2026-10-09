import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * StarWatt 星瓦 · 前端构建配置（M5 §5.1 / §5.9）
 *
 * 三条硬约束
 * ==========
 *
 * 1. **产物落到 `../static/`** —— 后端 `web/factory.py` 只在 `static/` 存在时
 *    才把它挂给 Flask；开发时不用起第二个服务。
 * 2. **`base: '/static/'`** —— 产物里所有资源引用都带 `/static/` 前缀，
 *    与 Flask 的 `static_url_path` 对齐（写 `/assets/...` 会 404）。
 * 3. **无公网 CDN** —— Chart.js 走 npm 依赖并打进产物；字体走 `/static/fonts/`
 *    （仓库里已有 MiSans / NotoEmoji，不再从 Google Fonts 拉）。
 *
 * 开发代理
 * ========
 *
 * `npm run dev`（5173）把 `/api` 与 `/healthz` 代理到 gunicorn（5000），
 * 于是开发期也能用真实会话 Cookie —— 前端不需要 mock 一套假数据。
 */
export default defineConfig({
  plugins: [vue()],
  base: '/static/',
  build: {
    outDir: '../static',
    emptyOutDir: false, // ⚠️ static/fonts 是仓库里的资产，不能被清掉
    assetsDir: 'assets',
    sourcemap: false,
    manifest: true, // 后端（或排障）可以据此定位带 hash 的文件名
  },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:5000', changeOrigin: false },
      '/healthz': { target: 'http://127.0.0.1:5000', changeOrigin: false },
    },
  },
})
