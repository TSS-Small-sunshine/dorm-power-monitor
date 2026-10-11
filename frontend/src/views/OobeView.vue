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
 *
 * v2 改动（**只有展示层**：state / advance / parse / verify 的调用一字未改）
 * =====================================================================
 *
 * * **步骤条从「一行彩色小字」升级成真正的进度指示**：编号圆点 + 连接线 +
 *   细进度条。旧版把 5 个步骤名平铺成文字，用户看不出「还剩几步」；而且
 *   它用 `bg-brand/10` 标当前步 —— 与「已完成」的绿色只差一点色相。
 *   现在：当前步是实心品牌色圆点，已完成是绿色对勾，未到是灰边。
 * * **每步一张卡（`header` / 正文 / `footer` 三段）**：导航按钮固定在卡片
 *   底部，不再随字段多少上下浮动；切步时卡片用 `:key` 重挂载，走一次
 *   `animate-fade-in-up`，让「换了一步」这件事看得见。
 * * **顶部条吸顶 + 毛玻璃**：字段多的步骤要滚，滚下去还得能点「稍后再配」。
 * * **`notice` 的色调按 `errors` 推导**：旧版把「有 N 项没通过校验」渲染成
 *   **绿色成功**提示 —— 那是这一步唯一需要用户动手的信号，不该是绿的。
 * * 学校接入那块：粘贴框换成 `BaseField`（与其它字段同一套标签/提示样式），
 *   解析结果用 `Notice`，`✅/❌` 换成 `<Icon>`。
 * * 加载态从一行「正在加载…」换成骨架屏（布局不会在数据到达时跳一下）。
 */
