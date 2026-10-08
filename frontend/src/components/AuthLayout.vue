<script setup lang="ts">
/**
 * 认证界面统一骨架（M5 §5.3 / Q21）
 *
 * 6 个认证界面（登录 / 首登改密 / 改密 / 忘记密码 / 会话过期 / 无权限）
 * **全部**套这个壳 —— 这是 Q21 的核心诉求：视觉一致，且**没有浏览器原生
 * Basic Auth 弹窗**（那个弹窗完全无法定制，就是「丑」的根源）。
 *
 * 布局
 * ====
 *
 * * 4 个断点：窄屏单列铺满、宽屏居中卡片（`max-w-[26rem]`）
 * * 键盘可达：内容顺序就是 Tab 顺序；品牌区不拦截焦点
 * * 深色/浅色都好看：颜色全部走 tokens.css 的变量
 */
import { computed } from 'vue'

import { useAuthStore } from '@/stores/auth'
import ThemeToggle from '@/components/ThemeToggle.vue'

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
  <div class="flex min-h-full flex-col bg-surface">
    <!-- 顶部工具条：只放主题切换，保持认证页干净 -->
    <header class="flex justify-end p-4">
      <ThemeToggle />
    </header>

    <main class="flex flex-1 items-center justify-center px-4 pb-10">
      <div class="w-full max-w-[26rem]">
        <!-- 品牌区 -->
        <div class="mb-6 text-center">
          <div
            class="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-card bg-brand/10 text-2xl"
            aria-hidden="true"
          >
            ⚡
          </div>
          <h1 class="text-xl font-bold text-content">{{ auth.siteName }}</h1>
          <p class="mt-1 text-sm text-muted">宿舍电量监控</p>
        </div>

        <!-- 表单卡片 -->
        <section class="sw-card p-6" :aria-label="props.title">
          <h2 class="text-lg font-semibold text-content">{{ props.title }}</h2>
          <p v-if="props.subtitle" class="mt-1 text-sm text-muted">{{ props.subtitle }}</p>

          <div class="mt-5">
            <slot />
          </div>
        </section>

        <p class="mt-6 text-center text-xs text-muted">
          <span v-if="version">{{ version }}</span>
          <span v-else>StarWatt 星瓦</span>
        </p>
      </div>
    </main>
  </div>
</template>
