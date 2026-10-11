<script setup lang="ts">
/**
 * 历史段（E6 / E7 / E8 / UI 改版 v2）
 *
 * * E6 每日用电曲线：``/api/daily``（后端已把累计表码推导成每日用量）
 * * E7 采集记录表：``/api/data``（倒序展示，最新在上）
 * * E8 区间筛选：``datetime-local`` + 查询/重置，走 ``/api/data?start=&end=``
 *
 * 为什么表格要倒序：用户关心的是「刚刚发生了什么」。后端按升序返回
 * （画图需要），所以**在前端翻转**，而不是让后端为两种用途各排一次。
 *
 * v2 改了什么（全部是**展示层**，一个口径都没碰）
 * ============================================
 *
 * 1. **快速区间与概览页用同一个分段控件**：旧版只有两个 ``datetime-local``
 *    输入框，想看「最近 7 天」得手打两次时间。现在 24 小时 / 3 天 / 7 天 /
 *    30 天一点即到 —— 仍然只调 ``/api/data?hours=``，参数与取值都没变；
 *    自定义区间收进「自定义区间」按钮后面，E8 的能力一条不少。
 * 2. **每日用电与采集记录彻底分成两张卡**：旧版第二张卡里既塞筛选表单又塞
 *    表格，「图」和「表」是两件事却挤在一个标题下。现在各自有标题、副标题
 *    与加载/错误状态。
 * 3. **表格能滚、好读**：容器限高（26rem）+ 表头吸顶，几百行也不会把整页
 *    拉长；时间列不换行、数字走等宽字形（``tabular``）、隔行浅底，
 *    纵向扫一列数字不会串行。
 * 4. **空状态说清「为什么空、等多久会有」**：旧版只有一句「没有记录」，
 *    用户分不清是坏了还是本来就没有。
 * 5. **两个请求各自报错**：旧版共用一个 ``error``，每日用电拉失败会把
 *    采集记录区一起染红，指向了错误的排查方向。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { DailyPayload, DataPayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import ChartLine from '@/components/ChartLine.vue'
import EmptyState from '@/components/EmptyState.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/** 快速区间（与概览页同一组值、同一个控件样式） */
const RANGES = [
  { hours: 24, label: '24 小时' },
  { hours: 72, label: '3 天' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
]

const daily = ref<DailyPayload | null>(null)
const records = ref<DataPayload | null>(null)

const dailyLoading = ref(true)
const recordsLoading = ref(true)
const dailyError = ref('')
const recordsError = ref('')

/** 记录表当前的快速区间 */
const hours = ref(24)
/** 记录表是否来自 E8 的自定义区间（此时分段控件不高亮任何一档） */
const customActive = ref(false)
const customOpen = ref(false)

// E8 的筛选输入（``datetime-local`` 的值形如 ``2026-10-06T12:00``）
const start = ref('')
const end = ref('')
const filtering = ref(false)

/** 表格最多渲染多少行（一次几百行会让低端手机卡顿） */
const TABLE_LIMIT = 100

const rows = computed(() => {
  const all = records.value?.rows ?? []
  return [...all].reverse().slice(0, TABLE_LIMIT)
})

const total = computed(() => records.value?.rows.length ?? 0)

/** 表头与空状态都要说明「这是哪一段」 */
const rangeLabel = computed(() => {
  if (customActive.value) {
    return `${start.value.replace('T', ' ')} 至 ${end.value.replace('T', ' ')}`
  }
  return RANGES.find((item) => item.hours === hours.value)?.label ?? `近 ${hours.value} 小时`
})

function num(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

async function loadDaily(): Promise<void> {
  dailyLoading.value = true
  dailyError.value = ''
  try {
    daily.value = await api.daily(30)
  } catch (err) {
    if (auth.handle(err)) return
    dailyError.value = err instanceof Error ? err.message : '每日用电加载失败'
  } finally {
    dailyLoading.value = false
  }
}

async function loadRecords(): Promise<void> {
  recordsLoading.value = true
  recordsError.value = ''
  try {
    records.value = await api.data(hours.value)
  } catch (err) {
    if (auth.handle(err)) return
    recordsError.value = err instanceof Error ? err.message : '采集记录加载失败'
  } finally {
    recordsLoading.value = false
  }
}

/** 点快速区间：清掉自定义区间的状态，回到 ``/api/data?hours=`` */
async function selectRange(value: number): Promise<void> {
  hours.value = value
  customActive.value = false
  customOpen.value = false
  await loadRecords()
}

async function applyFilter(): Promise<void> {
  if (!start.value || !end.value) {
    recordsError.value = '请同时填写起始与截止时间'
    return
  }
  filtering.value = true
  recordsError.value = ''
  try {
    // ``datetime-local`` 给的是 ``2026-10-06T12:00``；后端按朴素本地时间比较，
    // 直接把 ``T`` 换成空格即可（不做时区转换 —— 全栈都是 CST 朴素时间）
    records.value = await api.dataRange(start.value.replace('T', ' '), end.value.replace('T', ' '))
    customActive.value = true
  } catch (err) {
    if (auth.handle(err)) return
    recordsError.value = err instanceof Error ? err.message : '查询失败'
  } finally {
    filtering.value = false
  }
}

async function reset(): Promise<void> {
  start.value = ''
  end.value = ''
  customActive.value = false
  recordsError.value = ''
  await loadRecords()
}

onMounted(async () => {
  await Promise.all([loadDaily(), loadRecords()])
})
</script>

<template>
  <div class="space-y-4">
    <!-- E6 每日用电 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="dailyLoading">
      <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="sw-section-title flex items-center gap-1.5">
            <Icon name="calendar" :size="15" class="text-muted" />
            每日用电
          </h2>
          <p class="mt-1 text-xs text-muted">近 30 天 · 由每日累计表码求差得出</p>
        </div>
        <span class="sw-chip bg-sunken text-muted">
          <Icon name="activity" :size="12" />
          {{ daily?.rows.length ?? 0 }} 天有数据
        </span>
      </div>

      <Notice v-if="dailyError" tone="danger" class="mb-3">{{ dailyError }}</Notice>

      <div v-if="dailyLoading" class="sw-skeleton h-[220px] w-full" aria-hidden="true" />
      <ChartLine
        v-else
        :labels="(daily?.rows ?? []).map((row) => row.dt.slice(5))"
        :values="(daily?.rows ?? []).map((row) => row.used)"
        unit=" kW·h"
        empty-text="每日用量要拿相邻两天的累计表码求差，所以至少要有连续两天的抄表记录；抓取器默认每 10 分钟抄一次，明天起这里就会有数据。"
      />

      <p class="mt-3 text-xs leading-relaxed text-muted">
        换表 / 重置的那一天会被跳过（与概览页「日均用量」同一套规则）。
      </p>
    </section>

    <!-- E8 区间筛选 + E7 记录表 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="recordsLoading || filtering">
      <div class="mb-4 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 class="sw-section-title flex items-center gap-1.5">
            <Icon name="list" :size="15" class="text-muted" />
            采集记录
          </h2>
          <p class="mt-1 text-xs text-muted">
            {{ rangeLabel }} · 共 {{ total }} 条<template v-if="total > TABLE_LIMIT">
              （表格只渲染最近 {{ TABLE_LIMIT }} 条）</template
            >
          </p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <div
            class="flex gap-1 rounded-control border border-border bg-sunken p-1"
            role="group"
            aria-label="快速时间范围"
          >
            <button
              v-for="range in RANGES"
              :key="range.hours"
              type="button"
              class="rounded-[8px] px-2.5 py-1 text-xs font-medium transition
                     disabled:cursor-not-allowed disabled:opacity-60"
              :class="
                !customActive && hours === range.hours
                  ? 'bg-elevated text-brand shadow-card'
                  : 'text-muted hover:text-content'
              "
              :aria-pressed="!customActive && hours === range.hours"
              :disabled="recordsLoading"
              @click="selectRange(range.hours)"
            >
              {{ range.label }}
            </button>
          </div>

          <BaseButton variant="ghost" size="sm" :aria-expanded="customOpen" @click="customOpen = !customOpen">
            <template #icon><Icon name="filter" :size="14" /></template>
            自定义区间
          </BaseButton>
        </div>
      </div>

      <form
        v-if="customOpen"
        class="mb-4 flex flex-wrap items-end gap-3 rounded-control border border-border bg-surface p-3"
        @submit.prevent="applyFilter"
      >
        <div>
          <label class="mb-1.5 block text-xs font-medium text-content" for="history-start">起始</label>
          <input
            id="history-start"
            v-model="start"
            type="datetime-local"
            class="sw-input w-52"
            :disabled="filtering"
          />
        </div>
        <div>
          <label class="mb-1.5 block text-xs font-medium text-content" for="history-end">截止</label>
          <input
            id="history-end"
            v-model="end"
            type="datetime-local"
            class="sw-input w-52"
            :disabled="filtering"
          />
        </div>
        <div class="flex items-center gap-2">
          <BaseButton type="submit" size="sm" :loading="filtering">
            <template #icon><Icon name="search" :size="14" /></template>
            查询
          </BaseButton>
          <BaseButton variant="ghost" size="sm" :disabled="filtering" @click="reset">
            <template #icon><Icon name="reset" :size="14" /></template>
            重置
          </BaseButton>
        </div>
      </form>

      <Notice v-if="recordsError" tone="danger" class="mb-3">{{ recordsError }}</Notice>

      <EmptyState
        v-if="!recordsLoading && !rows.length"
        title="这个区间还没有采集记录"
        description="抓取器默认每 10 分钟抄表一次；等下一次抓取成功后，记录就会出现在这里。"
        icon="inbox"
      />

      <div v-else class="max-h-[26rem] overflow-auto rounded-control border border-border">
        <table class="w-full min-w-[34rem] border-collapse text-sm">
          <caption class="sr-only">
            采集记录（{{ rangeLabel }}），按采集时间倒序，最多显示 {{ TABLE_LIMIT }} 条
          </caption>
          <thead>
            <tr>
              <th
                scope="col"
                class="sticky top-0 z-10 whitespace-nowrap border-b border-border bg-surface px-3 py-2
                       text-left text-xs font-medium text-muted"
              >
                采集时间
              </th>
              <th
                scope="col"
                class="sticky top-0 z-10 whitespace-nowrap border-b border-border bg-surface px-3 py-2
                       text-left text-xs font-medium text-muted"
              >
                抄表时间
              </th>
              <th
                scope="col"
                class="sticky top-0 z-10 whitespace-nowrap border-b border-border bg-surface px-3 py-2
                       text-right text-xs font-medium text-muted"
              >
                剩余（kW·h）
              </th>
            </tr>
          </thead>
          <tbody>
            <template v-if="recordsLoading">
              <tr v-for="n in 5" :key="`skeleton-${n}`" class="border-b border-border/60 last:border-0">
                <td class="px-3 py-3"><div class="sw-skeleton h-3 w-32" /></td>
                <td class="px-3 py-3"><div class="sw-skeleton h-3 w-32" /></td>
                <td class="px-3 py-3"><div class="sw-skeleton ml-auto h-3 w-12" /></td>
              </tr>
            </template>
            <template v-else>
              <tr
                v-for="row in rows"
                :key="row.id"
                class="border-b border-border/60 transition last:border-0
                       even:bg-surface/60 hover:bg-brand-soft/50"
              >
                <td class="whitespace-nowrap px-3 py-2.5 tabular text-content">{{ row.ts }}</td>
                <td class="whitespace-nowrap px-3 py-2.5 tabular text-muted">
                  {{ row.read_time ?? '—' }}
                </td>
                <td class="whitespace-nowrap px-3 py-2.5 text-right tabular font-medium text-content">
                  {{ num(row.remain) }}
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
