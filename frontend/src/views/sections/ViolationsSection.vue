<script setup lang="ts">
/**
 * 违规段（E9 / E10 / UI 改版 v2）
 *
 * 两条 legacy 判定规则，注意单位与文案
 * =====================================
 *
 * * **E9 红色标签**：legacy 写的是 ``wg_power > 1000``，因为它把同一个数字
 *   按 **W** 显示。重写把单位统一成 **kW**（L7），所以阈值是 ``> 1`` kW。
 *   直接照抄 1000 会导致「永远不标红」—— 这是本次重写要修的隐性 bug 之一。
 * * **E10 红色金额**：``fee_type`` 里含「罚」字（退费/罚款）标红。
 *
 * v2 改了什么（全部是**展示层**，两条判定规则一个字没动）
 * ======================================================
 *
 * 1. **一条违规 = 一行记录**：左边图标给严重度，中间是原因 + 时间，
 *    右边是功率胶囊 —— 时间 / 原因 / 功率三件事各自有位，不再挤成一团。
 * 2. **没有违规时给正面表述**：旧版是「没有违规记录 🎉」，emoji 在不同
 *    平台字形不一致，读屏还会念出「派对彩球」；现在交给 ``EmptyState``
 *    说清「近 30 天一切正常」以及多久检查一次。
 * 3. **加载态是骨架屏**：旧版加载中会先闪一句「没有违规记录」，用户以为
 *    一切正常，数据到了又突然冒出一屏红。
 * 4. **错误走 ``Notice``**：与仪表盘其它提示条同一套外观，danger 自带
 *    ``role="alert"``，读屏会立刻播报。
 * 5. **功率胶囊用 ``sw-chip``**：与全站状态胶囊同一套样式，不再手拼
 *    ``rounded-control`` + 透明度底色。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { PaymentPayload, ViolationPayload } from '@/api/types'
import EmptyState from '@/components/EmptyState.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

/** 高功率阈值（kW）—— legacy 的 1000 是「W 口径」，见组件头部说明 */
const HIGH_POWER_KW = 1

const violations = ref<ViolationPayload | null>(null)
const payments = ref<PaymentPayload | null>(null)
const loading = ref(true)
const error = ref('')

const violationRows = computed(() => violations.value?.rows ?? [])
const paymentRows = computed(() => payments.value?.rows ?? [])

function num(value: number | null | undefined, digits = 2): string {
  return value === null || value === undefined ? '—' : value.toFixed(digits)
}

function text(value: string | null | undefined): string {
  return value && value.trim() !== '' ? value : '—'
}

function isHighPower(power: number | null): boolean {
  return power !== null && power > HIGH_POWER_KW
}

/** E10：含「罚」字标红（退费/罚款） */
function isPenalty(row: { fee_type: string | null }): boolean {
  return (row.fee_type ?? '').includes('罚')
}

onMounted(async () => {
  try {
    const [v, p] = await Promise.all([api.violations(30), api.payments(90)])
    violations.value = v
    payments.value = p
  } catch (err) {
    if (auth.handle(err)) return
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div class="space-y-4">
    <Notice v-if="error" tone="danger">{{ error }}</Notice>

    <!-- E9 违规记录 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="loading">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="sw-section-title flex items-center gap-1.5">
            <Icon name="alert" :size="15" class="text-muted" />
            违规记录
          </h2>
          <p class="mt-1 text-xs text-muted">近 30 天 · 按时间倒序 · 功率单位为 kW</p>
        </div>
        <span
          class="sw-chip"
          :class="violationRows.length ? 'bg-danger-soft text-danger' : 'bg-success-soft text-success'"
        >
          <Icon :name="violationRows.length ? 'alert' : 'shield'" :size="12" />
          <template v-if="loading">加载中</template>
          <template v-else>{{ violationRows.length }} 条</template>
        </span>
      </div>

      <div v-if="loading" class="space-y-2" aria-hidden="true">
        <div v-for="n in 3" :key="n" class="sw-skeleton h-14 w-full" />
      </div>

      <EmptyState
        v-else-if="!violationRows.length"
        title="近 30 天没有违规记录"
        description="用电一切正常，继续保持。系统每 10 分钟检查一次，出现违规会第一时间推送提醒。"
        icon="shield"
      />

      <ul v-else class="divide-y divide-border/60">
        <li
          v-for="(row, index) in violationRows"
          :key="`${row.dt}-${index}`"
          class="flex flex-wrap items-center gap-3 py-3"
        >
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
            :class="isHighPower(row.power) ? 'bg-danger-soft text-danger' : 'bg-sunken text-muted'"
            aria-hidden="true"
          >
            <Icon :name="isHighPower(row.power) ? 'alert' : 'info'" :size="16" />
          </span>

          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium leading-snug text-content">{{ text(row.reason) }}</p>
            <p class="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
              <Icon name="clock" :size="12" />
              <span class="tabular whitespace-nowrap">{{ row.dt }}</span>
            </p>
          </div>

          <span
            class="sw-chip shrink-0 tabular"
            :class="isHighPower(row.power) ? 'bg-danger-soft text-danger' : 'bg-sunken text-muted'"
          >
            <Icon name="zap" :size="12" />
            {{ num(row.power) }} kW
          </span>
        </li>
      </ul>
    </section>

    <!-- E10 缴费记录 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="loading">
      <div class="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="sw-section-title flex items-center gap-1.5">
            <Icon name="wallet" :size="15" class="text-muted" />
            缴费记录
          </h2>
          <p class="mt-1 text-xs text-muted">近 90 天 · 按时间倒序 · 金额单位为元</p>
        </div>
        <span class="sw-chip bg-sunken text-muted">
          <Icon name="list" :size="12" />
          <template v-if="loading">加载中</template>
          <template v-else>{{ paymentRows.length }} 条</template>
        </span>
      </div>

      <div v-if="loading" class="space-y-2" aria-hidden="true">
        <div v-for="n in 3" :key="n" class="sw-skeleton h-14 w-full" />
      </div>

      <EmptyState
        v-else-if="!paymentRows.length"
        title="近 90 天没有缴费记录"
        description="学校门户出现充值或扣费后，这里会按时间倒序列出。"
        icon="wallet"
      />

      <ul v-else class="divide-y divide-border/60">
        <li
          v-for="(row, index) in paymentRows"
          :key="`${row.dt}-${index}`"
          class="flex flex-wrap items-center gap-3 py-3"
        >
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-full"
            :class="isPenalty(row) ? 'bg-danger-soft text-danger' : 'bg-sunken text-muted'"
            aria-hidden="true"
          >
            <Icon :name="isPenalty(row) ? 'alert' : 'wallet'" :size="16" />
          </span>

          <div class="min-w-0 flex-1">
            <p class="text-sm font-medium leading-snug text-content">
              {{ text(row.pay_type) }}
              <span v-if="row.fee_type" class="ml-1 text-xs font-normal text-muted">
                · {{ row.fee_type }}
              </span>
            </p>
            <p class="mt-0.5 flex items-center gap-1.5 text-xs text-muted">
              <Icon name="clock" :size="12" />
              <span class="tabular whitespace-nowrap">{{ row.dt }}</span>
            </p>
          </div>

          <span
            class="shrink-0 tabular text-sm font-semibold"
            :class="isPenalty(row) ? 'text-danger' : 'text-content'"
          >
            {{ num(row.money) }} 元
          </span>
        </li>
      </ul>
    </section>
  </div>
</template>
