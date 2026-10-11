<script setup lang="ts">
/**
 * 连通测试（5.5 / UI 改版 v2）—— 合并了 legacy OOBE 的两个 validate 端点
 *
 * 为什么这个页面值得存在：渠道配错了（webhook 打错、Bot Secret 填成 AppSecret）
 * 在**自动推送**里表现为「什么都没发生」，非常难排查。主动点一次按钮拿到
 * 明确的成功/失败，是排障的第一入口。
 *
 * 后端 ``POST /api/admin/config/test`` 是**显式动作例外**：即使群推送总开关
 * 关着也会真的发一条（见 ``starwatt/services/admin_service.py``）——
 * 否则「睡前关推送」之后这个按钮就废了。
 *
 * v2 只改**展示层**（``api.configTest()`` 的调用与「400 + {ok:false} 也当成一次结果」
 * 的处置一字未动）
 * ==========================================================================
 *
 * * **每个渠道一张卡**：带图标、说明与自己的「发送测试」按钮 ——
 *   旧版只有标题和一行小字，两个渠道挨在一起分不清哪个按钮对应哪个。
 * * **结果走 ``Notice``**：成功/失败不再是一段手写底色文字，
 *   失败时自带 ``role="alert"``，容器带 ``aria-live``，读屏会播报结果。
 * * **成功/失败的对勾与叉换成 ``Icon``**：emoji 的字形由系统字体决定，
 *   Linux 上是单色、Windows 上是彩色，且与旁边的文字对不齐。
 * * 正在测试时按钮 ``loading``（``BaseButton`` 自带 ``aria-busy``）。
 */
import { ref } from 'vue'

import { api } from '@/api/client'
import type { ConfigTestResult } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const TARGETS = [
  {
    key: 'feishu_group',
    label: '飞书群机器人',
    hint: '往配置的 webhook 发一张测试卡片',
    icon: 'send',
  },
  {
    key: 'qq',
    label: 'QQ 官方机器人',
    hint: '给最近一次会话（或配置的目标）发一条测试消息',
    icon: 'terminal',
  },
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
    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>

    <section v-for="target in TARGETS" :key="target.key" class="sw-card p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="flex min-w-0 items-start gap-3">
          <span
            class="flex h-9 w-9 shrink-0 items-center justify-center rounded-control
                   bg-brand-soft text-brand"
            aria-hidden="true"
          >
            <Icon :name="target.icon" :size="17" />
          </span>
          <div class="min-w-0">
            <h2 class="sw-section-title">{{ target.label }}</h2>
            <p class="mt-0.5 text-xs text-muted">{{ target.hint }}</p>
          </div>
        </div>

        <BaseButton
          :loading="busy === target.key"
          :disabled="busy !== '' && busy !== target.key"
          @click="run(target.key)"
        >
          <template #icon><Icon name="send" :size="14" /></template>
          发送测试
        </BaseButton>
      </div>

      <!-- 结果：异步到达，用 aria-live 让读屏播报 -->
      <div v-if="results[target.key]" class="mt-3" aria-live="polite">
        <Notice
          :tone="results[target.key]?.ok ? 'success' : 'danger'"
          :icon="results[target.key]?.ok ? 'check' : 'alert'"
        >
          <template v-if="results[target.key]?.ok">
            发送成功，请到对应渠道查看这条测试消息。
          </template>
          <template v-else>{{ results[target.key]?.error || '发送失败' }}</template>
        </Notice>
      </div>
    </section>

    <p class="text-xs leading-relaxed text-muted">
      提示：测试消息<strong class="text-content">不受「群推送总开关」影响</strong>
      （那是显式动作），但渠道凭据必须先在「配置中心」里填对。
    </p>
  </div>
</template>
