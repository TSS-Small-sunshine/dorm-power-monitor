<script setup lang="ts">
/**
 * 配置项控件（5.6 / UI 改版 v2）—— **由 schema 的 ``type`` 决定渲染什么**
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
 *
 * v2 只改**展示层**（取值、emit、脱敏与「只在改动时提交」的行为一字未动）
 * ====================================================================
 *
 * * **两列布局**：左列回答「这项是什么」（名称 / 说明 / key / 校验规则），
 *   右列是控件。旧版用 flex-wrap，标签一长控件就被挤到下一行 ——
 *   94 个配置项读下来视线要在左右之间来回跳。
 * * **secret 补了状态胶囊**：旧版只有一个锁的小字，用户分不清
 *   「现在看到的是真值还是圆点」「我改了没有」。现在明确写「未改动 / 已修改」，
 *   并写明**只有改动过才会提交**。
 * * **锁标记换成 ``Icon name="lock"``**：emoji 的字形由系统字体决定，
 *   Linux 上是单色、Windows 上是彩色，且无法与文字基线对齐。
 * * **错误态加红边**：旧版只有下面一行小字，扫一眼看不到是哪个框错了。
 * * **bool 用 ``role="switch"``**：开关语义比裸 checkbox 更贴切；
 *   仍保留原生 checkbox 的 ``@change`` 行为，提交逻辑没变。
 *
 * 注意：本组件同时被 ``views/OobeView.vue``（首次配置向导）复用 ——
 * 新增 props 只能是可选的，已有 props 的名字与语义不许动。
 */
import { computed, ref, watch } from 'vue'

import type { ConfigSetting } from '@/api/types'
import Icon from '@/components/Icon.vue'

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
/** secret 的明文/圆点切换（纯展示，不影响取值） */
const revealed = ref(false)

const inputId = computed(() => `cfg-${props.setting.key}`)
const hintId = computed(() => `${inputId.value}-hint`)
const errorId = computed(() => `${inputId.value}-error`)

/** 说明与错误都要能被读屏关联到控件上 */
const describedBy = computed(() => {
  const ids = [props.setting.help ? hintId.value : '', props.error ? errorId.value : ''].filter(
    Boolean,
  )
  return ids.length ? ids.join(' ') : undefined
})

