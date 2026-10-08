<script setup lang="ts">
/**
 * 仪表盘外壳（E1 / E12 / E13 / E14）—— 四段式导航 + 顶部工具条
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
 */
import { computed } from 'vue'
import { RouterLink } from 'vue-router'

import BaseButton from '@/components/BaseButton.vue'
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
    <header class="border-b border-border bg-elevated">
      <div class="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3">
        <div class="min-w-0">
          <h1 class="truncate text-base font-bold text-content">{{ auth.siteName }}</h1>
          <p class="truncate text-xs text-muted">
            {{ payload?.latest_ts ? `最后更新 ${payload.latest_ts}` : '正在加载…' }}
            <span v-if="failures > 0" class="text-warning"> · 连续失败 {{ failures }} 次</span>
          </p>
        </div>

        <div class="flex shrink-0 items-center gap-2">
          <BaseButton v-if="canRefresh" variant="ghost" :loading="refreshing" @click="refresh">
            刷新
          </BaseButton>
          <ThemeToggle />
          <RouterLink
            v-if="auth.isAdmin"
            to="/admin"
            class="rounded-control border border-border px-2.5 py-1.5 text-sm text-content transition hover:bg-surface"
          >
            管理
          </RouterLink>
          <button
            type="button"
            class="rounded-control border border-border px-2.5 py-1.5 text-sm text-content transition hover:bg-surface"
            @click="auth.logout()"
          >
            退出
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-6xl px-4 py-4">
      <p
        v-if="refreshNotice"
        class="mb-3 rounded-control bg-success/10 px-3 py-2 text-xs text-success"
        role="status"
      >
        {{ refreshNotice }}
      </p>
      <p
        v-if="refreshError"
        class="mb-3 rounded-control bg-danger/10 px-3 py-2 text-xs text-danger"
        role="alert"
      >
        {{ refreshError }}
      </p>
      <p
        v-if="payload?.stale"
        class="mb-3 rounded-control bg-warning/10 px-3 py-2 text-xs text-warning"
      >
        ⚠ 数据已陈旧（超过配置时限未成功抓取）—— 下面显示的是最后一次成功的值。
      </p>

      <SectionTabs class="mb-4" />

      <p v-if="loading && !payload" class="py-10 text-center text-sm text-muted">正在加载…</p>

      <template v-else>
        <OverviewSection v-if="section === 'overview'" :live="payload" />
        <HistorySection v-else-if="section === 'history'" />
        <ViolationsSection v-else-if="section === 'violations'" />
        <MeterSection v-else :live="payload" />
      </template>
    </main>
  </div>
</template>
