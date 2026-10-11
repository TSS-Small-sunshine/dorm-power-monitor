<script setup lang="ts">
/**
 * 功能开关（5.7 / Q16 / UI 改版 v2）—— 18 个开关 + **实时状态**
 *
 * 与「配置中心」的区别
 * ====================
 *
 * 配置中心按注册表分组渲染**所有** 94 项；这一页只回答一个问题：
 * **「现在会不会推送、不会的话为什么」**。所以数据来自
 * ``GET /api/admin/flags``（后端用 ``flags.py`` 的父子关系 + ``why_suppressed``
 * 算出来的运行时判断），而不是前端自己猜。
 *
 * 三条语义（页面上也写清楚了，否则用户会以为「关了 = 坏了」）
 * ======================================================
 *
 * 1. **总开关关闭 → 分项开关被忽略**（不必逐个关 8 个层）
 * 2. **关闭 = 静默降级**：不发送、记一条 NOTICE 日志，**不报错**
 * 3. **静默时段**只压 L1/L2；日报/周报/月报是用户主动订阅的，照发
 *
 * v2 只改**展示层**（``api.flagsState()`` / ``api.configUpdate()`` 的调用与参数一字未动）
 * ============================================================================
 *
 * * **父子抑制关系看得见**：旧版只把 ``why_suppressed`` 的原因印成一行小字，
 *   父开关关掉时子项看上去和「自己关了」没区别。现在子项会明确打上
 *   **「已抑制」** 胶囊并写明是哪一级、为什么 —— 而且**不隐藏**，
 *   用户才知道「为什么改了没用」。
 * * **分组卡片化 + 序号**：总开关 → 告警层 → 命令，三组的边界一眼可见。
 * * **开关从裸 checkbox 换成 ``role="switch"`` + 可见文字**：
 *   旧版只有一个 4px 的方框，开/关全靠旁边一个「开 / 关」小字。
 * * **提示条统一走 ``Notice``**：静默时段那条的月亮 emoji 换成 ``Icon name="moon"``
 *   （emoji 的字形由系统字体决定，两个平台长得不一样）。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { FlagInfo, FlagKind, FlagsStatePayload } from '@/api/types'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const state = ref<FlagsStatePayload | null>(null)
const loading = ref(true)
const busy = ref('')
const fatal = ref('')
const notice = ref('')

/** ``key → 抑制原因``（层开关用；有原因说明「现在推不出去」） */
const reasons = computed(() => {
  const map: Record<string, string> = {}
  for (const item of state.value?.suppressed ?? []) map[item.key] = item.reason
  return map
})

/** ``key → 此刻是否生效``（用来判断子项是不是被父开关压住） */
const enabledByKey = computed(() => {
  const map: Record<string, boolean> = {}
  for (const flag of state.value?.flags ?? []) map[flag.key] = flag.enabled
  return map
})

/** 列表里的一行 = 开关 + 展示用的状态胶囊与中文原因 */
interface FlagRow {
  key: string
  label: string
  kind: FlagKind
  parent: string | null
  enabled: boolean
  /** 非空 = 现在推不出去，值是中文原因 */
  suppression: string
  statusLabel: string
  statusTone: string
  statusIcon: string
}

/**
 * 按 ``kind`` 分好组的行。
 *
 * 抑制原因优先用后端给的 ``why_suppressed``；后端没给（例如命令类开关的
 * 父开关被关掉）时，用「父开关此刻是否生效」兜一个中文原因 ——
 * 纯展示推导，不改变任何开关的取值与提交。
 */
const rowsByKind = computed<Record<string, FlagRow[]>>(() => {
  const map: Record<string, FlagRow[]> = {}
  for (const flag of state.value?.flags ?? []) {
    const suppression =
      reasons.value[flag.key] ??
      (flag.parent && enabledByKey.value[flag.parent] === false
        ? `上级开关 ${flag.parent} 已关闭 —— 本项被忽略，改它不会有任何效果`
        : '')
    const status = suppression
      ? { label: '已抑制', tone: 'bg-warning-soft text-warning', icon: 'alert' }
      : flag.enabled
        ? { label: '生效中', tone: 'bg-success-soft text-success', icon: 'check' }
        : { label: '已关闭', tone: 'bg-sunken text-muted', icon: 'x' }
    map[flag.kind] = [
      ...(map[flag.kind] ?? []),
      {
        ...flag,
        suppression,
        statusLabel: status.label,
        statusTone: status.tone,
        statusIcon: status.icon,
      },
    ]
  }
  return map
})

