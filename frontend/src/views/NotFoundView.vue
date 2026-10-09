<script setup lang="ts">
/**
 * 404（M5 §5.3 的认证界面家族之一）
 *
 * 为什么 SPA 也需要它：客户端路由的后退/收藏夹里可能有失效链接，
 * 白屏会让用户以为服务挂了。这里给一句人话 + 一个出口。
 */
import { RouterLink } from 'vue-router'

import AuthLayout from '@/components/AuthLayout.vue'
</script>

<template>
  <AuthLayout title="页面不存在" subtitle="链接可能已经失效，或者你输错了地址">
    <RouterLink
      to="/"
      class="block w-full rounded-control bg-brand px-4 py-2 text-center text-sm font-medium text-white
             transition hover:brightness-110"
    >
      回到总览
    </RouterLink>
  </AuthLayout>
</template>
