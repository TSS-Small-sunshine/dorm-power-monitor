<script setup lang="ts">
/**
 * 电表段（E11）
 *
 * 字段全部是**驼峰**（``run_status`` / ``vol`` / ``cur`` / ``yggl`` / ``update_dt``）——
 * 这是门户返回的形状，也是卡片契约里的形状（见 ``card_builder`` 的说明：
 * 下划线键是内部表示，不该出现在契约里）。这里只展示，不做单位换算：
 * 门户给的就是带单位的字符串（如 ``"220.5"``）。
 */
import { computed } from 'vue'

import type { LivePayload } from '@/api/types'
import StatCard from '@/components/StatCard.vue'

const props = defineProps<{ live: LivePayload | null }>()

const meter = computed(() => props.live?.run_status ?? {})

const FIELDS = [
  { key: 'vol', label: '电压', unit: 'V' },
  { key: 'cur', label: '电流', unit: 'A' },
  { key: 'yggl', label: '有功功率', unit: 'W' },
] as const

function text(value: string | undefined): string {
  return value && value.trim() !== '' ? value : '—'
}

const offline = computed(() => {
  const status = meter.value.run_status ?? ''
  return status !== '' && !['在线', '正常', '通讯正常'].includes(status)
})
</script>

<template>
  <div class="space-y-4">
    <section class="sw-card p-5">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <p class="text-xs text-muted">电表状态</p>
          <p class="mt-1 text-2xl font-bold" :class="offline ? 'text-danger' : 'text-content'">
            {{ text(meter.run_status) }}
          </p>
          <p class="mt-1 text-xs text-muted">最后上报 {{ text(meter.update_dt) }}</p>
        </div>
        <p v-if="offline" class="rounded-control bg-danger/10 px-3 py-2 text-xs text-danger">
          ⚠ 电表不在线 —— 数据可能停止更新，请检查宿舍配电箱或联系宿管。
        </p>
      </div>
    </section>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-3">
      <StatCard
        v-for="field in FIELDS"
        :key="field.key"
        :label="field.label"
        :value="text(meter[field.key])"
        :unit="field.unit"
      />
    </div>

    <p class="text-xs text-muted">
      实时读数来自学校门户的 F4 接口，随抓取周期更新（默认 10 分钟一次）。
    </p>
  </div>
</template>
