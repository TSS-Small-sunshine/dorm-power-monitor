<script setup lang="ts">
/**
 * 深浅色切换（M5 §5.2）
 *
 * 三态循环「跟随系统 → 浅色 → 深色」，选择存 `localStorage`（见 theme.ts）。
 * 图标用 emoji + 自带 NotoEmoji 字体渲染，避免为了两个图标再打一个 SVG 库。
 */
import { computed, ref } from 'vue'

import { applyTheme, currentTheme, type ThemeChoice } from '@/theme'

const choice = ref<ThemeChoice>(currentTheme())

const LABELS: Record<ThemeChoice, string> = {
  system: '跟随系统',
  light: '浅色',
  dark: '深色',
}

const ICONS: Record<ThemeChoice, string> = {
  system: '🖥',
  light: '☀',
  dark: '🌙',
}

const label = computed(() => LABELS[choice.value])

function cycle(): void {
  const order: ThemeChoice[] = ['system', 'light', 'dark']
  const next = order[(order.indexOf(choice.value) + 1) % order.length]!
  choice.value = next
  applyTheme(next)
}
</script>

<template>
  <button
    type="button"
    class="rounded-control border border-border bg-elevated px-2.5 py-1.5 text-sm text-content
           transition hover:bg-surface"
    :aria-label="`外观：${label}（点击切换）`"
    :title="`外观：${label}`"
    @click="cycle"
  >
    <span aria-hidden="true">{{ ICONS[choice] }}</span>
  </button>
</template>
