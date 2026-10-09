/**
 * 前端入口（M5 §5.1）
 *
 * 顺序有讲究
 * ==========
 *
 * 1. **先定深浅色**（`initTheme`）再挂载 —— 否则首帧会是浅色再闪成深色
 * 2. 再建 Pinia（路由守卫要用 auth store）
 * 3. 最后挂载 —— 守卫里的 `bootstrap()` 会在首次导航时探身份
 */
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import router from './router'
import { initTheme } from './theme'
import './styles/main.css'

initTheme()

const app = createApp(App)
app.use(createPinia())
app.use(router)
app.mount('#app')
