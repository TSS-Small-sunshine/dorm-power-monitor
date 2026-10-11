<script setup lang="ts">
/**
 * 空状态（UI 改版 v2，新组件）
 *
 * 旧版每个列表/图表各自写一句「暂无数据」，有的甚至什么都不显示 ——
 * 空白让人以为是坏了，而不是「还没抓到数据」。
 *
 * 三件事必须说清（这也是组件只留三个插槽的原因）：
 *
 * 1. **是什么空了**（`title`）—— 「还没有违规记录」
 * 2. **为什么**（`description`）—— 「抓取器每 10 分钟检查一次，有新记录会推送到飞书」
 * 3. **能做什么**（默认插槽）—— 可选的操作按钮
 *
 * 图标默认 `inbox`（空收件箱），对「还没有数据」这个语义比问号贴切。
 */
import Icon from '@/components/Icon.vue'

withDefaults(
  defineProps<{
    title: string
    description?: string
    icon?: string
  }>(),
  { description: '', icon: 'inbox' },
)
</script>

<template>
  <div class="flex flex-col items-center justify-center px-6 py-10 text-center">
    <span
      class="mb-3 flex h-11 w-11 items-center justify-center rounded-full bg-sunken text-muted"
      aria-hidden="true"
    >
      <Icon :name="icon" :size="20" />
    </span>
    <p class="text-sm font-medium text-content">{{ title }}</p>
    <p v-if="description" class="mt-1 max-w-sm text-xs leading-relaxed text-muted">
      {{ description }}
    </p>
    <div class="mt-4 empty:hidden">
      <slot />
    </div>
  </div>
</template>
