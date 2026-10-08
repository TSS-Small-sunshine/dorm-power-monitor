<script setup lang="ts">
/**
 * 总览（M5 §5.4 的第一段，验证整条链路）
 *
 * 数据来源与字段名都来自**冻结契约**：
 *
 * * ``GET /api/live`` → ``stats{remain, hourly_used, read_time, daily_avg}``、
 *   ``monthly_projection{...}``、``run_status{cur,vol,yggl,run_status,update_dt}``、
 *   ``stale``
 * * 字段名一个都不能改（``tests/regression/fixtures/api.json`` 钉着）
 *
 * ⚠️ 数值格式化：``null`` 必须显示「—」而不是 0 或 NaN —— 后端用 null 表达
 * 「窗口不够，算不出来」（日均/近一小时），把 null 当 0 会误导用户。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { LivePayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const payload = ref<LivePayload | null>(null)
const loading = ref(true)
const error = ref('')

const stats = computed(() => payload.value?.stats ?? null)
const monthly = computed(() => payload.value?.monthly_projection ?? null)
const meter = computed(() => payload.value?.run_status ?? null)

function num(value: number | null | undefined, digits = 2, unit = ''): string {
  if (value === null || value === undefined) return '—'
  return `${value.toFixed(digits)}${unit}`
}

function text(value: string | null | undefined): string {
  return value && value.trim() !== '' ? value : '—'
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    payload.value = await api.live()
  } catch (err) {
    if (auth.handle(err)) {
      // 401 → 会话没了，交给守卫；403 强制改密 → 跳改密页
      return
    }
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="mx-auto max-w-5xl px-4 py-6">
    <header class="mb-5 flex items-center justify-between">
      <div>
        <h1 class="text-lg font-bold text-content">总览</h1>
        <p class="text-sm text-muted">
          {{ auth.siteName }}
          <span v-if="payload?.latest_ts"> · 最新数据 {{ payload.latest_ts }}</span>
        </p>
      </div>
      <BaseButton variant="ghost" :loading="loading" @click="load">刷新</BaseButton>
    </header>

    <p v-if="payload?.stale" class="mb-4 rounded-control bg-warning/10 px-3 py-2 text-xs text-warning">
      ⚠ 数据已陈旧（超过配置的时限未成功抓取）—— 下面显示的是最后一次成功的值。
    </p>

    <p v-if="error" class="mb-4 rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ error }}
    </p>

    <!-- 4 个统计卡（stats 契约） -->
    <div class="grid grid-cols-2 gap-3 md:grid-cols-4">
      <div class="sw-card p-4">
        <p class="text-xs text-muted">剩余电量</p>
        <p class="mt-1 text-2xl font-bold text-content">{{ num(stats?.remain, 2) }}</p>
        <p class="text-xs text-muted">kW·h</p>
      </div>
      <div class="sw-card p-4">
        <p class="text-xs text-muted">近一小时</p>
        <p class="mt-1 text-2xl font-bold text-content">{{ num(stats?.hourly_used, 2) }}</p>
        <p class="text-xs text-muted">kW·h</p>
      </div>
      <div class="sw-card p-4">
        <p class="text-xs text-muted">日均用量</p>
        <p class="mt-1 text-2xl font-bold text-content">{{ num(stats?.daily_avg, 2) }}</p>
        <p class="text-xs text-muted">kW·h / 天</p>
      </div>
      <div class="sw-card p-4">
        <p class="text-xs text-muted">本月预计</p>
        <p class="mt-1 text-2xl font-bold text-content">
          {{ num(monthly?.monthly_projection, 2) }}
        </p>
        <p class="text-xs text-muted">kW·h</p>
      </div>
    </div>

    <!-- 电表快照（驼峰键，与卡片契约一致） -->
    <section class="sw-card mt-4 p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">实时电表</h2>
      <dl class="grid grid-cols-2 gap-3 text-sm md:grid-cols-5">
        <div>
          <dt class="text-xs text-muted">状态</dt>
          <dd class="text-content">{{ text(meter?.run_status) }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">电压</dt>
          <dd class="text-content">{{ text(meter?.vol) }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">电流</dt>
          <dd class="text-content">{{ text(meter?.cur) }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">功率</dt>
          <dd class="text-content">{{ text(meter?.yggl) }}</dd>
        </div>
        <div>
          <dt class="text-xs text-muted">上报时间</dt>
          <dd class="text-content">{{ text(meter?.update_dt) }}</dd>
        </div>
      </dl>
    </section>

    <p class="mt-4 text-xs text-muted">
      读取时间 {{ text(stats?.read_time) }} · 电价 {{ num(payload?.eqprice, 2) }} 元/kW·h ·
      本月已观测 {{ monthly?.days_observed ?? '—' }} 天（剩 {{ monthly?.days_left ?? '—' }} 天）
    </p>
  </div>
</template>
