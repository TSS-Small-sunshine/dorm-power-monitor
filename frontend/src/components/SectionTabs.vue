<script setup lang="ts">
/**
 * 四段式顶部导航（E1）
 *
 * 无障碍：用 `role="tablist"` + `aria-selected`，键盘左右键可切换
 * （Q21 的「键盘全程可达」不是只针对登录页）。
 */
import { useSection, type SectionKey } from '@/composables/useSection'

const { section, sections } = useSection()

function onKeydown(event: KeyboardEvent): void {
  const index = sections.findIndex((s) => s.key === section.value)
  if (event.key === 'ArrowRight') {
    event.preventDefault()
    section.value = sections[(index + 1) % sections.length]!.key
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault()
    section.value = sections[(index - 1 + sections.length) % sections.length]!.key
  }
}

function select(key: SectionKey): void {
  section.value = key
}
</script>

<template>
  <nav
    class="flex gap-1 overflow-x-auto border-b border-border"
    role="tablist"
    aria-label="仪表盘分区"
    @keydown="onKeydown"
  >
    <button
      v-for="item in sections"
      :key="item.key"
      type="button"
      role="tab"
      :aria-selected="section === item.key"
      :tabindex="section === item.key ? 0 : -1"
      class="flex shrink-0 items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium transition"
      :class="
        section === item.key
          ? 'border-brand text-brand'
          : 'border-transparent text-muted hover:text-content'
      "
      @click="select(item.key)"
    >
      <span aria-hidden="true">{{ item.icon }}</span>
      {{ item.label }}
    </button>
  </nav>
</template>
