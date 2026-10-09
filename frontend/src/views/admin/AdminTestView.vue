<script setup lang="ts">
/**
 * 连通测试（5.5）—— 合并了 legacy OOBE 的两个 validate 端点
 *
 * 为什么这个页面值得存在：渠道配错了（webhook 打错、Bot Secret 填成 AppSecret）
 * 在**自动推送**里表现为「什么都没发生」，非常难排查。主动点一次按钮拿到
 * 明确的成功/失败，是排障的第一入口。
 *
 * 后端 ``POST /api/admin/config/test`` 是**显式动作例外**：即使群推送总开关
 * 关着也会真的发一条（见 ``starwatt/services/admin_service.py``）——
 * 否则「睡前关推送」之后这个按钮就废了。
 */
import { ref } from 'vue'

import { api } from '@/api/client'
import type { ConfigTestResult } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const TARGETS = [
  { key: 'feishu_group', label: '飞书群机器人', hint: '往配置的 webhook 发一张测试卡片' },
  { key: 'qq', label: 'QQ 官方机器人', hint: '给最近一次会话（或配置的目标）发一条测试消息' },
]

const results = ref<Record<string, ConfigTestResult | null>>({})
const busy = ref('')
const fatal = ref('')

async function run(target: string): Promise<void> {
  busy.value = target
  fatal.value = ''
  try {
    results.value = { ...results.value, [target]: await api.configTest(target) }
  } catch (err) {
    if (auth.handle(err)) return
    // 后端用 400 + {ok:false,error} 表达「配置不对」，这里也当成一次结果展示
    results.value = {
      ...results.value,
      [target]: {
        ok: false,
        target,
        error: err instanceof Error ? err.message : '测试失败',
      },
    }
  } finally {
    busy.value = ''
  }
}
</script>

<template>
  <div class="space-y-4">
    <p v-if="fatal" class="rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ fatal }}
    </p>

    <section v-for="target in TARGETS" :key="target.key" class="sw-card p-4">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h2 class="text-sm font-semibold text-content">{{ target.label }}</h2>
          <p class="text-xs text-muted">{{ target.hint }}</p>
        </div>
        <BaseButton :loading="busy === target.key" @click="run(target.key)">发送测试</BaseButton>
      </div>

      <p
        v-if="results[target.key]"
        class="mt-3 rounded-control px-3 py-2 text-sm"
        :class="
          results[target.key]?.ok
            ? 'bg-success/10 text-success'
            : 'bg-danger/10 text-danger'
        "
        role="status"
      >
        <template v-if="results[target.key]?.ok">✅ 发送成功，请到对应渠道查看。</template>
        <template v-else>❌ {{ results[target.key]?.error || '发送失败' }}</template>
      </p>
    </section>

    <p class="text-xs text-muted">
      提示：测试消息不受「群推送总开关」影响（那是显式动作），但渠道凭据必须先在
      「配置中心」里填对。
    </p>
  </div>
</template>
