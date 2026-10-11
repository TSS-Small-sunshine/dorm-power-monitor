<script setup lang="ts">
/**
 * 认证界面统一骨架（M5 §5.3 / Q21 / UI 改版 v2）
 *
 * 6 个认证界面（登录 / 首登改密 / 改密 / 忘记密码 / 会话过期 / 无权限）
 * **全部**套这个壳 —— 这是 Q21 的核心诉求：视觉一致，且**没有浏览器原生
 * Basic Auth 弹窗**（那个弹窗完全无法定制，就是「丑」的根源）。
 *
 * 布局
 * ====
 *
 * * **窄屏铺满、宽屏居中**：<768 时卡片去掉圆角与左右边框、用负外边距顶到
 *   屏幕边缘 —— 手机上应该是一张「整页表单」，而不是浮在灰底上的小方块
 * * **品牌区在卡片之上**：站点名是用户自己填的（可能叫「XX 宿舍」），所以
 *   需要一个固定元素让人确认「这是星瓦」。用 SVG 的 ⚡ 而不是 emoji：
 *   emoji 的字形随系统变（Linux 上是单色），与旁边的文字不是一套视觉
 * * **键盘可达**：内容顺序就是 Tab 顺序；品牌区与页脚不拦截焦点
 * * 深色/浅色都好看：颜色全部走 tokens.css 的变量
 *
 * v2 改动
 * ======
 *
 * * 品牌标记从 emoji 换成 `<Icon name="zap">`，底色改成实心品牌色 —— 认证页
 *   只有一张卡，需要一处足够确定的视觉锚点
 * * 页面底从 `surface` 换成 `sunken`：卡片是 `elevated`，与页面底差一级才
 *   看得出「哪里是卡片」（旧版两者几乎同色，全靠边框猜）
 * * 卡片入场用 `animate-fade-in-up`（`prefers-reduced-motion` 下自动关闭）
 * * 页脚版本号用等宽字形（`.tabular`），在几个认证页之间不再左右跳
 */
import { computed } from 'vue'

import Icon from '@/components/Icon.vue'
import ThemeToggle from '@/components/ThemeToggle.vue'
import { useAuthStore } from '@/stores/auth'

const props = defineProps<{
  /** 标题（如「登录」） */
  title: string
  /** 副标题（一句话说明这个页面要做什么） */
  subtitle?: string
}>()

const auth = useAuthStore()
const version = computed(() => auth.site?.app_version ?? '')
</script>

<template>
  <div class="relative flex min-h-full flex-col bg-sunken">
    <!-- 顶部品牌色光晕：纯装饰，不参与交互，也不压正文对比度 -->
    <div
      class="pointer-events-none absolute inset-x-0 top-0 h-56 bg-gradient-to-b from-brand/10 to-transparent"
      aria-hidden="true"
    />

    <!-- 顶部工具条：只放主题切换，保持认证页干净 -->
    <header class="relative flex justify-end px-4 py-4">
      <ThemeToggle />
    </header>

    <main class="relative flex flex-1 items-start justify-center px-4 pb-12 sm:items-center sm:pb-16">
      <div class="w-full max-w-[26rem]">
        <!-- 品牌区 -->
        <div class="mb-6 text-center">
          <div
            class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-card
                   bg-brand text-brand-ink shadow-card"
            aria-hidden="true"
          >
            <Icon name="zap" :size="24" />
          </div>
          <h1 class="text-xl font-bold text-content">{{ auth.siteName }}</h1>
          <p class="mt-1 text-sm text-muted">宿舍电量监控</p>
        </div>

        <!-- 表单卡片 -->
        <section
          class="sw-card -mx-4 animate-fade-in-up rounded-none border-x-0 p-5
                 sm:mx-0 sm:rounded-card sm:border-x sm:p-6"
          :aria-label="props.title"
        >
          <h2 class="text-lg font-semibold text-content">{{ props.title }}</h2>
          <p v-if="props.subtitle" class="mt-1 text-sm leading-relaxed text-muted">
            {{ props.subtitle }}
          </p>

          <div class="mt-5">
            <slot />
          </div>
        </section>

        <p class="mt-6 text-center text-xs text-muted">
          <span v-if="version" class="tabular">{{ version }}</span>
          <span v-else>StarWatt 星瓦</span>
        </p>
      </div>
    </main>
  </div>
</template>
