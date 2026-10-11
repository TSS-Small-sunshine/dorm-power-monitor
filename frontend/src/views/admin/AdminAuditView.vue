<script setup lang="ts">
/**
 * 审计日志（5.5 / UI 改版 v2）—— 谁在什么时候改了什么
 *
 * Q17 要求「配置类操作必须留痕」。这个页面就是那条要求的可见部分：
 * 改配置 / 导入 / 重置 / 建号 / 删号 / 登录失败都会出现在这里。
 *
 * 审计内容**绝不含 secret 与 openid**（后端写入前已脱敏）——
 * 所以这个页面可以放心展示给管理员，也方便复制给别人排障。
 *
 * v2 只改**展示层**（``api.audit(limit, action)`` 的调用与 ``details()`` 的截断规则一字未动）
 * ==================================================================================
 *
 * * **筛选条是真的表单**：两个输入各自带 ``sw-label``，回车即可查询，
 *   右侧实时显示条数 —— 旧版用 ``text-xs`` 当标签，输入框与标签基线对不齐。
 * * **表格能读长列表**：表头吸在滚动容器顶部、时间列用等宽数字对齐、
 *   详情列等宽字体 + ``title`` 悬浮看全文（旧版超过 120 字符截断后没有任何办法看全）。
 * * **空列表用 ``EmptyState``**：旧版只有一行「没有匹配的记录」，
 *   看不出是「筛掉了」还是「压根没有」。
 * * 图标全部走 ``Icon``：这一页只该有一个视觉语言。
 */
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { AuditEntry } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import EmptyState from '@/components/EmptyState.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const entries = ref<AuditEntry[]>([])
const loading = ref(true)
const fatal = ref('')
const action = ref('')
const limit = ref(100)

async function load(): Promise<void> {
  loading.value = true
  fatal.value = ''
  try {
    entries.value = (await api.audit(limit.value, action.value.trim())).entries
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

function details(entry: AuditEntry): string {
  if (!entry.details) return '—'
  const text = JSON.stringify(entry.details)
  return text.length > 120 ? `${text.slice(0, 120)}…` : text
}

/** 悬浮看完整详情（截断只是排版，原文仍然给得出来） */
function fullDetails(entry: AuditEntry): string {
  return entry.details ? JSON.stringify(entry.details) : ''
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <!-- 筛选 -->
    <section class="sw-card p-4">
      <div class="flex flex-wrap items-end gap-3">
        <label class="block w-full min-w-0 sm:w-64">
          <span class="sw-label">动作过滤</span>
          <input
            v-model="action"
            class="sw-input font-mono text-xs"
            placeholder="如 config.update / user.create"
            @keyup.enter="load"
          />
        </label>

        <label class="block w-24">
          <span class="sw-label">条数</span>
          <input v-model.number="limit" type="number" min="1" max="1000" class="sw-input tabular" />
        </label>

        <BaseButton :loading="loading" @click="load">
          <template #icon><Icon name="search" :size="14" /></template>
          查询
        </BaseButton>

        <p class="ml-auto text-xs text-muted">
          共 <span class="tabular text-content">{{ entries.length }}</span> 条（倒序）
        </p>
      </div>
    </section>

    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>

    <section class="sw-card overflow-hidden">
      <header class="border-b border-border bg-sunken/60 px-4 py-3">
        <h2 class="sw-section-title">审计日志</h2>
        <p class="mt-0.5 text-xs leading-relaxed text-muted">
          改配置 / 导入 / 重置 / 建号 / 删号 / 登录失败都会留痕；内容已在后端脱敏，
          <strong class="text-content">不含 secret 与 openid</strong>，可放心复制给别人排障。
        </p>
      </header>

      <div v-if="loading" class="space-y-2 p-4" aria-busy="true" aria-live="polite">
        <div v-for="n in 5" :key="n" class="flex items-center gap-3">
          <div class="sw-skeleton h-4 w-32" />
          <div class="sw-skeleton h-4 w-24" />
          <div class="sw-skeleton ml-auto h-4 w-40" />
        </div>
        <span class="sr-only">正在加载审计日志…</span>
      </div>

      <EmptyState
        v-else-if="!entries.length"
        title="没有匹配的审计记录"
        description="换一个动作过滤词、调大条数再查；如果确实什么都没做过，这里就会是空的。"
        icon="shield"
      />

      <div v-else class="max-h-[70vh] overflow-auto">
        <table class="w-full text-sm">
          <caption class="sr-only">
            审计记录列表：时间、动作、对象、操作用户、来源 IP 与详情（倒序，最新在最上面）
          </caption>
          <thead class="sticky top-0 z-10 bg-elevated">
            <tr class="border-b border-border text-left text-xs text-muted">
              <th scope="col" class="whitespace-nowrap px-4 py-2 font-medium">时间</th>
              <th scope="col" class="px-3 py-2 font-medium">动作</th>
              <th scope="col" class="px-3 py-2 font-medium">对象</th>
              <th scope="col" class="px-3 py-2 font-medium">用户</th>
              <th scope="col" class="whitespace-nowrap px-3 py-2 font-medium">来源 IP</th>
              <th scope="col" class="px-4 py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="entry in entries"
              :key="entry.id"
              class="border-b border-border last:border-0 align-top hover:bg-sunken/60"
            >
              <td class="whitespace-nowrap px-4 py-2 text-xs tabular text-content">
                {{ entry.created_at }}
              </td>
              <td class="px-3 py-2">
                <code
                  class="whitespace-nowrap rounded bg-sunken px-1.5 py-0.5 font-mono text-[11px]
                         text-content"
                >
                  {{ entry.action }}
                </code>
              </td>
              <td class="px-3 py-2 text-muted">{{ entry.target ?? '—' }}</td>
              <td class="px-3 py-2 text-xs tabular text-muted">{{ entry.user_id ?? '—' }}</td>
              <td class="whitespace-nowrap px-3 py-2 font-mono text-xs text-muted">
                {{ entry.ip ?? '—' }}
              </td>
              <td
                class="px-4 py-2 font-mono text-[11px] leading-relaxed text-muted"
                :title="fullDetails(entry)"
              >
                {{ details(entry) }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
