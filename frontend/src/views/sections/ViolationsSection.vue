<script setup lang="ts">
/**
 * 违规段（E9 / E10）
 *
 * 两条 legacy 判定规则，注意单位与文案
 * =====================================
 *
 * * **E9 红色标签**：legacy 写的是 ``wg_power > 1000``，因为它把同一个数字
 *   按 **W** 显示。重写把单位统一成 **kW**（L7），所以阈值是 ``> 1`` kW。
 *   直接照抄 1000 会导致「永远不标红」—— 这是本次重写要修的隐性 bug 之一。
 * * **E10 红色金额**：``fee_type`` 里含「罚」字（退费/罚款）标红。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { PaymentPayload, ViolationPayload } from '@/api/types'
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
    <p v-if="error" class="rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ error }}
    </p>

    <!-- E9 违规记录 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">
        违规记录
        <span class="ml-1 text-xs font-normal text-muted">近 30 天 · {{ violationRows.length }} 条</span>
      </h2>

      <ul class="divide-y divide-border/60">
        <li v-for="row in violationRows" :key="row.dt" class="flex items-center justify-between gap-3 py-2.5">
          <div class="min-w-0">
            <p class="truncate text-sm text-content">{{ row.reason || '—' }}</p>
            <p class="text-xs text-muted">{{ row.dt }}</p>
          </div>
          <span
            class="shrink-0 rounded-control px-2 py-0.5 text-xs font-medium"
            :class="
              isHighPower(row.power)
                ? 'bg-danger/10 text-danger'
                : 'bg-surface text-muted'
            "
          >
            {{ num(row.power) }} kW
          </span>
        </li>
        <li v-if="!violationRows.length && !loading" class="py-6 text-center text-sm text-muted">
          没有违规记录 🎉
        </li>
      </ul>
    </section>

    <!-- E10 缴费记录 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">
        缴费记录
        <span class="ml-1 text-xs font-normal text-muted">近 90 天 · {{ paymentRows.length }} 条</span>
      </h2>

      <ul class="divide-y divide-border/60">
        <li v-for="row in paymentRows" :key="`${row.dt}-${row.money}`" class="flex items-center justify-between gap-3 py-2.5">
          <div class="min-w-0">
            <p class="truncate text-sm text-content">
              {{ row.pay_type || '—' }}
              <span v-if="row.fee_type" class="text-xs text-muted">· {{ row.fee_type }}</span>
            </p>
            <p class="text-xs text-muted">{{ row.dt }}</p>
          </div>
          <span
            class="shrink-0 text-sm font-medium"
            :class="isPenalty(row) ? 'text-danger' : 'text-content'"
          >
            {{ num(row.money) }} 元
          </span>
        </li>
        <li v-if="!paymentRows.length && !loading" class="py-6 text-center text-sm text-muted">
          没有缴费记录
        </li>
      </ul>
    </section>
  </div>
</template>