/** 校验失败时给控件加红边（只改 class，不改取值） */
const errorClass = computed(() =>
  props.error ? 'border-danger focus:border-danger focus:ring-danger/25' : '',
)

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
  <div class="sw-divider py-4 first:border-t-0">
    <div class="grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]">
      <!-- 左列：这项是什么 -->
      <div class="min-w-0">
        <label :for="inputId" class="flex flex-wrap items-center gap-x-2 gap-y-1">
          <span class="text-sm font-medium text-content">{{ setting.label }}</span>

          <span
            v-if="setting.secret"
            class="sw-chip bg-brand-soft text-brand"
            title="加密存储：后端不回显真值，只给脱敏串"
          >
            <Icon name="lock" :size="11" />
            加密存储
          </span>
          <span v-if="!setting.user_editable" class="sw-chip bg-sunken text-muted">
            <Icon name="lock" :size="11" />
            只读
          </span>
          <span v-if="setting.requires_restart" class="sw-chip bg-warning-soft text-warning">
            <Icon name="refresh" :size="11" />
            需重启生效
          </span>
        </label>

        <p v-if="setting.help" :id="hintId" class="mt-1 text-xs leading-relaxed text-muted">
          {{ setting.help }}
        </p>

        <p class="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
          <code class="rounded bg-sunken px-1.5 py-0.5 font-mono text-[11px] text-content">
            {{ setting.key }}
          </code>
          <span v-if="ruleHint" class="inline-flex items-center gap-1">
            <Icon name="info" :size="12" />
            {{ ruleHint }}
          </span>
        </p>

        <p v-if="setting.depends_on" class="mt-1 text-xs text-muted">
          仅当
          <code class="rounded bg-sunken px-1 font-mono text-[11px]">{{ setting.depends_on }}</code>
          开启时才生效
        </p>

        <p v-if="!setting.user_editable" class="mt-1 text-xs text-muted">运行时状态，只读</p>
        <p v-if="setting.requires_restart" class="mt-1 text-xs text-warning">
          改动后需重启服务才生效
        </p>
      </div>

      <!-- 右列：控件 -->
      <div class="min-w-0">
        <!-- bool：开关（保留 checkbox 语义 + @change，只加 role="switch"） -->
        <label
          v-if="setting.type === 'bool'"
          class="flex w-fit items-center gap-3"
          :class="setting.user_editable ? 'cursor-pointer' : 'cursor-not-allowed opacity-60'"
        >
          <input
            :id="inputId"
            type="checkbox"
            role="switch"
            class="peer sr-only"
            :checked="checked"
            :disabled="!setting.user_editable"
            :aria-checked="checked"
            :aria-describedby="describedBy"
            @change="onBool"
          />
          <span
            class="flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition duration-150
                   peer-focus-visible:ring-2 peer-focus-visible:ring-brand/40
                   peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-elevated"
            :class="checked ? 'bg-brand' : 'bg-border-strong'"
            aria-hidden="true"
          >
            <span
              class="h-4 w-4 rounded-full bg-elevated shadow-card transition-transform duration-150"
              :class="checked ? 'translate-x-4' : 'translate-x-0'"
            />
          </span>
          <span class="text-sm font-medium" :class="checked ? 'text-content' : 'text-muted'">
            {{ checked ? '开启' : '关闭' }}
          </span>
        </label>

        <!-- enum：下拉 -->
        <select
          v-else-if="setting.type === 'enum'"
          :id="inputId"
          :value="text"
          :disabled="!setting.user_editable"
          :aria-invalid="error ? 'true' : undefined"
          :aria-describedby="describedBy"
          class="sw-input"
          :class="errorClass"
          @change="onSelect"
        >
          <option v-for="choice in setting.choices" :key="choice" :value="choice">
            {{ choice }}
          </option>
        </select>

        <!-- json：多行 -->
        <textarea
          v-else-if="setting.type === 'json'"
          :id="inputId"
          :value="text"
          rows="4"
          spellcheck="false"
          :disabled="!setting.user_editable"
          :aria-invalid="error ? 'true' : undefined"
          :aria-describedby="describedBy"
          class="sw-input font-mono text-xs leading-relaxed"
          :class="errorClass"
          @input="onText"
        />

        <!-- time：原生时间选择器（HH:MM） -->
        <input
          v-else-if="setting.type === 'time'"
          :id="inputId"
          type="time"
          :value="text"
          :disabled="!setting.user_editable"
          :aria-invalid="error ? 'true' : undefined"
          :aria-describedby="describedBy"
          class="sw-input tabular"
          :class="errorClass"
          @input="onText"
        />

        <!-- int / float / duration：数字 + 单位 -->
        <div
          v-else-if="['int', 'float', 'duration'].includes(setting.type)"
          class="flex items-center gap-2"
        >
          <input
            :id="inputId"
            type="number"
            :step="setting.type === 'float' ? '0.01' : '1'"
            :value="text"
            :disabled="!setting.user_editable"
            :aria-invalid="error ? 'true' : undefined"
            :aria-describedby="describedBy"
            class="sw-input tabular"
            :class="errorClass"
            @input="onText"
          />
          <span v-if="unit" class="shrink-0 text-xs text-muted">{{ unit }}</span>
        </div>

        <!-- secret：密码框；未改动时占位提示保持原值 -->
        <div v-else-if="setting.type === 'secret'" class="space-y-1.5">
          <div class="relative">
            <input
              :id="inputId"
              :type="revealed ? 'text' : 'password'"
              :value="text"
              :disabled="!setting.user_editable"
              :placeholder="secretDirty ? '' : '留空 / 保持原值'"
              autocomplete="new-password"
              :aria-invalid="error ? 'true' : undefined"
              :aria-describedby="describedBy"
              class="sw-input pr-11 font-mono text-xs"
              :class="errorClass"
              @input="onText"
            />
            <button
              type="button"
              class="absolute inset-y-0 right-0 flex w-11 items-center justify-center text-muted
                     transition hover:text-content"
              :aria-pressed="revealed"
              :aria-label="revealed ? '隐藏当前显示的凭据串' : '显示当前显示的凭据串'"
              @click="revealed = !revealed"
            >
              <Icon :name="revealed ? 'eyeOff' : 'eye'" :size="16" />
            </button>
          </div>

          <p class="flex flex-wrap items-center gap-2 text-xs text-muted">
            <span
              class="sw-chip"
              :class="secretDirty ? 'bg-warning-soft text-warning' : 'bg-sunken text-muted'"
            >
              <Icon :name="secretDirty ? 'key' : 'lock'" :size="11" />
              {{ secretDirty ? '已修改' : '未改动' }}
            </span>
            <span>
              后端只回显脱敏串；<strong class="text-content">只有改动过这一项才会提交</strong>。
            </span>
          </p>
        </div>

        <!-- str / url / 兜底 -->
        <input
          v-else
          :id="inputId"
          :type="setting.type === 'url' ? 'url' : 'text'"
          :value="text"
          :disabled="!setting.user_editable"
          :aria-invalid="error ? 'true' : undefined"
          :aria-describedby="describedBy"
          class="sw-input"
          :class="errorClass"
          @input="onText"
        />

        <p v-if="error" :id="errorId" class="sw-error" role="alert">{{ error }}</p>
      </div>
    </div>
  </div>
</template>
