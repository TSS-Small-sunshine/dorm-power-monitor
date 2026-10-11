<script setup lang="ts">
/**
 * 深浅色切换（M5 §5.2 / UI 改版 v2）
 *
 * 三态循环「跟随系统 → 浅色 → 深色」，选择存 `localStorage`（见 theme.ts）。
 *
 * v2：图标从 emoji（`🖥 ☀ 🌙`）换成 SVG。emoji 依赖系统字体，在 Linux 上
 * 是单色字形、在 Windows 上是彩色，同一份代码两个平台长得不一样；而这是个
 * 固定尺寸的小按钮，字形一变大就会顶到边框。宽屏下补一个文字标签 ——
 * 只有图标的按钮，用户得先点一次才知道它是什么。
 */
import { computed, ref } from 'vue'

import Icon from '@/components/Icon.vue'
import { applyTheme, currentTheme, type ThemeChoice } from '@/theme'

const choice = ref<ThemeChoice>(currentTheme())

const LABELS: Record<ThemeChoice, string> = {
  system: '跟随系统',
  light: '浅色',
  dark: '深色',
}

const ICONS: Record<ThemeChoice, string> = {
  system: 'monitor',
  light: 'sun',
  dark: 'moon',
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
    class="inline-flex items-center gap-1.5 rounded-control border border-border bg-elevated
           px-2.5 py-1.5 text-sm text-content transition hover:border-border-strong hover:bg-surface"
    :aria-label="`外观：${label}（点击切换）`"
    :title="`外观：${label}`"
    @click="cycle"
  >
    <Icon :name="ICONS[choice]" :size="15" />
    <span class="hidden sm:inline">{{ label }}</span>
  </button>
</template>
