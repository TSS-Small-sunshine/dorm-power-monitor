/**
 * PostCSS 配置 —— Tailwind 的入口。
 *
 * `.js`（而不是 `.cjs`）也 OK：`package.json` 里 `"type": "module"`，
 * 所以这个文件按 ESM 解析 —— Vite 两者都支持。
 */
export default {
  plugins: {
    tailwindcss: {},
    autoprefixer: {},
  },
}
