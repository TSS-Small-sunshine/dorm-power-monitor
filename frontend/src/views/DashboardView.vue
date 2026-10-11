<script setup lang="ts">
/**
 * 仪表盘外壳（E1 / E12 / E13 / E14 / UI 改版 v2）—— 四段式导航 + 顶部工具条
 *
 * 各段的内容在 ``views/sections/*``；本文件只管四件事：
 *
 * * **E1** 四段导航（当前段记在 ``localStorage``）
 * * **E12** 30 秒轮询 ``/api/live``（页面不可见时暂停）
 * * **E13** 手动刷新按钮（仅管理员 —— 后端对 viewer 回 403）
 * * **E14** 深浅色切换
 *
 * 数据只在**外壳**里拉一次，往下传给各段（概览与电表都要 live）——
 * 否则切段会重复请求同一份数据。
 *
 * v2 改动
 * ======
 *
 * * **顶部条吸顶 + 半透明毛玻璃**：页面滚到趋势图时，「刷新 / 管理 / 退出」
 *   不该跟着滚走（旧版滚下去就找不到刷新按钮了）。用 `sw-glass` 而不是实心
 *   白底，是为了不把页面硬生生切成两截。
 * * **加品牌标记**：左上角一个 ⚡ 圆角块 —— 站点名是用户自己填的，
 *   需要一个固定元素让人确认「这是星瓦」。
 * * **三处提示条统一走 `Notice`**：旧版各自手写 `rounded bg-brand/10 text-xs`，
 *   字号与留白都不一样，且陈旧警告没有图标、role 也不对。
 * * **首屏加载改成骨架屏**：旧版是一行「正在加载…」，布局会在数据到达时
 *   整页跳一下；骨架屏把位置先占住。
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import BaseButton from '@/components/BaseButton.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import SectionTabs from '@/components/SectionTabs.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useLive } from '@/composables/useLive'
import { useSection } from '@/composables/useSection'
import { useAuthStore } from '@/stores/auth'
import HistorySection from '@/views/sections/HistorySection.vue'
import MeterSection from '@/views/sections/MeterSection.vue'
import OverviewSection from '@/views/sections/OverviewSection.vue'
import ViolationsSection from '@/views/sections/ViolationsSection.vue'

const auth = useAuthStore()
const { section } = useSection()
const { payload, loading, refreshing, failures, refreshError, refreshNotice, refresh } = useLive()

const canRefresh = computed(() => auth.isAdmin)
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
            <h1 class="truncate text-base font-bold leading-tight text-content">
              {{ auth.siteName }}
            </h1>
            <p class="truncate text-xs text-muted">
              {{ payload?.latest_ts ? `最后更新 ${payload.latest_ts}` : '正在加载…' }}
              <span v-if="failures > 0" class="text-warning"> · 连续失败 {{ failures }} 次</span>
            </p>
          </div>
        </div>

        <div class="flex shrink-0 items-center gap-1.5">
          <BaseButton v-if="canRefresh" variant="ghost" size="sm" :loading="refreshing" @click="refresh">
            <template #icon><Icon name="refresh" :size="14" /></template>
            刷新
          </BaseButton>
          <ThemeToggle />
          <RouterLink
            v-if="auth.isAdmin"
            to="/admin"
            class="inline-flex items-center gap-1.5 rounded-control border border-border bg-elevated
                   px-2.5 py-1.5 text-sm text-content transition
                   hover:border-border-strong hover:bg-surface"
          >
            <Icon name="sliders" :size="15" />
            <span class="hidden sm:inline">管理</span>
          </RouterLink>
          <button
            type="button"
            class="inline-flex items-center gap-1.5 rounded-control border border-border bg-elevated
                   px-2.5 py-1.5 text-sm text-content transition
                   hover:border-border-strong hover:bg-surface"
            @click="auth.logout()"
          >
            <Icon name="logout" :size="15" />
            <span class="hidden sm:inline">退出</span>
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-6xl space-y-4 px-4 py-5">
      <Notice v-if="refreshNotice" tone="success">{{ refreshNotice }}</Notice>
      <Notice v-if="refreshError" tone="danger">{{ refreshError }}</Notice>
      <Notice v-if="payload?.stale" tone="warning">
        数据已陈旧（超过配置时限未成功抓取）—— 下面显示的是最后一次成功的值。
      </Notice>

      <SectionTabs />

      <!-- 首屏骨架：把布局先占住，数据到达时不会整页跳 -->
      <div v-if="loading && !payload" class="space-y-4" aria-busy="true" aria-live="polite">
        <div class="sw-card p-5">
          <div class="sw-skeleton h-3 w-20" />
          <div class="sw-skeleton mt-3 h-10 w-48" />
          <div class="sw-skeleton mt-3 h-3 w-64" />
        </div>
        <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <div v-for="n in 4" :key="n" class="sw-card p-4">
            <div class="sw-skeleton h-3 w-16" />
            <div class="sw-skeleton mt-3 h-7 w-24" />
          </div>
        </div>
        <span class="sr-only">正在加载仪表盘数据…</span>
      </div>

      <template v-else>
        <OverviewSection v-if="section === 'overview'" :live="payload" />
        <HistorySection v-else-if="section === 'history'" />
        <ViolationsSection v-else-if="section === 'violations'" />
        <MeterSection v-else :live="payload" />
      </template>
    </main>
  </div>
</template>
