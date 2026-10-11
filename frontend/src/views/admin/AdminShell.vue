<script setup lang="ts">
/**
 * 管理后台外壳（5.5 / UI 改版 v2）—— 顶部条 + 后台导航 + 路由出口
 *
 * 6 个页面（计划 §5.5）：配置中心 / 功能开关 / 日志 / 用户 / 连通测试 / 审计。
 * 本文件只负责「导航 + 出口」，页面内容各自独立 —— 加一个管理页只需
 * 在 ``router`` 里加一条路由 + 在下面的 ``TABS`` 里加一行。
 *
 * 访问控制：整个 ``/admin`` 分支由路由守卫拦「非管理员」，后端另有
 * ``admin_access``（403）兜底 —— 前端隐藏入口只是体验，不是安全边界。
 *
 * v2 改了什么
 * ==========
 *
 * * **顶部条吸顶 + 毛玻璃**（``sw-glass``）：旧版顶栏会跟着页面滚走，
 *   在配置中心往下翻 90 个字段后就找不到「返回仪表盘 / 退出」了。
 * * **品牌标记**：左上角一个 ``zap`` 图标块，与仪表盘一致 ——
 *   站点名是用户自己填的，需要一个固定元素让人确认「这是星瓦」。
 * * **导航从「下划线标签」改成分段控件**：旧版 6 个带下划线的文字链接
 *   与页面里的普通链接视觉上分不开，看不出「这是一组互斥的视图切换」。
 *   选中态同时有底色、阴影与 ``aria-current``，不靠颜色单独承担。
 * * **窄屏不再横向滚动藏掉分区**：``sm`` 以上等分铺满，以下自动换行成两排，
 *   6 个分区始终都看得见。
 * * **图标全部换成 ``Icon``**：导航项各配一个 SVG，扫一眼就能定位
 *   （旧版纯文字，6 项长得一样）。
 */
import { RouterLink, RouterView, useRouter } from 'vue-router'

import Icon from '@/components/Icon.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const TABS = [
  { name: 'admin-config', label: '配置中心', icon: 'sliders' },
  { name: 'admin-switches', label: '功能开关', icon: 'power' },
  { name: 'admin-logging', label: '日志', icon: 'file' },
  { name: 'admin-users', label: '用户', icon: 'users' },
  { name: 'admin-test', label: '连通测试', icon: 'send' },
  { name: 'admin-audit', label: '审计日志', icon: 'shield' },
]

/** 顶栏次级按钮（与仪表盘上的「管理 / 退出」同一套样式） */
const TOP_ACTION =
  'inline-flex items-center gap-1.5 rounded-control border border-border bg-elevated px-2.5 py-1.5 ' +
  'text-sm text-content transition hover:border-border-strong hover:bg-surface'

async function logout(): Promise<void> {
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <div class="min-h-full bg-surface">
    <header class="sw-glass sticky top-0 z-20 border-b border-border">
      <div class="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
        <div class="flex min-w-0 items-center gap-3">
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-control
                   bg-brand-soft text-brand"
            aria-hidden="true"
          >
            <Icon name="zap" :size="18" />
          </span>
          <div class="min-w-0">
            <h1 class="flex items-center gap-2 truncate text-base font-bold leading-tight text-content">
              管理后台
              <span v-if="auth.isAdmin" class="sw-chip bg-brand-soft text-brand">管理员</span>
            </h1>
            <p class="truncate text-xs text-muted">
              {{ auth.siteName }} · 以 {{ auth.user?.username }} 登录
            </p>
          </div>
        </div>

        <div class="flex shrink-0 items-center gap-1.5">
          <RouterLink to="/" :class="TOP_ACTION" aria-label="返回仪表盘">
            <Icon name="chart" :size="15" />
            <span class="hidden sm:inline">返回仪表盘</span>
          </RouterLink>
          <ThemeToggle />
          <button type="button" :class="TOP_ACTION" aria-label="退出" @click="logout">
            <Icon name="logout" :size="15" />
            <span class="hidden sm:inline">退出</span>
          </button>
        </div>
      </div>

      <nav class="mx-auto max-w-6xl px-4 pb-2" aria-label="管理功能">
        <ul class="flex flex-wrap gap-1 rounded-control border border-border bg-sunken p-1">
          <li v-for="tab in TABS" :key="tab.name" class="min-w-0 flex-1 sm:flex-none">
            <RouterLink
              :to="{ name: tab.name }"
              class="flex items-center justify-center gap-1.5 rounded-[8px] px-2.5 py-2
                     text-sm font-medium transition duration-150"
              :class="
                $route.name === tab.name
                  ? 'bg-elevated text-brand shadow-card'
                  : 'text-muted hover:text-content'
              "
              :aria-current="$route.name === tab.name ? 'page' : undefined"
            >
              <Icon :name="tab.icon" :size="15" />
              <span class="truncate">{{ tab.label }}</span>
            </RouterLink>
          </li>
        </ul>
      </nav>
    </header>

    <main class="mx-auto max-w-6xl px-4 py-5">
      <RouterView />
    </main>
  </div>
</template>
