<script setup lang="ts">
/**
 * 统计卡 / 信息卡（E4）
 *
 * `value` 为 `null` 时显示 `—`：后端的 null 表示「窗口不够，算不出来」
 * （日均、近一小时），显示 0 会是个假数字。
 */
withDefaults(
  defineProps<{
    label: string
    value: string
    unit?: string
    /** 次要说明（如「最后更新」） */
    hint?: string
    tone?: 'default' | 'warning' | 'danger'
  }>(),
  { unit: '', hint: '', tone: 'default' },
)

const TONES: Record<string, string> = {
  default: 'text-content',
  warning: 'text-warning',
  danger: 'text-danger',
}
</script>

<template>
  <div class="sw-card p-4">
    <p class="text-xs text-muted">{{ label }}</p>
    <p class="mt-1 text-2xl font-bold" :class="TONES[tone]">
      {{ value }}
      <span v-if="unit" class="ml-0.5 text-xs font-normal text-muted">{{ unit }}</span>
    </p>
    <p v-if="hint" class="mt-0.5 text-xs text-muted">{{ hint }}</p>
  </div>
</template>