async function load(): Promise<void> {
  loading.value = true
  fatal.value = ''
  try {
    state.value = await api.flagsState()
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/** 切换一个开关（走配置中心的 PUT，逐键独立校验） */
async function toggle(flag: FlagInfo): Promise<void> {
  if (busy.value) return
  busy.value = flag.key
  fatal.value = ''
  notice.value = ''
  try {
    const result = await api.configUpdate({ [flag.key]: !flag.enabled })
    if (result.errors[flag.key]) {
      fatal.value = result.errors[flag.key] ?? '保存失败'
      return
    }
    notice.value = `${flag.label}：已${flag.enabled ? '关闭' : '开启'}`
    await load() // 重新拉状态（父开关会影响子项显示）
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = ''
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>
    <Notice v-if="notice" tone="success" aria-live="polite">{{ notice }}</Notice>
    <Notice v-if="state?.quiet_hours" tone="warning" icon="moon" aria-live="polite">
      现在处于<strong>静默时段</strong> —— L1 低电与 L2 摘要不会发送（日报/周报/月报照发）。
    </Notice>

    <section class="sw-card p-4">
      <h2 class="sw-section-title">三条语义</h2>
      <ul class="mt-2 space-y-1.5 text-xs leading-relaxed text-muted">
        <li class="flex gap-2">
          <Icon name="power" :size="14" class="mt-0.5 shrink-0" />
          <span>
            总开关关闭 → 它下面的分项开关被<strong class="text-content">忽略</strong>（不必逐个关）
          </span>
        </li>
        <li class="flex gap-2">
          <Icon name="info" :size="14" class="mt-0.5 shrink-0" />
          <span>
            关闭 = <strong class="text-content">静默降级</strong>：不发送、记一条 NOTICE 日志，
            不会报错刷屏
          </span>
        </li>
        <li class="flex gap-2">
          <Icon name="alert" :size="14" class="mt-0.5 shrink-0" />
          <span>
            被抑制的层会在下面标出<strong class="text-content">具体原因</strong>
            （总开关 / 分项开关 / 静默时段）
          </span>
        </li>
      </ul>
    </section>

    <!-- 首屏骨架 -->
    <div v-if="loading" class="space-y-4" aria-busy="true" aria-live="polite">
      <div v-for="n in 3" :key="n" class="sw-card p-4">
        <div class="sw-skeleton h-4 w-24" />
        <div class="mt-4 space-y-4">
          <div v-for="m in 3" :key="m" class="flex items-center justify-between gap-3">
            <div class="min-w-0 flex-1">
              <div class="sw-skeleton h-3.5 w-40" />
              <div class="sw-skeleton mt-2 h-3 w-56" />
            </div>
            <div class="sw-skeleton h-5 w-9 shrink-0" />
          </div>
        </div>
      </div>
      <span class="sr-only">正在加载功能开关…</span>
    </div>

    <template v-else>
      <section
        v-for="(group, index) in state?.groups ?? []"
        :key="group.kind"
        class="sw-card overflow-hidden"
      >
        <header
          class="flex flex-wrap items-center justify-between gap-2 border-b border-border
                 bg-sunken/60 px-4 py-3"
        >
          <div class="flex min-w-0 items-center gap-2">
            <span
              class="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-elevated
                     text-xs font-semibold tabular text-muted"
              aria-hidden="true"
            >
              {{ index + 1 }}
            </span>
            <h2 class="sw-section-title truncate">{{ group.title }}</h2>
          </div>
          <span class="sw-chip bg-elevated text-muted">
            <span class="tabular">{{ (rowsByKind[group.kind] ?? []).length }}</span>
            项
          </span>
        </header>

        <ul class="divide-y divide-border">
          <li
            v-for="row in rowsByKind[group.kind] ?? []"
            :key="row.key"
            class="flex flex-wrap items-start justify-between gap-3 px-4 py-3"
          >
            <div class="min-w-0 flex-1">
              <div class="flex flex-wrap items-center gap-2">
                <p class="text-sm font-medium text-content">{{ row.label }}</p>
                <span class="sw-chip" :class="row.statusTone">
                  <Icon :name="row.statusIcon" :size="11" />
                  {{ row.statusLabel }}
                </span>
              </div>

              <p class="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted">
                <code class="rounded bg-sunken px-1.5 py-0.5 font-mono text-[11px]">
                  {{ row.key }}
                </code>
                <span v-if="row.parent" class="inline-flex items-center gap-1">
                  <Icon name="chevronRight" :size="12" />
                  上级
                  <code class="rounded bg-sunken px-1 font-mono text-[11px]">{{ row.parent }}</code>
                </span>
              </p>

              <p v-if="row.suppression" class="mt-1 flex items-start gap-1.5 text-xs text-warning">
                <Icon name="alert" :size="12" class="mt-0.5 shrink-0" />
                <span>{{ row.suppression }}</span>
              </p>
            </div>

            <label
              class="flex shrink-0 items-center gap-2"
              :class="[
                busy && busy !== row.key ? 'opacity-60' : '',
                busy === row.key ? 'cursor-wait' : 'cursor-pointer',
              ]"
            >
              <input
                type="checkbox"
                role="switch"
                class="peer sr-only"
                :checked="row.enabled"
                :disabled="busy === row.key"
                :aria-checked="row.enabled"
                :aria-label="`${row.label}（${row.key}）`"
                @change="toggle(row)"
              />
              <span
                class="flex h-5 w-9 shrink-0 items-center rounded-full p-0.5 transition duration-150
                       peer-focus-visible:ring-2 peer-focus-visible:ring-brand/40
                       peer-focus-visible:ring-offset-2 peer-focus-visible:ring-offset-elevated"
                :class="row.enabled ? 'bg-brand' : 'bg-border-strong'"
                aria-hidden="true"
              >
                <span
                  class="h-4 w-4 rounded-full bg-elevated shadow-card transition-transform duration-150"
                  :class="row.enabled ? 'translate-x-4' : 'translate-x-0'"
                />
              </span>
              <span
                class="w-8 text-xs font-medium"
                :class="row.enabled ? 'text-success' : 'text-muted'"
              >
                {{ row.enabled ? '开' : '关' }}
              </span>
            </label>
          </li>
        </ul>
      </section>
    </template>

    <p class="text-xs text-muted">
      开关之外的告警参数（阈值、推送时间、静默时段起止、命令白名单等）在
      <RouterLink to="/admin/config" class="text-brand hover:underline">配置中心</RouterLink>
      里改。
    </p>
  </div>
</template>
