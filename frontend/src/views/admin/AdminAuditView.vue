<script setup lang="ts">
/**
 * 审计日志（5.5）—— 谁在什么时候改了什么
 *
 * Q17 要求「配置类操作必须留痕」。这个页面就是那条要求的可见部分：
 * 改配置 / 导入 / 重置 / 建号 / 删号 / 登录失败都会出现在这里。
 *
 * 📌 审计内容**绝不含 secret 与 openid**（后端写入前已脱敏）——
 * 所以这个页面可以放心展示给管理员，也方便复制给别人排障。
 */
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { AuditEntry } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
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

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <div class="sw-card flex flex-wrap items-end gap-2 p-4">
      <label class="flex flex-col gap-1 text-xs text-muted">
        动作过滤
        <input
          v-model="action"
          class="sw-input w-56"
          placeholder="如 config.update / user.create"
          @keyup.enter="load"
        />
      </label>
      <label class="flex flex-col gap-1 text-xs text-muted">
        条数
        <input v-model.number="limit" type="number" min="1" max="1000" class="sw-input w-24" />
      </label>
      <BaseButton :loading="loading" @click="load">查询</BaseButton>
      <p class="ml-auto text-xs text-muted">共 {{ entries.length }} 条（倒序）</p>
    </div>

    <p v-if="fatal" class="rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ fatal }}
    </p>

    <section class="sw-card p-4">
      <p v-if="loading" class="py-6 text-center text-sm text-muted">加载中…</p>

      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-left text-xs text-muted">
              <th class="py-2 pr-3 font-medium">时间</th>
              <th class="py-2 pr-3 font-medium">动作</th>
              <th class="py-2 pr-3 font-medium">对象</th>
              <th class="py-2 pr-3 font-medium">用户</th>
              <th class="py-2 pr-3 font-medium">来源 IP</th>
              <th class="py-2 font-medium">详情</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="entry in entries" :key="entry.id" class="border-b border-border/60 last:border-0">
              <td class="whitespace-nowrap py-2 pr-3 text-content">{{ entry.created_at }}</td>
              <td class="py-2 pr-3">
                <code class="rounded bg-surface px-1 text-xs">{{ entry.action }}</code>
              </td>
              <td class="py-2 pr-3 text-muted">{{ entry.target ?? '—' }}</td>
              <td class="py-2 pr-3 text-muted">{{ entry.user_id ?? '—' }}</td>
              <td class="py-2 pr-3 text-muted">{{ entry.ip ?? '—' }}</td>
              <td class="py-2 font-mono text-xs text-muted">{{ details(entry) }}</td>
            </tr>
            <tr v-if="!entries.length">
              <td colspan="6" class="py-6 text-center text-muted">没有匹配的记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  </div>
</template>
