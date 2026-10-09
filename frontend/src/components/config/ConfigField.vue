<script setup lang="ts">
/**
 * 配置项控件（5.6）—— **由 schema 的 ``type`` 决定渲染什么**
 *
 * 这就是 Q15 验收里「新增一个配置项只需改后端注册表，前端零改动」的落点：
 * 本文件不出现任何具体配置键名，只认 10 种类型。
 *
 * 几个容易踩的坑，都在这里处理掉
 * ==============================
 *
 * 1. **secret 只在改动时提交**：后端 GET 返回的是 ``••••1234``，
 *    如果原样 PUT 回去，就会把真凭据覆盖成那串圆点。所以只有用户
 *    真敲了新值（``secretDirty``）才进 payload。
 * 2. **json 用 textarea + 本地解析**：解析失败只提示，不提交非法 JSON
 *    （后端也会拒，但用户不该为一次手滑等一个往返）。
 * 3. **bool 的 checkbox 要能读回 false**：``v-model`` 在 checkbox 上
 *    必须绑 ``boolean``，否则「取消勾选」不会进 payload。
 * 4. **state 项只读**：``user_editable=false`` 的是运行时状态
 *    （如 ``oobe_completed``），展示但不允许改 —— 后端也会拒。
 */
import { computed, ref, watch } from 'vue'

import type { ConfigSetting } from '@/api/types'

const props = defineProps<{
  setting: ConfigSetting
  /** 当前值（可能是脱敏串） */
  value: unknown
  /** 该键的校验错误（来自 PUT 的 207 响应） */
  error?: string
}>()

const emit = defineEmits<{ 'update:modelValue': [value: unknown]; dirty: [] }>()

const text = ref('')
const checked = ref(false)
const secretDirty = ref(false)

/** 把任意值转成输入框里的文本 */
function toText(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value, null, 1)
  return String(value)
}

watch(
  () => props.value,
  (value) => {
    text.value = toText(value)
    checked.value = value === true
    secretDirty.value = false
  },
  { immediate: true },
)

/** 校验规则串 → 人话提示（``len:1..32`` / ``regex:...`` / ``0..86400`` …） */
const ruleHint = computed(() => {
  const rule = props.setting.validate
  if (!rule || rule === 'none') return ''
  if (rule.startsWith('len:')) return `长度 ${rule.slice(4)}`
  if (rule.startsWith('enum:')) return `可选：${rule.slice(5).split('|').join(' / ')}`
  if (rule.startsWith('regex:')) return `格式需匹配 ${rule.slice(6)}`
  if (rule === 'url') return '需为完整 URL（http/https）'
  if (rule === 'json') return '合法 JSON'
  if (/^\d+\.\.\d+$/.test(rule)) return `取值范围 ${rule.replace('..', ' ~ ')}`
  return rule
})

const unit = computed(() => (props.setting.type === 'duration' ? '秒' : ''))

function onText(event: Event): void {
  const raw = (event.target as HTMLInputElement | HTMLTextAreaElement).value
  text.value = raw
  emit('dirty')

  if (props.setting.type === 'secret') secretDirty.value = true

  if (props.setting.type === 'int' || props.setting.type === 'duration') {
    const parsed = Number.parseInt(raw, 10)
    emit('update:modelValue', Number.isNaN(parsed) ? raw : parsed)
    return
  }
  if (props.setting.type === 'float') {
    const parsed = Number.parseFloat(raw)
    emit('update:modelValue', Number.isNaN(parsed) ? raw : parsed)
    return
  }
  if (props.setting.type === 'json') {
    try {
      emit('update:modelValue', raw.trim() === '' ? null : JSON.parse(raw))
    } catch {
      emit('update:modelValue', raw) // 非法 JSON：原样带上，让后端给出中文原因
    }
    return
  }
  emit('update:modelValue', raw)
}

function onBool(event: Event): void {
  checked.value = (event.target as HTMLInputElement).checked
  emit('dirty')
  emit('update:modelValue', checked.value)
}

