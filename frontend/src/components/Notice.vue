<script setup lang="ts">
/**
 * 提示条（UI 改版 v2，新组件）
 *
 * 仪表盘与后台里有 6 处「一行彩色小字」（陈旧警告 / 刷新结果 / 表单级错误 /
 * 导入导出结果），旧版各自手写 `rounded bg-brand/10 px-3 py-2 text-xs` ——
 * 于是字号、留白、图标有无各不相同。
 *
 * 语义与无障碍
 * ============
 *
 * * `warning` / `danger` 用 `role="alert"`（读屏立刻播报）
 * * `info` / `success` 用 `role="status"`（等当前朗读结束再播报）
 * * 图标与颜色**同时**表达严重性 —— 只靠颜色的话，色觉障碍用户区分不出
 */
import { computed } from 'vue'

import Icon from '@/components/Icon.vue'

const props = withDefaults(
  defineProps<{
    tone?: 'info' | 'success' | 'warning' | 'danger'
    /** 覆盖默认图标（默认按 tone 选） */
    icon?: string
  }>(),
  { tone: 'info', icon: '' },
)

const TONES: Record<string, { box: string; icon: string }> = {
  info: { box: 'border-brand/25 bg-brand-soft text-brand', icon: 'info' },
  success: { box: 'border-success/25 bg-success-soft text-success', icon: 'check' },
  warning: { box: 'border-warning/30 bg-warning-soft text-warning', icon: 'warning' },
  danger: { box: 'border-danger/30 bg-danger-soft text-danger', icon: 'alert' },
}

const tone = computed(() => TONES[props.tone] ?? TONES.info!)
const role = computed(() => (props.tone === 'warning' || props.tone === 'danger' ? 'alert' : 'status'))
</script>

<template>
  <div class="sw-notice" :class="tone.box" :role="role">
    <Icon :name="icon || tone.icon" :size="16" class="mt-0.5" />
    <div class="min-w-0 flex-1 leading-relaxed">
      <slot />
    </div>
  </div>
</template>
