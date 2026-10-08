<script setup lang="ts">
/**
 * 概览段（E2 / E3 / E4）
 *
 * 单位容易错，逐个说清
 * ====================
 *
 * * ``monthly_projection`` 是**金额（元）**，不是度数 —— 公式是
 *   ``(used_kwh + avg_daily × days_left) × eqprice``（见 fixtures/algorithms.json
 *   的 ``normal`` 向量：2.0 × 24 × 0.5 = 24.0）。第一版把它标成 kW·h 是错的。
 * * ``stats.hourly_used`` / ``daily_avg`` 可能为 ``null``（窗口不够）→ 显示「—」
 *
 * E3 的 4 档范围（24h / 3d / 7d / 30d）各自拉一次 ``/api/data?hours=``：
 * 后端窗口是查询参数，不需要前端做聚合。
 */
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { DataPayload, LivePayload } from '@/api/types'
import ChartLine from '@/components/ChartLine.vue'
import StatCard from '@/components/StatCard.vue'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{ live: LivePayload | null }>()

const auth = useAuthStore()

/** E3 的 4 档范围（legacy 的 24h / 3d / 7d / 30d） */
const RANGES = [
  { hours: 24, label: '24 小时' },
  { hours: 72, label: '3 天' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
]

const hours = ref(24)
const history = ref<DataPayload | null>(null)
const chartError = ref('')

const stats = computed(() => props.live?.stats ?? null)
const monthly = computed(() => props.live?.monthly_projection ?? null)

/** 连接状态点：陈旧 / 离线 / 正常（E2） */
const connection = computed(() => {
  if (!props.live) return { tone: 'default' as const, text: '等待数据' }
  if (props.live.stale) return { tone: 'warning' as const, text: '数据陈旧' }
  const status = props.live.run_status?.run_status
  if (status && status !== '在线' && status !== '正常' && status !== '通讯正常') {
    return { tone: 'danger' as const, text: `电表${status}` }
  }
  return { tone: 'default' as const, text: '正常' }
})

function num(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function text(value: string | null | undefined): string {
  return value && value.trim() !== '' ? value : '—'
}

/** 横轴：``MM-DD HH:mm``（24h 视图）或 ``MM-DD``（长窗口） */
function label(ts: string): string {
  return hours.value <= 24 ? ts.slice(5, 16) : ts.slice(5, 10)
}

async function loadHistory(): Promise<void> {
  chartError.value = ''
  try {
    history.value = await api.data(hours.value)
  } catch (err) {
    if (auth.handle(err)) return
    chartError.value = err instanceof Error ? err.message : '趋势数据加载失败'
  }
}

watch(hours, loadHistory, { immediate: true })
</script>

<template>
  <div class="space-y-4">
    <!-- E2 hero -->
    <section class="sw-card p-5">
      <div class="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p class="text-xs text-muted">剩余电量</p>
          <p class="mt-1 text-4xl font-bold text-content">
            {{ num(stats?.remain) }}
            <span class="text-sm font-normal text-muted">kW·h</span>
          </p>
          <p class="mt-1 text-xs text-muted">
            抄表时间 {{ text(stats?.read_time) }} · 最后更新 {{ text(live?.latest_ts) }}
          </p>
        </div>
        <p
          class="flex items-center gap-1.5 text-sm"
          :class="{
            'text-muted': connection.tone === 'default',
            'text-warning': connection.tone === 'warning',
            'text-danger': connection.tone === 'danger',
          }"
        >
          <span
            class="h-2 w-2 rounded-full"
            :class="{
              'bg-success': connection.tone === 'default',
              'bg-warning': connection.tone === 'warning',
              'bg-danger': connection.tone === 'danger',
            }"
            aria-hidden="true"
          />
          {{ connection.text }}
        </p>
      </div>
    </section>

    <!-- E4 4 张统计卡 -->
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatCard
        label="本月预计电费"
        :value="num(monthly?.monthly_projection)"
        unit="元"
        :hint="`已观测 ${monthly?.days_observed ?? '—'} 天 · 剩 ${monthly?.days_left ?? '—'} 天`"
      />
      <StatCard
        label="近一小时"
        :value="num(stats?.hourly_used)"
        unit="kW·h"
        hint="间隔 ≥ 3500s 才算"
      />
      <StatCard
        label="日均用量"
        :value="num(stats?.daily_avg)"
        unit="kW·h / 天"
        hint="窗口内有效差值"
      />
      <StatCard label="抄表时间" :value="text(stats?.read_time)" hint="电表上报时刻" />
    </div>

    <!-- E3 剩余电量趋势 -->
    <section class="sw-card p-4">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h2 class="text-sm font-semibold text-content">剩余电量趋势</h2>
        <div class="flex gap-1" role="group" aria-label="时间范围">
          <button
            v-for="range in RANGES"
            :key="range.hours"
            type="button"
            class="rounded-control px-2.5 py-1 text-xs transition"
            :class="
              hours === range.hours
                ? 'bg-brand/10 text-brand'
                : 'text-muted hover:bg-surface hover:text-content'
            "
            :aria-pressed="hours === range.hours"
            @click="hours = range.hours"
          >
            {{ range.label }}
          </button>
        </div>
      </div>

      <p v-if="chartError" class="sw-error" role="alert">{{ chartError }}</p>
      <ChartLine
        :labels="(history?.rows ?? []).map((row) => label(row.ts))"
        :values="(history?.rows ?? []).map((row) => row.remain)"
        unit=" kW·h"
        empty-text="这个时间范围内还没有抓取记录"
      />
    </section>
  </div>
</template>
