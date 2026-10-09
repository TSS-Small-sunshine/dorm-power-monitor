<script setup lang="ts">
/**
 * 功能开关（5.7 / Q16）—— 18 个开关 + **实时状态**
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
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { FlagInfo, FlagsStatePayload } from '@/api/types'
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

function byKind(kind: string): FlagInfo[] {
  return (state.value?.flags ?? []).filter((flag) => flag.kind === kind)
}

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
    <p v-if="state?.quiet_hours" class="rounded-control bg-warning/10 px-3 py-2 text-sm text-warning">
      🌙 现在处于**静默时段** —— L1 低电与 L2 摘要不会发送（日报/周报/月报照发）。
    </p>

    <div class="sw-card p-4 text-xs text-muted">
      <p class="font-medium text-content">三条语义</p>
      <ul class="mt-1 space-y-0.5">
        <li>· 总开关关闭 → 它下面的分项开关被**忽略**（不必逐个关）</li>
        <li>· 关闭 = **静默降级**：不发送、记一条 NOTICE 日志，不会报错刷屏</li>
        <li>· 被抑制的层会在下面标出**具体原因**（总开关 / 分项开关 / 静默时段）</li>
      </ul>
    </div>

    <p v-if="loading" class="py-10 text-center text-sm text-muted">正在加载…</p>

    <section v-for="group in state?.groups ?? []" :key="group.kind" class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">{{ group.title }}</h2>

      <ul class="divide-y divide-border/60">
        <li v-for="flag in byKind(group.kind)" :key="flag.key" class="flex items-center justify-between gap-3 py-3">
          <div class="min-w-0">
            <p class="text-sm text-content">
              {{ flag.label }}
              <span v-if="flag.kind !== 'master'" class="ml-1 text-xs text-muted">
                · {{ flag.enabled ? '生效中' : '已关闭' }}
              </span>
            </p>
            <p class="text-xs text-muted">
              <code class="rounded bg-surface px-1">{{ flag.key }}</code>
              <span v-if="flag.parent"> · 受 <code>{{ flag.parent }}</code> 控制</span>
            </p>
            <p v-if="reasons[flag.key]" class="mt-0.5 text-xs text-warning">
              ⚠ {{ reasons[flag.key] }}
            </p>
          </div>

          <label class="flex shrink-0 cursor-pointer items-center gap-2">
            <input
              type="checkbox"
              class="h-4 w-4"
              :checked="flag.enabled"
              :disabled="busy === flag.key"
              :aria-label="`${flag.label}（${flag.key}）`"
              @change="toggle(flag)"
            />
            <span class="w-8 text-xs" :class="flag.enabled ? 'text-success' : 'text-muted'">
              {{ flag.enabled ? '开' : '关' }}
            </span>
          </label>
        </li>
      </ul>
    </section>

    <p class="text-xs text-muted">
      开关之外的告警参数（阈值、推送时间、静默时段起止、命令白名单等）在
      <RouterLink to="/admin/config" class="text-brand hover:underline">配置中心</RouterLink>
      里改。
    </p>
  </div>
</template>
