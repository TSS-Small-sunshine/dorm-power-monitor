<script setup lang="ts">
/**
 * 电表段（E11 / UI 改版 v2）
 *
 * 字段全部是**驼峰**（``run_status`` / ``vol`` / ``cur`` / ``yggl`` / ``update_dt``）——
 * 这是门户返回的形状，也是卡片契约里的形状（见 ``card_builder`` 的说明：
 * 下划线键是内部表示，不该出现在契约里）。这里只展示，不做单位换算：
 * 门户给的就是带单位的字符串（如 ``"220.5"``）。
 *
 * ⚠️ 契约里**没有**「电表编号 / 房间号」这一项
 * ==========================================
 *
 * ``/api/live`` 的 ``run_status`` 只有 5 个键（``vol`` / ``cur`` / ``yggl`` /
 * ``run_status`` / ``update_dt``，见 ``tests/regression/fixtures/api.json`` 与
 * ``dashboard_service.live``），``LivePayload`` 本身也没有 ``room_id``。
 * 硬塞一行恒为「—」的编号是假信息，比不显示更糟 —— 所以本段按 E11 的
 * 五项（电压 / 电流 / 有功功率 / 电表状态 / 最后上报）呈现，另加一条
 * 「最后更新」（``latest_ts``，与概览页同一叫法）。
 *
 * v2 改了什么（全部是**展示层**）
 * ==============================
 *
 * 1. **状态是胶囊**：``sw-chip`` + 图标 + 文字 + 底色四重表达。旧版是
 *    一行 2xl 加粗文字，在线黑色、离线红色 —— 只靠颜色区分，色觉障碍
 *    用户看不出差别。
 * 2. **读数改成信息网格**：``<dl>`` 里 label / value 成对，窄屏一列、
 *    中屏两列、宽屏三列。旧版是 3 张 ``StatCard``，把「电压」这种一行字
 *    撑成一张大卡，白占一屏。
 * 3. **离线时给一条 ``Notice``**：旧版是一段 12px 红字，前面还挂了个
 *    ``⚠`` 字符（emoji 字形各平台不一致）。现在由 ``Notice`` 的 danger
 *    图标 + ``role="alert"`` 承担，读屏会立刻播报。
 * 4. **缺字段一律「—」**：后端没返回时不留空白 —— 空白像「坏了」，
 *    「—」才像「这次没有」。
 */
import { computed } from 'vue'

import type { LivePayload, RunStatus } from '@/api/types'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'

const props = defineProps<{ live: LivePayload | null }>()

const meter = computed<RunStatus>(() => props.live?.run_status ?? {})

/** 电表认为「在线」的三种取值（与后端 thresholds.ONLINE_LABELS 同义） */
const ONLINE = ['在线', '正常', '通讯正常']

/** 状态胶囊的底色（颜色不是唯一线索 —— 还有图标与文字） */
const CHIP: Record<string, string> = {
  muted: 'bg-sunken text-muted',
  success: 'bg-success-soft text-success',
  danger: 'bg-danger-soft text-danger',
}

function text(value: string | null | undefined): string {
  return value && value.trim() !== '' ? value : '—'
}

/** 状态：颜色 + 图标 + 文字三重表达 */
const status = computed(() => {
  if (!props.live) return { tone: 'muted', text: '等待数据', icon: 'clock' }
  const raw = (meter.value.run_status ?? '').trim()
  if (raw === '') return { tone: 'muted', text: '状态未知', icon: 'info' }
  if (ONLINE.includes(raw)) return { tone: 'success', text: raw, icon: 'check' }
  return { tone: 'danger', text: raw, icon: 'alert' }
})

/** 离线（或状态异常）—— 后端返回的既不是「在线」也不是空 */
const offline = computed(() => status.value.tone === 'danger')

interface InfoItem {
  key: string
  label: string
  value: string
  unit: string
}

/** 信息网格：label + value 成对；缺字段显示「—」（不留空白） */
const infoItems = computed<InfoItem[]>(() => [
  { key: 'vol', label: '电压', value: text(meter.value.vol), unit: 'V' },
  { key: 'cur', label: '电流', value: text(meter.value.cur), unit: 'A' },
  { key: 'yggl', label: '有功功率', value: text(meter.value.yggl), unit: 'W' },
  { key: 'update_dt', label: '最后上报', value: text(meter.value.update_dt), unit: '' },
  { key: 'latest_ts', label: '最后更新', value: text(props.live?.latest_ts), unit: '' },
])
</script>

<template>
  <div class="space-y-4">
    <!-- E11 电表状态 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="!live">
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <p class="text-xs font-medium text-muted">电表状态</p>
          <div class="mt-2 flex flex-wrap items-center gap-x-3 gap-y-2">
            <span class="sw-chip px-3 py-1.5 text-sm" :class="CHIP[status.tone]">
              <Icon :name="status.icon" :size="14" />
              {{ status.text }}
            </span>
            <span class="inline-flex items-center gap-1.5 text-xs text-muted">
              <Icon name="clock" :size="13" />
              最后上报
              <span class="tabular">{{ text(meter.update_dt) }}</span>
            </span>
          </div>
        </div>

        <span class="sw-chip bg-sunken text-muted">
          <Icon name="cpu" :size="12" />
          F4 实时读数
        </span>
      </div>

      <Notice v-if="offline" tone="danger" class="mt-4">
        电表不在线 —— 数据可能停止更新，请检查宿舍配电箱或联系宿管。
      </Notice>
      <Notice v-else-if="!live" tone="info" class="mt-4">正在等待第一次抓取…</Notice>
      <Notice v-else-if="status.tone === 'muted'" tone="warning" class="mt-4">
        还没有读到电表状态 —— 抓取成功后这里会显示电压、电流与有功功率。
      </Notice>
    </section>

    <!-- E11 读数网格 -->
    <section class="sw-card p-4 sm:p-5" :aria-busy="!live">
      <div class="mb-3">
        <h2 class="sw-section-title flex items-center gap-1.5">
          <Icon name="gauge" :size="15" class="text-muted" />
          实时读数
        </h2>
        <p class="mt-1 text-xs leading-relaxed text-muted">
          来自学校门户的 F4 接口，随抓取周期更新（默认 10 分钟一次）；后端没返回的字段显示「—」。
        </p>
      </div>

      <dl class="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
        <div v-for="item in infoItems" :key="item.key" class="rounded-control bg-sunken px-3.5 py-3">
          <dt class="text-xs text-muted">{{ item.label }}</dt>
          <dd class="mt-1 text-base font-semibold text-content">
            <span class="tabular">{{ item.value }}</span>
            <span
              v-if="item.unit && item.value !== '—'"
              class="ml-1 text-xs font-normal text-muted"
            >
              {{ item.unit }}
            </span>
          </dd>
        </div>
      </dl>
    </section>
  </div>
</template>
