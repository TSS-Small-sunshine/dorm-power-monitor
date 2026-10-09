<script setup lang="ts">
/**
 * 首次配置向导（5.3 / B4 / B5）—— **完全由 ``/api/oobe/state`` 驱动**
 *
 * 5 步：欢迎 → 学校接入 → 飞书群推送 → 飞书机器人 → 推送偏好 + 完成。
 *
 * 为什么只有两个端点（``state`` / ``advance``）
 * ==========================================
 *
 * legacy 有 10 个端点（save-state / next / prev / skip-step / validate-feishu /
 * validate-webhook / complete …）。重写把它们收敛成：
 *
 * * 前进 / 后退 / 跳过 → ``advance`` 的 ``direction`` + ``skip``
 * * 完成 → 走到最后一步即完成（不需要单独的 ``/complete``）
 * * 渠道测试 → 复用 ``/api/admin/config/test``（管理 → 连通测试）
 *
 * 表单元数据来自配置注册表（``current.settings``），所以这里复用配置中心的
 * ``ConfigField`` —— **同一套控件、同一套校验**，向导不是「另一套配置界面」。
 *
 * 每一步都可以跳过：跳过 = 只移动步号、不保存。走到最后一步即标记完成，
 * 之后所有配置仍能在「配置中心」里改。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, api } from '@/api/client'
import type { OobeStatePayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import ConfigField from '@/components/config/ConfigField.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const state = ref<OobeStatePayload | null>(null)
const edited = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})
const loading = ref(true)
const busy = ref(false)
const fatal = ref('')
const notice = ref('')

// 学校接入这步的便利工具：粘贴 URL 自动解析 / 连通验证
const pastedUrl = ref('')
const parsing = ref(false)
const verifyResult = ref<{ ok: boolean; text: string } | null>(null)

const step = computed(() => state.value?.current ?? null)
const isFirst = computed(() => (state.value?.step ?? 0) === 0)
const isLast = computed(() => (state.value?.step ?? 0) === (state.value?.total ?? 5) - 1)
/** 本步没有必填项 → 可以跳过 */
const skippable = computed(() => (step.value?.required.length ?? 0) === 0)