import { computed, onMounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError, api } from '@/api/client'
import type { OobeStatePayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import ConfigField from '@/components/config/ConfigField.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
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
  <div class="min-h-full bg-sunken">
    <header class="sw-glass sticky top-0 z-20 border-b border-border">
      <div class="mx-auto flex max-w-3xl items-center justify-between gap-3 px-4 py-3">
        <div class="flex min-w-0 items-center gap-3">
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-control
                   bg-brand-soft text-brand"
            aria-hidden="true"
          >
            <Icon name="zap" :size="18" />
          </span>
          <div class="min-w-0">
            <h1 class="truncate text-base font-bold leading-tight text-content">首次配置</h1>
            <p class="truncate text-xs text-muted">
              {{ auth.siteName }} · 第 {{ (state?.step ?? 0) + 1 }} / {{ state?.total ?? 5 }} 步
            </p>
          </div>
        </div>

        <div class="flex shrink-0 items-center gap-1.5">
          <ThemeToggle />
          <BaseButton variant="ghost" size="sm" @click="router.push('/')">
            <template #icon><Icon name="x" :size="14" /></template>
            稍后再配
          </BaseButton>
        </div>
      </div>
    </header>

    <main class="mx-auto max-w-3xl px-4 py-5">
      <Notice v-if="fatal" tone="danger" class="mb-3">{{ fatal }}</Notice>
      <!--
        校验失败时 `errors` 非空 —— 那才是「需要你动手」的信号，用 warning；
        其余（已跳过 / 解析结果 / 完成）是进展通报，用 info 就够。
      -->
      <Notice
        v-if="notice"
        :tone="Object.keys(errors).length ? 'warning' : 'info'"
        class="mb-3"
      >
        {{ notice }}
      </Notice>

      <!-- 进度：步骤条 + 细进度条。步骤名只在 ≥768 显示（窄屏放不下 5 个），
           窄屏靠下面卡片头部的「第 N 步 / 共 5 步」与标题辨认当前位置 -->
      <nav v-if="state" class="mb-4" aria-label="配置进度">
        <ol class="flex items-center gap-2">
          <li
            v-for="item in state.steps"
            :key="item.key"
            class="flex min-w-0 flex-1 items-center gap-2"
            :aria-current="item.index === state.step ? 'step' : undefined"
          >
            <span
              class="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border
                     text-xs font-medium"
              :class="
                item.index === state.step
                  ? 'border-brand bg-brand text-brand-ink'
                  : item.done
                    ? 'border-success/40 bg-success-soft text-success'
                    : 'border-border bg-elevated text-muted'
              "
            >
              <Icon v-if="item.done && item.index !== state.step" name="check" :size="14" />
              <template v-else>{{ item.index + 1 }}</template>
            </span>
            <span
              class="sr-only sm:not-sr-only sm:min-w-0 sm:truncate sm:text-xs sm:font-medium"
              :class="item.index === state.step ? 'sm:text-content' : 'sm:text-muted'"
            >
              {{ item.title }}
            </span>
            <span
              v-if="item.index < state.total - 1"
              class="h-px min-w-2 flex-1 bg-border"
              aria-hidden="true"
            />
          </li>
        </ol>

        <div
          class="mt-3 h-1 overflow-hidden rounded-full bg-border/60"
          role="progressbar"
          :aria-valuenow="state.step + 1"
          aria-valuemin="1"
          :aria-valuemax="state.total"
          :aria-label="`配置进度：第 ${state.step + 1} 步，共 ${state.total} 步`"
        >
          <div
            class="h-full rounded-full bg-brand transition-all duration-300"
            :style="{ width: `${((state.step + 1) / state.total) * 100}%` }"
          />
        </div>

        <!-- 换步时读屏也能跟上（进度条本身只是个数值，不会自己播报） -->
        <p class="sr-only" aria-live="polite">
          当前第 {{ state.step + 1 }} 步，共 {{ state.total }} 步：{{ step?.title ?? '' }}
        </p>
      </nav>

      <!-- 首屏骨架：把布局先占住，数据到达时不会整页跳 -->
      <div v-if="loading" class="sw-card p-5" aria-busy="true">
        <div class="sw-skeleton h-4 w-32" />
        <div class="sw-skeleton mt-3 h-3 w-64" />
        <div class="sw-skeleton mt-6 h-10 w-full" />
        <div class="sw-skeleton mt-3 h-10 w-full" />
        <span class="sr-only">正在加载配置向导…</span>
      </div>

      <section
        v-else-if="step"
        :key="step.index"
        class="sw-card animate-fade-in-up overflow-hidden"
        aria-labelledby="oobe-step-title"
      >
        <header class="border-b border-border px-5 py-4">
          <p class="text-xs font-medium text-brand">
            第 {{ step.index + 1 }} 步 / 共 {{ state?.total ?? 5 }} 步
          </p>
          <h2 id="oobe-step-title" class="mt-1 text-lg font-semibold text-content">
            {{ step.title }}
          </h2>
          <p class="mt-1 text-sm leading-relaxed text-muted">{{ step.description }}</p>
        </header>

        <div class="px-5 py-5">
          <!-- 学校接入：粘贴地址自动解析 + 连通验证 -->
          <div
            v-if="step.key === 'school'"
            class="mb-5 rounded-card border border-dashed border-border-strong bg-surface p-4"
          >
            <p class="mb-3 flex items-center gap-1.5 text-xs font-medium text-content">
              <Icon name="zap" :size="14" class="text-brand" />
              从学校页面自动填写（可选）
            </p>

            <!-- Enter 解析：keyup 从输入框冒泡到 BaseField 的根节点 -->
            <BaseField
              id="oobe-pasted-url"
              v-model="pastedUrl"
              label="粘贴学校 H5 页面地址"
              placeholder="https://<学校域名>/...?openid=...&roomId=..."
              hint="在微信里打开宿舍电费页面，复制地址栏的完整 URL 粘到这里。"
              @keyup.enter="parsePasted"
            />

            <div class="mt-3 flex flex-wrap items-center gap-2">
              <BaseButton variant="ghost" size="sm" :loading="parsing" @click="parsePasted">
                <template #icon><Icon name="download" :size="14" /></template>
                解析地址
              </BaseButton>
              <BaseButton variant="ghost" size="sm" :loading="parsing" @click="verify">
                <template #icon><Icon name="activity" :size="14" /></template>
                测试连通
              </BaseButton>
            </div>

            <Notice
              v-if="verifyResult"
              class="mt-3"
              :tone="verifyResult.ok ? 'success' : 'danger'"
              :icon="verifyResult.ok ? 'check' : 'x'"
            >
              {{ verifyResult.text }}
            </Notice>
          </div>

          <!-- 本步字段：元数据全部来自配置注册表（与配置中心同一套控件） -->
          <div>
            <ConfigField
              v-for="setting in step.settings"
              :key="setting.key"
              :setting="setting"
              :value="valueOf(setting.key)"
              :error="errors[setting.key]"
              @update:model-value="(v) => onEdit(setting.key, v)"
            />
          </div>
        </div>

        <!-- 导航固定在卡片底部：按钮位置不随字段多少上下浮动 -->
        <footer
          class="flex flex-wrap items-center gap-2 border-t border-border bg-sunken/60 px-5 py-4"
        >
          <BaseButton
            variant="ghost"
            class="flex-1 sm:flex-none"
            :disabled="isFirst || busy"
            @click="move('prev')"
          >
            <template #icon><Icon name="arrowLeft" :size="15" /></template>
            上一步
          </BaseButton>
          <BaseButton class="flex-1 sm:flex-none" :loading="busy" @click="move('next')">
            <template #icon>
              <Icon v-if="!busy" :name="isLast ? 'check' : 'chevronRight'" :size="15" />
            </template>
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
          <p class="w-full text-xs leading-relaxed text-muted sm:ml-auto sm:w-auto">
            每步都可跳过；所有配置之后都能在「管理 → 配置中心」里改。
          </p>
        </footer>
      </section>
    </main>
  </div>
</template>
