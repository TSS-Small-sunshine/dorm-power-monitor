<script setup lang="ts">
/**
 * 按钮（M5 §5.2）—— 全站唯一按钮样式
 *
 * `type` 默认 `button`：**不是** `submit`。HTML 里 `<button>` 默认就是 submit，
 * 一个没写完的表单会在用户按回车时意外提交 —— 显式默认更安全。
 */
withDefaults(
  defineProps<{
    variant?: 'primary' | 'ghost' | 'danger'
    type?: 'button' | 'submit'
    loading?: boolean
    disabled?: boolean
    block?: boolean
  }>(),
  {
    variant: 'primary',
    type: 'button',
    loading: false,
    disabled: false,
    block: false,
  },
)

const VARIANTS: Record<string, string> = {
  primary: 'bg-brand text-white hover:brightness-110 active:brightness-95',
  ghost: 'border border-border bg-elevated text-content hover:bg-surface',
  danger: 'bg-danger text-white hover:brightness-110 active:brightness-95',
}
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    :aria-busy="loading"
    class="inline-flex items-center justify-center gap-2 rounded-control px-4 py-2 text-sm font-medium
           transition disabled:cursor-not-allowed disabled:opacity-60"
    :class="[VARIANTS[variant], block ? 'w-full' : '']"
  >
    <span
      v-if="loading"
      class="h-3.5 w-3.5 animate-spin rounded-full border-2 border-current border-t-transparent"
      aria-hidden="true"
    />
    <slot />
  </button>
</template>