function onSelect(event: Event): void {
  text.value = (event.target as HTMLSelectElement).value
  emit('dirty')
  emit('update:modelValue', text.value)
}
</script>

<template>
  <div class="border-b border-border/60 py-4 last:border-0">
    <div class="flex flex-wrap items-start justify-between gap-3">
      <div class="min-w-0 flex-1">
        <label :for="`cfg-${setting.key}`" class="text-sm font-medium text-content">
          {{ setting.label }}
          <span v-if="setting.secret" class="ml-1 text-xs text-muted">🔒</span>
        </label>
        <p v-if="setting.help" class="mt-0.5 text-xs text-muted">{{ setting.help }}</p>
        <p class="mt-0.5 text-xs text-muted">
          <code class="rounded bg-surface px-1">{{ setting.key }}</code>
          <span v-if="ruleHint"> · {{ ruleHint }}</span>
        </p>
      </div>

      <div class="w-full shrink-0 sm:max-w-sm">
        <!-- bool：开关 -->
        <label v-if="setting.type === 'bool'" class="flex cursor-pointer items-center gap-2">
          <input
            :id="`cfg-${setting.key}`"
            type="checkbox"
            class="h-4 w-4"
            :checked="checked"
            :disabled="!setting.user_editable"
            @change="onBool"
          />
          <span class="text-sm text-muted">{{ checked ? '开启' : '关闭' }}</span>
        </label>

        <!-- enum：下拉 -->
        <select
          v-else-if="setting.type === 'enum'"
          :id="`cfg-${setting.key}`"
          :value="text"
          :disabled="!setting.user_editable"
          class="sw-input"
          @change="onSelect"
        >
          <option v-for="choice in setting.choices" :key="choice" :value="choice">
            {{ choice }}
          </option>
        </select>

        <!-- json：多行 -->
        <textarea
          v-else-if="setting.type === 'json'"
          :id="`cfg-${setting.key}`"
          :value="text"
          rows="3"
          spellcheck="false"
          :disabled="!setting.user_editable"
          class="sw-input font-mono text-xs"
          @input="onText"
        />

        <!-- time：原生时间选择器（HH:MM） -->
        <input
          v-else-if="setting.type === 'time'"
          :id="`cfg-${setting.key}`"
          type="time"
          :value="text"
          :disabled="!setting.user_editable"
          class="sw-input"
          @input="onText"
        />

        <!-- int / float / duration：数字 -->
        <div
          v-else-if="['int', 'float', 'duration'].includes(setting.type)"
          class="flex items-center gap-2"
        >
          <input
            :id="`cfg-${setting.key}`"
            type="number"
            :step="setting.type === 'float' ? '0.01' : '1'"
            :value="text"
            :disabled="!setting.user_editable"
            class="sw-input"
            @input="onText"
          />
          <span v-if="unit" class="shrink-0 text-xs text-muted">{{ unit }}</span>
        </div>

        <!-- secret：密码框；未改动时占位提示保持原值 -->
        <input
          v-else-if="setting.type === 'secret'"
          :id="`cfg-${setting.key}`"
          type="password"
          :value="text"
          :disabled="!setting.user_editable"
          :placeholder="secretDirty ? '' : '留空 / 保持原值'"
          autocomplete="new-password"
          class="sw-input"
          @input="onText"
        />

        <!-- str / url / 兜底 -->
        <input
          v-else
          :id="`cfg-${setting.key}`"
          :type="setting.type === 'url' ? 'url' : 'text'"
          :value="text"
          :disabled="!setting.user_editable"
          class="sw-input"
          @input="onText"
        />

        <p v-if="!setting.user_editable" class="sw-hint">运行时状态，只读</p>
        <p v-if="setting.requires_restart" class="sw-hint">⚠ 改动后需重启服务才生效</p>
        <p v-if="error" class="sw-error" role="alert">{{ error }}</p>
      </div>
    </div>
  </div>
</template>
