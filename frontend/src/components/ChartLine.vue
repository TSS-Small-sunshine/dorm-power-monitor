<script setup lang="ts">
/**
 * Chart.js 折线图封装（E3 剩余电量趋势 / E6 每日用电）
 *
 * 为什么要包一层
 * ==============
 *
 * * **无公网 CDN**：Chart.js 走 npm 依赖并打进产物（5.9）。这里只
 *   `register` 用到的部件，Tree-shaking 后体积远小于整包 UMD。
 * * **跟随主题**：坐标轴/网格颜色取自 CSS 变量，深浅色切换后重画
 *   （Chart.js 不会自动感知 CSS 变量变化）。
 * * **空数据要给人话**：没数据时画一张空图会让人以为坏了 —— 显示一句提示。
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
  }>(),
  {
    unit: '',
    color: '',
    height: 220,
    emptyText: '暂无数据',
  },
)

const canvas = ref<HTMLCanvasElement | null>(null)
let chart: Chart | null = null

/** 读 CSS 变量拿主题色（`22 119 255` → `rgb(22 119 255)`） */
function token(name: string, fallback: string): string {
  const raw = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return raw ? `rgb(${raw})` : fallback
}

function build(): void {
  if (!canvas.value) return
  const line = props.color || token('--color-brand', '#1677ff')
  const grid = token('--color-border', '#e2e5eb')
  const text = token('--color-muted', '#6c7485')

  chart?.destroy()
  chart = new Chart(canvas.value, {
    type: 'line',
    data: {
      labels: props.labels,
      datasets: [
        {
          data: props.values,
          borderColor: line,
          backgroundColor: line.replace('rgb(', 'rgba(').replace(')', ' / 0.12)'),
          fill: true,
          tension: 0.3,
          pointRadius: 2,
          pointHoverRadius: 4,
          spanGaps: true, // null 不连线（后端用 null 表示「算不出来」）
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 200 },
      plugins: {
        legend: { display: false },
        tooltip: {
          displayColors: false,
          callbacks: {
            label: (item) => {
              const value = item.parsed.y
              return value === null || value === undefined
                ? '—'
                : `${value}${props.unit}`
            },
          },
        },
      },
      scales: {
        x: { grid: { display: false }, ticks: { color: text, maxTicksLimit: 8 } },
        y: { grid: { color: grid }, ticks: { color: text }, beginAtZero: true },
      },
    },
  })
}

onMounted(build)

// 数据 / 主题变化后重画
watch(
  () => [props.labels, props.values],
  () => build(),
  { deep: true },
)
watch(
  () => document.documentElement.classList.contains('dark'),
  () => build(),
)

onBeforeUnmount(() => chart?.destroy())
</script>

<template>
  <div :style="{ height: `${height}px` }" class="relative">
    <p
      v-if="!values.length"
      class="absolute inset-0 flex items-center justify-center text-sm text-muted"
    >
      {{ emptyText }}
    </p>
    <canvas v-show="values.length" ref="canvas" />
  </div>
</template>
