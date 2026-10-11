<script setup lang="ts">
/**
 * 概览段（E2 / E3 / E4 / UI 改版 v2）
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
 *
 * v2 改了什么（都是**展示层**，没有碰任何口径）
 * ==========================================
 *
 * 1. **去掉重复的「抄表时间」卡**：它本来就在 hero 里，占掉 4 张卡的一格。
 *    换成**本月已用**（`monthly.used_kwh`，本来就随 `/api/live` 回来了）
 *    与单价 —— 用户最常问的两个数，之前要去「历史」段翻。
 * 2. **提示语从实现细节改成人话**：旧版写「间隔 ≥ 3500s 才算」
 *    「窗口内有效差值」，那是代码里的常量与变量名，用户看不懂。
 * 3. **状态从「一个小圆点 + 小字」变成胶囊**：电量状态是这个页面最重要的一条
 *    信息，值得有底色（且颜色不是唯一线索 —— 还有图标与文字）。
 * 4. **图表标题区补了记录条数**：图上只有 3 个点时，用户会怀疑是不是坏了；
 *    写明「共 N 条记录」就不会。
 *
 * ⚠️ 刻意**不在前端算**任何用量口径（区间差值、可用天数等）：那属于
 * `starwatt/domain/`，前端再算一遍就是 CONTRIBUTING 里说的「孪生兄弟」——
 * 后端防了充值、前端没防，两边迟早对不上。
 */
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { DataPayload, LivePayload } from '@/api/types'
import ChartLine from '@/components/ChartLine.vue'
import Icon from '@/components/Icon.vue'
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

/** 电表认为「在线」的三种取值（与后端 `thresholds.ONLINE_LABELS` 同义） */
const ONLINE = ['在线', '正常', '通讯正常']

/** 连接状态（E2）：陈旧 / 离线 / 正常 —— 颜色 + 图标 + 文字三重表达 */
const connection = computed(() => {
  if (!props.live) return { tone: 'muted', text: '等待数据', icon: 'clock' }
  if (props.live.stale) return { tone: 'warning', text: '数据陈旧', icon: 'warning' }
  const status = props.live.run_status?.run_status
  if (status && !ONLINE.includes(status)) {
    return { tone: 'danger', text: `电表${status}`, icon: 'alert' }
  }
  return { tone: 'success', text: '正常', icon: 'check' }
})

const CHIP: Record<string, string> = {
  muted: 'bg-sunken text-muted',
  success: 'bg-success-soft text-success',
  warning: 'bg-warning-soft text-warning',
  danger: 'bg-danger-soft text-danger',
}

/** 图上到底有多少个点（数据稀疏时给用户一个解释） */
const pointCount = computed(() => (history.value?.rows ?? []).filter((r) => r.remain !== null).length)

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
        <div class="min-w-0">
          <div class="flex items-center gap-2">
            <p class="text-xs font-medium text-muted">剩余电量</p>
            <span class="sw-chip" :class="CHIP[connection.tone]">
              <Icon :name="connection.icon" :size="12" />
              {{ connection.text }}
            </span>
          </div>

          <p class="mt-2 flex items-baseline gap-1.5">
            <span class="text-4xl font-bold leading-none tabular text-content sm:text-5xl">
              {{ num(stats?.remain) }}
            </span>
            <span class="text-sm font-normal text-muted">kW·h</span>
          </p>

          <div class="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5 text-xs text-muted">
            <span class="inline-flex items-center gap-1.5">
              <Icon name="clock" :size="13" />
              抄表时间 {{ text(stats?.read_time) }}
            </span>
            <span class="inline-flex items-center gap-1.5">
              <Icon name="refresh" :size="13" />
              最后更新 {{ text(live?.latest_ts) }}
            </span>
          </div>
        </div>
      </div>
    </section>

    <!-- E4 4 张统计卡 -->
    <div class="grid grid-cols-2 gap-3 lg:grid-cols-4">
      <StatCard
        label="本月预计电费"
        :value="num(monthly?.monthly_projection)"
        unit="元"
        icon="wallet"
        :loading="!live"
        :hint="`已观测 ${monthly?.days_observed ?? '—'} 天 · 剩 ${monthly?.days_left ?? '—'} 天`"
      />
      <StatCard
        label="本月已用"
        :value="num(monthly?.used_kwh)"
        unit="kW·h"
        icon="activity"
        :loading="!live"
        :hint="`单价 ${num(live?.eqprice)} 元 / kW·h`"
      />
      <StatCard
        label="近一小时"
        :value="num(stats?.hourly_used)"
        unit="kW·h"
        icon="clock"
        :loading="!live"
        hint="需要相隔一小时以上的两次抄表"
      />
      <StatCard
        label="日均用量"
        :value="num(stats?.daily_avg)"
        unit="kW·h / 天"
        icon="trending"
        :loading="!live"
        hint="按当前窗口内的抄表估算"
      />
    </div>

    <!-- E3 剩余电量趋势 -->
    <section class="sw-card p-4 sm:p-5">
      <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-content">剩余电量趋势</h2>
          <p class="mt-0.5 text-xs text-muted">
            <template v-if="pointCount">共 {{ pointCount }} 条抄表记录</template>
            <template v-else>按已抓取的抄表记录绘制</template>
          </p>
        </div>
        <div
          class="flex gap-1 rounded-control border border-border bg-sunken p-1"
          role="group"
          aria-label="时间范围"
        >
          <button
            v-for="range in RANGES"
            :key="range.hours"
            type="button"
            class="rounded-[8px] px-2.5 py-1 text-xs font-medium transition"
            :class="
              hours === range.hours
                ? 'bg-elevated text-brand shadow-card'
                : 'text-muted hover:text-content'
            "
            :aria-pressed="hours === range.hours"
            @click="hours = range.hours"
          >
            {{ range.label }}
          </button>
        </div>
      </div>

      <p v-if="chartError" class="sw-error mb-2" role="alert">{{ chartError }}</p>
      <ChartLine
        :labels="(history?.rows ?? []).map((row) => label(row.ts))"
        :values="(history?.rows ?? []).map((row) => row.remain)"
        unit=" kW·h"
        empty-text="这个时间范围内还没有抓取记录，等下一次抓取后就会出现。"
      />
    </section>
  </div>
</template>
