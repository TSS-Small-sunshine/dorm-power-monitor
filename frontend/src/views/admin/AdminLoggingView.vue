<script setup lang="ts">
/**
 * 日志设置（5.8 / Q20）
 *
 * 页面结构 = 「现在是什么样」+「改成什么样」
 * ========================================
 *
 * * **实时状态**（上半）：全局级别、**每个类别实际生效的级别**、日志格式、
 *   文件路径与保留天数、已注册脱敏的 secret 数量 —— 来自
 *   ``GET /api/admin/logging``（后端把「全局 + 模块级覆盖」算成一张表）。
 * * **改设置**（下半）：复用配置中心的 ``ConfigField`` 渲染「日志」分组的
 *   配置项（级别 / 覆盖 / 格式 / 写文件 / 保留天数），保存走同一套 PUT。
 *
 * 为什么要有上半部分：用户改 ``log_overrides`` 时最容易犯的错是「以为生效了
 * 其实没有」（类别名拼错、或被子项压住）。表里直接写「scrape = DEBUG」，
 * 一眼就能核对。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { ConfigSetting, LoggingStatePayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import ConfigField from '@/components/config/ConfigField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const status = ref<LoggingStatePayload | null>(null)
const settings = ref<ConfigSetting[]>([])
const values = ref<Record<string, unknown>>({})
const edited = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})

const loading = ref(true)
const saving = ref(false)
const fatal = ref('')
const notice = ref('')

/** 日志分组的键（从 schema 的 group 字段取，不写死配置键名） */
const LOG_GROUP = '日志'

const dirty = computed(() => Object.keys(edited.value).length)

/** 被模块级覆盖调整过的类别（表里高亮） */
const overridden = computed(() => new Set(Object.keys(status.value?.overrides ?? {})))

function valueOf(key: string): unknown {
  return key in edited.value ? edited.value[key] : values.value[key]
}

function onEdit(key: string, value: unknown): void {
  edited.value = { ...edited.value, [key]: value }
  if (errors.value[key]) {
    const next = { ...errors.value }
    delete next[key]
    errors.value = next
  }
}

async function load(): Promise<void> {
  loading.value = true
  fatal.value = ''
  try {
    const [schema, current, state] = await Promise.all([
      api.configSchema(),
      api.configValues(),
      api.loggingState(),
    ])
    settings.value = (
      schema.groups.find((group) => group.key === LOG_GROUP)?.settings ?? []
    ).filter((setting) => setting.user_editable)
    values.value = current.values
    status.value = state
    edited.value = {}
    errors.value = {}
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!dirty.value || saving.value) return
  saving.value = true
  fatal.value = ''
  notice.value = ''
  errors.value = {}
  try {
    const result = await api.configUpdate(edited.value)
    errors.value = result.errors ?? {}
    const failed = Object.keys(errors.value).length
    if (result.applied.length) {
      notice.value =
        `已保存 ${result.applied.length} 项${failed ? `，${failed} 项未通过校验` : ''}` +
        '（日志设置立即生效，无需重启）'
      const rest: Record<string, unknown> = {}
      for (const key of Object.keys(edited.value)) {
        if (!result.applied.includes(key)) rest[key] = edited.value[key]
      }
      edited.value = rest
      await load()
    }
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <p v-if="fatal" class="rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ fatal }}
    </p>
    <p
      v-if="notice"
      class="rounded-control bg-success/10 px-3 py-2 text-sm text-success"
      role="status"
    >
      {{ notice }}
    </p>

    <p v-if="loading" class="py-10 text-center text-sm text-muted">正在加载…</p>

    <template v-else>
      <!-- 实时状态 -->
      <section class="sw-card p-4">
        <h2 class="mb-3 text-sm font-semibold text-content">当前状态</h2>

        <dl class="grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
          <div>
            <dt class="text-xs text-muted">全局级别</dt>
            <dd class="font-medium text-content">{{ status?.global_level }}</dd>
          </div>
          <div>
            <dt class="text-xs text-muted">日志格式</dt>
            <dd class="font-medium text-content">{{ status?.format }}</dd>
          </div>
          <div>
            <dt class="text-xs text-muted">写日志文件</dt>
            <dd class="font-medium text-content">
              {{ status?.file.enabled ? `是（保留 ${status?.file.retention_days} 天）` : '否' }}
            </dd>
          </div>
          <div>
            <dt class="text-xs text-muted">已注册脱敏的凭据</dt>
            <dd class="font-medium text-content">{{ status?.masked_secrets }} 个</dd>
          </div>
        </dl>

        <p v-if="status?.file.path" class="mt-2 text-xs text-muted">
          文件：<code class="rounded bg-surface px-1">{{ status.file.path }}</code>
        </p>

        <!-- 每个类别实际生效的级别（「实时状态」的核心） -->
        <h3 class="mb-2 mt-4 text-xs font-semibold text-content">各模块实际生效的级别</h3>
        <div class="grid grid-cols-2 gap-2 md:grid-cols-4">
          <div
            v-for="category in status?.categories ?? []"
            :key="category"
            class="flex items-center justify-between rounded-control bg-surface px-3 py-2 text-xs"
          >
            <code class="text-content">{{ category }}</code>
            <span class="font-medium" :class="overridden.has(category) ? 'text-brand' : 'text-muted'">
              {{ status?.effective[category] }}
              <span v-if="overridden.has(category)" title="被模块级覆盖">*</span>
            </span>
          </div>
        </div>
        <p class="mt-2 text-xs text-muted">
          带 <span class="text-brand">*</span> 的类别被「模块级级别覆盖」单独调整过；其余跟随全局级别。
        </p>
      </section>

      <!-- 改设置 -->
      <section class="sw-card p-4">
        <div class="mb-2 flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 class="text-sm font-semibold text-content">日志设置</h2>
            <p class="text-xs text-muted">
              级别：TRACE &lt; DEBUG &lt; INFO &lt; NOTICE &lt; WARNING &lt; ERROR &lt; CRITICAL
            </p>
          </div>
          <BaseButton :loading="saving" :disabled="!dirty" @click="save">
            保存{{ dirty ? `（${dirty}）` : '' }}
          </BaseButton>
        </div>

        <ConfigField
          v-for="setting in settings"
          :key="setting.key"
          :setting="setting"
          :value="valueOf(setting.key)"
          :error="errors[setting.key]"
          @update:model-value="(v) => onEdit(setting.key, v)"
        />

        <p class="mt-3 text-xs text-muted">
          🔒 日志里的 secret（openid / token / 密码）由日志系统**自动脱敏**，
          任何级别都不会出现明文。
        </p>
      </section>
    </template>
  </div>
</template>
