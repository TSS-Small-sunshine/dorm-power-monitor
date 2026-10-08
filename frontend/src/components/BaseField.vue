<script setup lang="ts">
/**
 * 表单字段（M5 §5.2）—— 标签 / 提示 / 行内错误 + 密码可见性切换
 *
 * 为什么自己做而不引组件库：Q21 要求 6 个认证界面**视觉一致**，
 * 而「一致」的前提是只有一套输入框实现（`.sw-input`）。
 *
 * 无障碍要点
 * ==========
 *
 * * `label` 的 `for` 与 input 的 `id` 绑定（点标签能聚焦）
 * * 错误用 `aria-describedby` 关联，读屏能听到
 * * 密码切换按钮 `type="button"`（否则会提交表单）且带 `aria-pressed`
 */
import { computed, ref } from 'vue'

const props = withDefaults(
  defineProps<{
    modelValue: string
    label: string
    type?: 'text' | 'password'
    id?: string
    placeholder?: string
    autocomplete?: string
    hint?: string
    error?: string
    disabled?: boolean
    autofocus?: boolean
  }>(),
  {
    type: 'text',
    id: undefined,
    placeholder: '',
    autocomplete: undefined,
    hint: '',
    error: '',
    disabled: false,
    autofocus: false,
  },
)

const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const revealed = ref(false)
const inputId = computed(() => props.id ?? `field-${Math.random().toString(36).slice(2, 9)}`)
const hintId = computed(() => `${inputId.value}-hint`)
const errorId = computed(() => `${inputId.value}-error`)
const describedBy = computed(() => {
  const ids = [props.hint ? hintId.value : '', props.error ? errorId.value : ''].filter(Boolean)
  return ids.length ? ids.join(' ') : undefined
})
const inputType = computed(() =>
  props.type === 'password' && !revealed.value ? 'password' : 'text',
)

function onInput(event: Event): void {
  emit('update:modelValue', (event.target as HTMLInputElement).value)
}
</script>

<template>
  <div>
    <label class="sw-label" :for="inputId">{{ label }}</label>

    <div class="relative">
      <input
        :id="inputId"
        :type="inputType"
        :value="modelValue"
        :placeholder="placeholder"
        :autocomplete="autocomplete"
        :disabled="disabled"
        :autofocus="autofocus"
        :aria-invalid="error ? 'true' : undefined"
        :aria-describedby="describedBy"
        class="sw-input"
        :class="type === 'password' ? 'pr-11' : ''"
        @input="onInput"
      />

      <button
        v-if="type === 'password'"
        type="button"
        class="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-muted hover:text-content"
        :aria-pressed="revealed"
        :aria-label="revealed ? '隐藏密码' : '显示密码'"
        @click="revealed = !revealed"
      >
        <span aria-hidden="true">{{ revealed ? '🙈' : '👁' }}</span>
      </button>
    </div>

    <p v-if="hint && !error" :id="hintId" class="sw-hint">{{ hint }}</p>
    <p v-if="error" :id="errorId" class="sw-error" role="alert">{{ error }}</p>
  </div>
</template>
