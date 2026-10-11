<script setup lang="ts">
/**
 * 日志设置（5.8 / Q20 / UI 改版 v2）
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
 *
 * v2 只改**展示层**（``api.configUpdate()`` / ``api.loggingState()`` 的调用一字未动）
 * ==========================================================================
 *
 * * **7 级 × 8 类画成真正的矩阵**：旧版把 8 个类别排成 4 列小卡片，
 *   每张只写「scrape DEBUG」，看不出「它离全局级别差几档」「还有哪些档可选」。
 *   现在行 = 类别、列 = 级别，生效的那一格高亮，一眼看出谁被覆盖、覆盖到哪一档。
 * * **摘要改用 ``StatCard``**：4 个关键数字（全局级别 / 格式 / 文件与保留 / 脱敏条数）
 *   带图标、骨架屏与等宽数字。
 * * **锁标记换成 ``Icon name="lock"``**：保留「自动脱敏」这句技术说明。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { ConfigSetting, LoggingStatePayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import StatCard from '@/components/StatCard.vue'
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

/** 7 个级别（按数值升序，后端给的顺序）—— 矩阵的列 */
const levels = computed(() => status.value?.levels ?? [])

/** 8 个业务类别 —— 矩阵的行 */
const categories = computed(() => status.value?.categories ?? [])

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
    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>
    <Notice v-if="notice" tone="success" aria-live="polite">{{ notice }}</Notice>

    <!-- 首屏骨架 -->
    <div v-if="loading" class="space-y-4" aria-busy="true" aria-live="polite">
      <div class="sw-card p-4">
        <div class="sw-skeleton h-4 w-24" />
        <div class="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4">
          <div v-for="n in 4" :key="n" class="sw-skeleton h-20 w-full" />
        </div>
      </div>
      <div class="sw-card p-4">
        <div class="sw-skeleton h-4 w-32" />
        <div class="sw-skeleton mt-4 h-40 w-full" />
      </div>
      <span class="sr-only">正在加载日志状态…</span>
    </div>

    <template v-else>
      <!-- 实时状态 -->
      <section class="sw-card overflow-hidden">
        <header class="border-b border-border bg-sunken/60 px-4 py-3">
          <h2 class="sw-section-title">当前状态</h2>
          <p class="mt-0.5 text-xs text-muted">
            来自运行中的日志系统；下半部分改完立即生效，无需重启
          </p>
        </header>

        <div class="grid grid-cols-2 gap-3 p-4 md:grid-cols-4">
          <StatCard label="全局级别" :value="status?.global_level ?? '—'" icon="sliders" />
          <StatCard label="日志格式" :value="status?.format ?? '—'" icon="file" />
          <StatCard
            label="写日志文件"
            :value="status?.file.enabled ? '开启' : '关闭'"
            icon="save"
            :hint="
              status?.file.enabled ? `保留 ${status?.file.retention_days} 天` : '只输出到标准输出'
            "
          />
          <StatCard
            label="已注册脱敏的凭据"
            :value="String(status?.masked_secrets ?? 0)"
            unit="个"
            icon="lock"
            hint="日志里出现这些值会被替换成掩码"
          />
        </div>

        <p v-if="status?.file.path" class="flex flex-wrap items-center gap-2 px-4 pb-3 text-xs text-muted">
          <Icon name="database" :size="13" class="shrink-0" />
          文件路径：
          <code class="rounded bg-sunken px-1.5 py-0.5 font-mono text-[11px] text-content">
            {{ status?.file.path }}
          </code>
        </p>

        <!-- 7 级 × 8 类：每个类别实际生效的级别（「实时状态」的核心） -->
        <div class="border-t border-border px-4 py-3">
          <div class="flex flex-wrap items-baseline justify-between gap-2">
            <h3 class="text-xs font-semibold text-content">各模块实际生效的级别</h3>
            <p class="text-xs text-muted">
              行 = 业务类别，列 = 日志级别；高亮的那一格就是它此刻生效的级别
            </p>
          </div>

          <div class="mt-2 overflow-x-auto">
            <table class="w-full min-w-[34rem] text-xs">
              <caption class="sr-only">
                每个日志类别实际生效的级别（7 个级别 × 8 个类别）；「模块级覆盖」表示该类别被
                模块级级别覆盖单独调整过，其余跟随全局级别
              </caption>
              <thead>
                <tr class="border-b border-border text-muted">
                  <th scope="col" class="sticky left-0 bg-elevated px-2 py-2 text-left font-medium">
                    类别
                  </th>
                  <th
                    v-for="level in levels"
                    :key="level"
                    scope="col"
                    class="px-1 py-2 text-center font-medium"
                  >
                    {{ level }}
                  </th>
                  <th scope="col" class="px-2 py-2 text-left font-medium">来源</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="category in categories" :key="category" class="border-b border-border">
                  <th
                    scope="row"
                    class="sticky left-0 bg-elevated px-2 py-1.5 text-left font-mono font-normal text-content"
                  >
                    {{ category }}
                  </th>
                  <td v-for="level in levels" :key="level" class="px-1 py-1 text-center">
                    <span
                      v-if="status?.effective[category] === level"
                      class="inline-flex min-w-[3.25rem] items-center justify-center rounded-full
                             px-2 py-0.5 font-medium tabular"
                      :class="
                        overridden.has(category)
                          ? 'bg-brand text-brand-ink'
                          : 'bg-brand-soft text-brand'
                      "
                    >
                      {{ level }}
                    </span>
                    <span v-else class="text-border-strong/70" aria-hidden="true">·</span>
                  </td>
                  <td class="px-2 py-1.5">
                    <span
                      v-if="overridden.has(category)"
                      class="sw-chip bg-brand-soft text-brand"
                    >
                      <Icon name="filter" :size="11" />
                      模块级覆盖
                    </span>
                    <span v-else class="text-muted">跟随全局</span>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>

          <p class="mt-2 text-xs text-muted">
            标「模块级覆盖」的类别被「模块级级别覆盖」单独调整过；其余跟随全局级别
            （当前 <span class="font-medium text-content">{{ status?.global_level }}</span>）。
          </p>
        </div>
      </section>

      <!-- 改设置 -->
      <section class="sw-card overflow-hidden">
        <header
          class="flex flex-wrap items-center justify-between gap-3 border-b border-border
                 bg-sunken/60 px-4 py-3"
        >
          <div class="min-w-0">
            <h2 class="sw-section-title">日志设置</h2>
            <p class="mt-0.5 text-xs text-muted">
              级别：TRACE &lt; DEBUG &lt; INFO &lt; NOTICE &lt; WARNING &lt; ERROR &lt; CRITICAL
            </p>
          </div>
          <BaseButton :loading="saving" :disabled="!dirty" @click="save">
            <template #icon><Icon name="save" :size="14" /></template>
            保存{{ dirty ? `（${dirty}）` : '' }}
          </BaseButton>
        </header>

        <div class="px-4">
          <ConfigField
            v-for="setting in settings"
            :key="setting.key"
            :setting="setting"
            :value="valueOf(setting.key)"
            :error="errors[setting.key]"
            @update:model-value="(v) => onEdit(setting.key, v)"
          />
        </div>

        <p
          class="flex items-start gap-2 border-t border-border bg-sunken/40 px-4 py-3 text-xs
                 leading-relaxed text-muted"
        >
          <Icon name="lock" :size="13" class="mt-0.5 shrink-0" />
          <span>
            日志里的 secret（openid / token / 密码）由日志系统<strong class="text-content">
              自动脱敏</strong>，任何级别都不会出现明文。
          </span>
        </p>
      </section>
    </template>
  </div>
</template>
