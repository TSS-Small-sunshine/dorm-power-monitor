<script setup lang="ts">
/**
 * 历史段（E6 / E7 / E8）
 *
 * * E6 每日用电曲线：``/api/daily``（后端已把累计表码推导成每日用量）
 * * E7 采集记录表：``/api/data``（倒序展示，最新在上）
 * * E8 区间筛选：``datetime-local`` + 查询/重置，走 ``/api/data?start=&end=``
 *
 * 为什么表格要倒序：用户关心的是「刚刚发生了什么」。后端按升序返回
 * （画图需要），所以**在前端翻转**，而不是让后端为两种用途各排一次。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { DailyPayload, DataPayload } from '@/api/types'
import ChartLine from '@/components/ChartLine.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const daily = ref<DailyPayload | null>(null)
const records = ref<DataPayload | null>(null)
const loading = ref(true)
const error = ref('')

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

function num(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

async function loadDaily(): Promise<void> {
  try {
    daily.value = await api.daily(30)
  } catch (err) {
    if (auth.handle(err)) return
    error.value = err instanceof Error ? err.message : '每日用电加载失败'
  }
}

async function loadRecords(): Promise<void> {
  try {
    records.value = await api.data(24)
  } catch (err) {
    if (auth.handle(err)) return
    error.value = err instanceof Error ? err.message : '采集记录加载失败'
  }
}

async function applyFilter(): Promise<void> {
  if (!start.value || !end.value) {
    error.value = '请同时填写起始与截止时间'
    return
  }
  filtering.value = true
  error.value = ''
  try {
    // ``datetime-local`` 给的是 ``2026-10-06T12:00``；后端按朴素本地时间比较，
    // 直接把 ``T`` 换成空格即可（不做时区转换 —— 全栈都是 CST 朴素时间）
    records.value = await api.dataRange(start.value.replace('T', ' '), end.value.replace('T', ' '))
  } catch (err) {
    if (auth.handle(err)) return
    error.value = err instanceof Error ? err.message : '查询失败'
  } finally {
    filtering.value = false
  }
}

async function reset(): Promise<void> {
  start.value = ''
  end.value = ''
  error.value = ''
  await loadRecords()
}

onMounted(async () => {
  await Promise.all([loadDaily(), loadRecords()])
  loading.value = false
})
</script>

<template>
  <div class="space-y-4">
    <!-- E6 每日用电 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">每日用电（近 30 天）</h2>
      <ChartLine
        :labels="(daily?.rows ?? []).map((row) => row.dt.slice(5))"
        :values="(daily?.rows ?? []).map((row) => row.used)"
        unit=" kW·h"
        empty-text="还没有每日用电数据（需要连续两天的表码）"
      />
      <p class="mt-2 text-xs text-muted">
        由每日累计表码求差得出；换表/重置的那一天会被跳过（与「日均用量」同一套规则）。
      </p>
    </section>

    <!-- E8 区间筛选 + E7 记录表 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">采集记录</h2>

      <form class="mb-4 flex flex-wrap items-end gap-2" @submit.prevent="applyFilter">
        <label class="flex flex-col gap-1 text-xs text-muted">
          起始
          <input v-model="start" type="datetime-local" class="sw-input w-52" :disabled="filtering" />
        </label>
        <label class="flex flex-col gap-1 text-xs text-muted">
          截止
          <input v-model="end" type="datetime-local" class="sw-input w-52" :disabled="filtering" />
        </label>
        <button
          type="submit"
          class="rounded-control bg-brand px-3 py-2 text-sm font-medium text-white transition hover:brightness-110 disabled:opacity-60"
          :disabled="filtering"
        >
          {{ filtering ? '查询中…' : '查询' }}
        </button>
        <button
          type="button"
          class="rounded-control border border-border px-3 py-2 text-sm text-content transition hover:bg-surface"
          :disabled="filtering"
          @click="reset"
        >
          重置
        </button>
      </form>

      <p v-if="error" class="sw-error mb-2" role="alert">{{ error }}</p>

      <div class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-left text-xs text-muted">
              <th class="py-2 pr-3 font-medium">采集时间</th>
              <th class="py-2 pr-3 font-medium">抄表时间</th>
              <th class="py-2 font-medium">剩余（kW·h）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in rows" :key="row.id" class="border-b border-border/60 last:border-0">
              <td class="py-2 pr-3 text-content">{{ row.ts }}</td>
              <td class="py-2 pr-3 text-muted">{{ row.read_time ?? '—' }}</td>
              <td class="py-2 text-content">{{ num(row.remain) }}</td>
            </tr>
            <tr v-if="!rows.length && !loading">
              <td colspan="3" class="py-6 text-center text-muted">没有记录</td>
            </tr>
          </tbody>
        </table>
      </div>

      <p v-if="(records?.rows.length ?? 0) > TABLE_LIMIT" class="mt-2 text-xs text-muted">
        仅显示最近 {{ TABLE_LIMIT }} 条（共 {{ records?.rows.length }} 条）
      </p>
    </section>
  </div>
</template>
