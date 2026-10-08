<script setup lang="ts">
/**
 * 应用外壳（M5 §5.1）
 *
 * 只做两件事：路由出口 + 「首次身份探测中」的过渡态。
 * 顶部导航（含站点名 / 主题切换 / 用户菜单）在 5.3-5.5 的页面骨架里补，
 * 这里保持最小 —— 认证页不需要导航栏。
 */
import { computed } from 'vue'
import { RouterView } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const booting = computed(() => !auth.ready)
</script>

<template>
  <div v-if="booting" class="flex h-full items-center justify-center bg-surface">
    <p class="text-sm text-muted" role="status">正在加载…</p>
  </div>

  <RouterView v-else />
</template>