function valueOf(key: string): unknown {
  if (key in edited.value) return edited.value[key]
  return state.value?.values[key]
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
    state.value = await api.oobeState()
    edited.value = {}
    errors.value = {}
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

/** 保存本步并移动（``skip`` 时只移动、不保存） */
async function move(direction: 'next' | 'prev', skip = false): Promise<void> {
  if (busy.value || !step.value) return
  busy.value = true
  fatal.value = ''
  notice.value = ''
  try {
    const payload = {
      direction,
      skip,
      values: skip ? undefined : { ...edited.value },
    }
    const result = await api.oobeAdvance(payload)
    state.value = result
    errors.value = result.saved?.errors ?? {}
    edited.value = {} // 已提交的值以服务端为准

    const failed = Object.keys(errors.value).length
    if (failed) {
      notice.value = `有 ${failed} 项没通过校验，请修正后重试`
      return // 校验失败就停在原地
    }
    if (skip) notice.value = '已跳过这一步（之后可在配置中心里改）'

    if (isLast.value && direction === 'next' && result.completed) {
      notice.value = '配置完成！正在进入仪表盘…'
      const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
      await router.replace(target)
    }
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    busy.value = false
  }
}

/** 学校接入：粘贴 H5 地址 → 解析出 openid / roomId 并填进表单 */
async function parsePasted(): Promise<void> {
  if (!pastedUrl.value.trim()) return
  parsing.value = true
  fatal.value = ''
  verifyResult.value = null
  try {
    const parsed = await api.setupParseUrl(pastedUrl.value.trim())
    if (parsed.base_url) onEdit('dorm_base_url', parsed.base_url)
    if (parsed.openid) onEdit('dorm_openid', parsed.openid)
    if (parsed.room_id) onEdit('dorm_room_id', parsed.room_id)
    if (typeof parsed.eqprice === 'number') onEdit('eqprice', parsed.eqprice)
    notice.value = `已解析：openid ${parsed.openid ? '✓' : '✗'}、房间 ${parsed.room_id || '未发现'}${
      parsed.room_label ? `（${parsed.room_label}）` : ''
    }`
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '解析失败'
  } finally {
    parsing.value = false
  }
}

/** 学校接入：真的连一次学校接口（不落库） */
async function verify(): Promise<void> {
  const openid = String(valueOf('dorm_openid') ?? '').trim()
  if (!openid) {
    verifyResult.value = { ok: false, text: '请先填 openid（或用上面的地址解析）' }
    return
  }
  parsing.value = true
  verifyResult.value = null
  try {
    const result = await api.setupVerify({
      openid,
      room_id: String(valueOf('dorm_room_id') ?? '').trim(),
      base_url: String(valueOf('dorm_base_url') ?? '').trim(),
    })
    verifyResult.value = result.ok
      ? {
          ok: true,
          text: `连接成功：房间 ${result.room_label || result.room_id}，剩余 ${
            result.remain ?? '—'
          } kW·h（抄表 ${result.read_time || '—'}）`,
        }
      : { ok: false, text: result.error || '验证失败' }
  } catch (err) {
    if (auth.handle(err)) return
    verifyResult.value = {
      ok: false,
      text: err instanceof ApiError ? err.message : '验证失败',
    }
  } finally {
    parsing.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="min-h-full bg-surface">
    <header class="border-b border-border bg-elevated">
      <div class="mx-auto flex max-w-3xl items-center justify-between gap-3 px-4 py-3">
        <div>
          <h1 class="text-base font-bold text-content">首次配置</h1>
          <p class="text-xs text-muted">
            {{ auth.siteName }} · 第 {{ (state?.step ?? 0) + 1 }} / {{ state?.total ?? 5 }} 步
          </p>
        </div>
        <div class="flex items-center gap-2">
          <ThemeToggle />
          <button
            type="button"
            class="rounded-control border border-border px-2.5 py-1.5 text-sm text-content transition hover:bg-surface"
            @click="router.push('/')"
          >
            稍后再配
          </button>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-3xl px-4 py-5">
      <p
        v-if="fatal"
        class="mb-3 rounded-control bg-danger/10 px-3 py-2 text-sm text-danger"
        role="alert"
      >
        {{ fatal }}
      </p>
      <p
        v-if="notice"
        class="mb-3 rounded-control bg-success/10 px-3 py-2 text-sm text-success"
        role="status"
      >
        {{ notice }}
      </p>

      <!-- 步骤条 -->
      <ol class="mb-4 flex flex-wrap gap-2" aria-label="配置步骤">
        <li
          v-for="item in state?.steps ?? []"
          :key="item.key"
          class="flex items-center gap-1.5 rounded-control px-2.5 py-1 text-xs"
          :class="
            item.index === state?.step
              ? 'bg-brand/10 text-brand'
              : item.done
                ? 'text-success'
                : 'text-muted'
          "
          :aria-current="item.index === state?.step ? 'step' : undefined"
        >
          <span aria-hidden="true">{{ item.done ? '✓' : item.index + 1 }}</span>
          {{ item.title }}
        </li>
      </ol>

      <p v-if="loading" class="py-10 text-center text-sm text-muted">正在加载…</p>

      <section v-else-if="step" class="sw-card p-5">
        <h2 class="text-base font-semibold text-content">{{ step.title }}</h2>
        <p class="mt-1 text-sm text-muted">{{ step.description }}</p>

        <!-- 学校接入：粘贴地址自动解析 + 连通验证 -->
        <div v-if="step.key === 'school'" class="mt-4 space-y-3 rounded-card bg-surface p-3">
          <label class="block">
            <span class="sw-label">粘贴学校 H5 页面地址（可选，自动填下面几项）</span>
            <span class="flex gap-2">
              <input
                v-model="pastedUrl"
                class="sw-input"
                placeholder="https://<学校域名>/...?openid=...&roomId=..."
                @keyup.enter="parsePasted"
              />
              <BaseButton variant="ghost" :loading="parsing" @click="parsePasted">解析</BaseButton>
            </span>
            <span class="sw-hint">
              在微信里打开宿舍电费页面，复制地址栏的完整 URL 粘到这里。
            </span>
          </label>

          <div class="flex items-center gap-2">
            <BaseButton variant="ghost" :loading="parsing" @click="verify">测试连通</BaseButton>
            <p
              v-if="verifyResult"
              class="text-xs"
              :class="verifyResult.ok ? 'text-success' : 'text-danger'"
              role="status"
            >
              {{ verifyResult.ok ? '✅' : '❌' }} {{ verifyResult.text }}
            </p>
          </div>
        </div>

        <!-- 本步字段：元数据全部来自配置注册表（与配置中心同一套控件） -->
        <div class="mt-3">
          <ConfigField
            v-for="setting in step.settings"
            :key="setting.key"
            :setting="setting"
            :value="valueOf(setting.key)"
            :error="errors[setting.key]"
            @update:model-value="(v) => onEdit(setting.key, v)"
          />
        </div>

        <!-- 导航 -->
        <div class="mt-5 flex flex-wrap items-center gap-2">
          <BaseButton variant="ghost" :disabled="isFirst || busy" @click="move('prev')">
            上一步
          </BaseButton>
          <BaseButton :loading="busy" @click="move('next')">
            {{ isLast ? '完成' : '下一步' }}
          </BaseButton>
          <BaseButton
            v-if="skippable && !isLast"
            variant="ghost"
            :disabled="busy"
            @click="move('next', true)"
          >
            跳过这步
          </BaseButton>
          <p class="ml-auto text-xs text-muted">
            每步都可跳过；所有配置之后都能在「管理 → 配置中心」里改。
          </p>
        </div>
      </section>
    </main>
  </div>
</template>
