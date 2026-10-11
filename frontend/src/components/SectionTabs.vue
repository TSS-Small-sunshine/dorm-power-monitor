<script setup lang="ts">
/**
 * 四段式顶部导航（E1 / UI 改版 v2）
 *
 * 无障碍：用 `role="tablist"` + `aria-selected`，键盘左右键可切换
 * （Q21 的「键盘全程可达」不是只针对登录页）。
 *
 * v2 从「下划线标签」改成「分段控件」
 * ================================
 *
 * 旧版是 4 个带下划线的文字标签，与页面里其它的链接/按钮视觉上分不开，
 * 看不出「这是一组互斥的视图切换」。分段控件（灰底 + 选中项浮起）是
 * 移动端与桌面端都通用的表达，且选中态不靠颜色单独承担 —— 还有底色与阴影。
 *
 * 窄屏：等分铺满（`flex-1`）而不是横向滚动 —— 4 个中文标签放得下，
 * 滚动条反而会藏掉一整个分区。
 */
import Icon from '@/components/Icon.vue'
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
    class="flex w-full gap-1 rounded-control border border-border bg-sunken p-1
           sm:inline-flex sm:w-auto"
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
      class="flex flex-1 items-center justify-center gap-1.5 rounded-[8px] px-3 py-2
             text-sm font-medium transition duration-150 sm:flex-none"
      :class="
        section === item.key
          ? 'bg-elevated text-brand shadow-card'
          : 'text-muted hover:text-content'
      "
      @click="select(item.key)"
    >
      <Icon :name="item.icon" :size="15" />
      {{ item.label }}
    </button>
  </nav>
</template>
