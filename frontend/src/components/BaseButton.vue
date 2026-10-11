<script setup lang="ts">
/**
 * 按钮（M5 §5.2 / UI 改版 v2）—— 全站唯一按钮样式
 *
 * `type` 默认 `button`：**不是** `submit`。HTML 里 `<button>` 默认就是 submit，
 * 一个没写完的表单会在用户按回车时意外提交 —— 显式默认更安全。
 *
 * v2 改了三处
 * ==========
 *
 * 1. **前景色不再是写死的 `text-white`**：主题色可被用户改成浅色，白字压上去
 *    会看不见。改用 `text-brand-ink`（由 `theme.ts` 按品牌色亮度算出来），
 *    并把悬停从 `brightness-110` 换成 `bg-brand-strong`（浅色主题压深、
 *    深色主题提亮，方向由 CSS 变量决定）。
 * 2. **danger 改成「柔和底 + 描边」**：实心红底在深色主题下是浅红
 *    （`--color-danger` 深色下为 #F8717D），白字对比度只有 2 点几；
 *    柔和底在两套主题下都读得清，且同样一眼看出是破坏性操作。
 * 3. **加了 `sm` 尺寸与 `icon` 插槽**：工具条上的次级按钮不必再各自写 class。
 */
withDefaults(
  defineProps<{
    variant?: 'primary' | 'ghost' | 'danger' | 'subtle'
    size?: 'sm' | 'md'
    type?: 'button' | 'submit'
    loading?: boolean
    disabled?: boolean
    block?: boolean
  }>(),
  {
    variant: 'primary',
    size: 'md',
    type: 'button',
    loading: false,
    disabled: false,
    block: false,
  },
)

const VARIANTS: Record<string, string> = {
  primary: 'bg-brand text-brand-ink hover:bg-brand-strong',
  ghost: 'border border-border bg-elevated text-content hover:border-border-strong hover:bg-surface',
  subtle: 'bg-brand-soft text-brand hover:bg-brand/20',
  danger: 'border border-danger/35 bg-danger-soft text-danger hover:border-danger/60',
}

const SIZES: Record<string, string> = {
  sm: 'px-2.5 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
}
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading"
    class="inline-flex items-center justify-center gap-1.5 rounded-control font-medium
           transition duration-150 disabled:cursor-not-allowed disabled:opacity-55"
    :class="[VARIANTS[variant], SIZES[size], block ? 'w-full' : '']"
  >
    <span
      v-if="loading"
      class="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
      aria-hidden="true"
    />
    <slot name="icon" />
    <slot />
  </button>
</template>
