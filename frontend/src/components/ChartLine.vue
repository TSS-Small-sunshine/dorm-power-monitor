<script setup lang="ts">
/**
 * Chart.js 折线图封装（E3 剩余电量趋势 / E6 每日用电 / UI 改版 v2）
 *
 * 为什么要包一层
 * ==============
 *
 * * **无公网 CDN**：Chart.js 走 npm 依赖并打进产物（5.9）。这里只
 *   `register` 用到的部件，Tree-shaking 后体积远小于整包 UMD。
 * * **跟随主题**：坐标轴/网格颜色取自 CSS 变量，深浅色切换后重画
 *   （Chart.js 不会自动感知 CSS 变量变化）。
 * * **空数据要给人话**：没数据时画一张空图会让人以为坏了 —— 交给
 *   `EmptyState` 说清「是什么空了」。
 *
 * v2 修了一个真实缺陷 + 三处观感
 * =============================
 *
 * 🐞 **深浅色切换后图不变色**：旧版靠 `watch(() => document.documentElement
 * .classList.contains('dark'))` 重画 —— 但这不是响应式依赖，`classList` 变了
 * 也不会触发 watch，所以切主题后坐标轴还是旧颜色（浅色网格浮在深色底上）。
 * 现在用 `MutationObserver` 监听 `<html>` 的 class 变化，真的会重画。
 *
 * 观感：① 面积改用**渐变**（顶部有色、底部透明），比整块 12% 平涂更像"趋势"；
 * ② 点数多时不再画每个点（旧版 30 天视图上是一串噪点）；③ 悬浮用
 * `mode: 'index'` —— 手指/鼠标在图上游走就能看到每个时刻的值，不用精确命中点。
 */
import {
  CategoryScale,
  Chart,
  Filler,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  Tooltip,
} from 'chart.js'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import EmptyState from '@/components/EmptyState.vue'

Chart.register(CategoryScale, LinearScale, LineController, LineElement, PointElement, Filler, Tooltip)

const props = withDefaults(
  defineProps<{
    /** 横轴标签 */
    labels: string[]
    /** 纵轴数据 */
    values: (number | null)[]
    /** 纵轴单位（tooltip 后缀） */
    unit?: string
    /** 曲线颜色（默认品牌色） */
    color?: string
    height?: number
    /** 数据为空时的提示 */
    emptyText?: string
    /**
     * 纵轴是否从 0 起。
     *
     * 默认 `true`：剩余电量这类指标「0 = 用光」是有意义的基准，
     * 从 0 起才看得出"还剩多少"。改成 `false` 会把小幅波动放大成剧烈起落，
     * 看着刺激但会误导 —— 除非指标本身没有零点（如差值类）。
     */
    beginAtZero?: boolean
  }>(),
  {
    unit: '',
    color: '',
    height: 220,
    emptyText: '暂无数据',
    beginAtZero: true,
  },
)

const canvas = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null
let observer: MutationObserver | null = null

/** 读 CSS 变量拿通道值（`22 119 255`） */
function channels(name: string, fallback: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return raw || fallback
}

/** `22 119 255` → `rgb(22, 119, 255)` / `rgba(22, 119, 255, .3)`
 *  （用经典逗号语法：canvas 的 `fillStyle` 对 CSS4 空格语法的支持还不齐） */
function paint(raw: string, alpha = 1): string {
  const [r, g, b] = raw.split(/[\s,]+/).map(Number) as [number, number, number]
  return alpha >= 1 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`
}

function build(): void {
  if (!canvas.value) return
  const lineRaw = props.color || channels('--color-brand', '22 119 255')
  const grid = paint(channels('--color-border', '226 229 235'))
  const text = paint(channels('--color-muted', '104 112 130'))
  const line = paint(lineRaw)
  const total = props.values.length

  chart?.destroy()
  chart = new Chart(canvas.value, {
    type: 'line',
    data: {
      labels: props.labels,
      datasets: [
        {
          data: props.values,
          borderColor: line,
          borderWidth: 2,
          backgroundColor: (context) => {
            const { ctx, chartArea } = context.chart
            if (!chartArea) return 'transparent'
            const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom)
            gradient.addColorStop(0, paint(lineRaw, 0.26))
            gradient.addColorStop(1, paint(lineRaw, 0))
            return gradient
          },
          fill: true,
          tension: 0.3,
          // 点少时画点（否则一个点也看不见），点多时交给曲线本身
          pointRadius: total <= 60 ? 2 : 0,
          pointHoverRadius: 4,
          pointBackgroundColor: line,
          spanGaps: true, // null 不连线（后端用 null 表示「算不出来」）
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 200 },
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          backgroundColor: paint(channels('--color-content', '23 26 33'), 0.92),
          padding: 10,
          cornerRadius: 8,
          callbacks: {
            label: (item) => {
              const value = item.parsed.y
              return value === null || value === undefined ? '—' : `${value}${props.unit}`
            },
          },
        },
      },
      scales: {
        x: {
          grid: { display: false },
          border: { display: false },
          ticks: { color: text, maxTicksLimit: 8, maxRotation: 0 },
        },
        y: {
          beginAtZero: props.beginAtZero,
          grid: { color: grid },
          border: { display: false },
          ticks: { color: text, maxTicksLimit: 5, padding: 6 },
        },
      },
    },
  })
}

onMounted(() => {
  build()
  // 🐞 修：class 变化不是 Vue 的响应式依赖，必须用 MutationObserver 才收得到
  observer = new MutationObserver(() => build())
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] })
})

// 数据变化后重画
watch(
  () => [props.labels, props.values],
  () => build(),
  { deep: true },
)

onBeforeUnmount(() => {
  observer?.disconnect()
  observer = null
  chart?.destroy()
})
</script>

<template>
  <div :style="{ height: `${height}px` }" class="relative">
    <EmptyState v-if="!values.length" title="还没有数据" :description="emptyText" icon="chart" />
    <canvas v-show="values.length" ref="canvas" />
  </div>
</template>
