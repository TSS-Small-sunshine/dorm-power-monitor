<script setup lang="ts">
/**
 * 统计卡 / 信息卡（E4 / UI 改版 v2）
 *
 * `value` 为 `null` 时显示 `—`：后端的 null 表示「窗口不够，算不出来」
 * （日均、近一小时），显示 0 会是个假数字。
 *
 * v2 改了三处
 * ==========
 *
 * 1. **加了图标位**：4 张卡并排时，扫一眼图标比读 4 个字更快定位
 *    （「哪张是钱、哪张是电」）。
 * 2. **`loading` 显示骨架屏**：旧版加载中直接显示 `—`，用户会以为
 *    「这个指标一直是空的」——骨架屏表达的才是「还没到」。
 * 3. **数字用等宽字形**（`.tabular`）：4 张卡的数字位数不同时，
 *    等宽让它们对齐，读起来不跳。
 */
import Icon from '@/components/Icon.vue'

withDefaults(
  defineProps<{
    label: string
    value: string
    unit?: string
    /** 次要说明（如「最后更新」） */
    hint?: string
    tone?: 'default' | 'warning' | 'danger'
    /** 图标名（见 `Icon.vue`）；不传则不显示图标 */
    icon?: string
    /** 加载中 → 骨架屏（不要用 `—` 冒充「算不出来」） */
    loading?: boolean
  }>(),
  { unit: '', hint: '', tone: 'default', icon: '', loading: false },
)

const TONES: Record<string, string> = {
  default: 'text-content',
  warning: 'text-warning',
  danger: 'text-danger',
}
</script>

<template>
  <div class="sw-card p-4">
    <div class="flex items-start justify-between gap-2">
      <p class="text-xs font-medium text-muted">{{ label }}</p>
      <span v-if="icon" class="text-muted/70" aria-hidden="true">
        <Icon :name="icon" :size="15" />
      </span>
    </div>

    <template v-if="loading">
      <div class="sw-skeleton mt-2 h-7 w-24" />
      <div class="sw-skeleton mt-2 h-3 w-32" />
    </template>

    <template v-else>
      <p class="mt-1.5 text-2xl font-bold tabular" :class="TONES[tone]">
        {{ value }}
        <span v-if="unit" class="ml-0.5 text-xs font-normal text-muted">{{ unit }}</span>
      </p>
      <p v-if="hint" class="mt-1 text-xs text-muted">{{ hint }}</p>
    </template>
  </div>
</template>
