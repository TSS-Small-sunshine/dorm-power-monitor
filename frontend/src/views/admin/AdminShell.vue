<script setup lang="ts">
/**
 * 管理后台外壳（5.5）—— 顶部导航 + 路由出口
 *
 * 5 个页面（计划 §5.5）：配置中心 / 用户 / 测试 / 审计（+ 概览入口）。
 * 本文件只负责「导航 + 出口」，页面内容各自独立 —— 加一个管理页只需
 * 在 ``router`` 里加一条路由 + 在下面的 ``TABS`` 里加一行。
 *
 * 访问控制：整个 ``/admin`` 分支由路由守卫拦「非管理员」，后端另有
 * ``admin_access``（403）兜底 —— 前端隐藏入口只是体验，不是安全边界。
 */
import { RouterLink, RouterView, useRouter } from 'vue-router'

import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const TABS = [
  { name: 'admin-config', label: '配置中心' },
  { name: 'admin-switches', label: '功能开关' },
  { name: 'admin-logging', label: '日志' },
  { name: 'admin-users', label: '用户' },
  { name: 'admin-test', label: '连通测试' },
  { name: 'admin-audit', label: '审计日志' },
]

async function logout(): Promise<void> {
  await auth.logout()
  await router.replace('/login')
}
</script>

<template>
  <div class="min-h-full bg-surface">
    <header class="border-b border-border bg-elevated">
      <div class="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
        <div class="min-w-0">
          <h1 class="truncate text-base font-bold text-content">管理后台</h1>
          <p class="truncate text-xs text-muted">
            {{ auth.siteName }} · 以 {{ auth.user?.username }} 登录
          </p>
        </div>
        <div class="flex shrink-0 items-center gap-2">
          <RouterLink
            to="/"
            class="rounded-control border border-border px-2.5 py-1.5 text-sm text-content transition hover:bg-surface"
          >
            返回仪表盘
          </RouterLink>
          <ThemeToggle />
          <button
            type="button"
            class="rounded-control border border-border px-2.5 py-1.5 text-sm text-content transition hover:bg-surface"
            @click="logout"
          >
            退出
          </button>
        </div>
      </div>

      <nav class="mx-auto flex max-w-6xl gap-1 overflow-x-auto px-4" aria-label="管理功能">
        <RouterLink
          v-for="tab in TABS"
          :key="tab.name"
          :to="{ name: tab.name }"
          class="shrink-0 border-b-2 px-3 py-2.5 text-sm font-medium transition"
          :class="
            $route.name === tab.name
              ? 'border-brand text-brand'
              : 'border-transparent text-muted hover:text-content'
          "
        >
          {{ tab.label }}
        </RouterLink>
      </nav>
    </header>

    <main class="mx-auto max-w-6xl px-4 py-4">
      <RouterView />
    </main>
  </div>
</template>
