<script setup lang="ts">
/**
 * 404（M5 §5.3 的认证界面家族之一）
 *
 * 为什么 SPA 也需要它：客户端路由的后退/收藏夹里可能有失效链接，
 * 白屏会让用户以为服务挂了。这里给一句人话 + 一个出口。
 *
 * v2 改动
 * ======
 *
 * * 加了 `404` 这个**大字号装饰数字**：用户从收藏夹点进来时，第一眼要能
 *   确认「地址错了」而不是「页面挂了」。数字用 `aria-hidden` 标掉 ——
 *   读屏念「四零四」没有任何信息量，副标题那句人话才是内容
 * * 出口做成主按钮样式（旧版是一句链接），并补一条「浏览器后退」的提示：
 *   404 最常见的场景就是点错了一个旧链接，后退往往比回总览更快
 */
import { RouterLink } from 'vue-router'

import AuthLayout from '@/components/AuthLayout.vue'
import Icon from '@/components/Icon.vue'
</script>

<template>
  <AuthLayout title="页面不存在" subtitle="链接可能已经失效，或者你输错了地址">
    <div class="text-center">
      <p class="tabular text-5xl font-bold leading-none text-brand/30" aria-hidden="true">404</p>

      <p class="mt-4 text-sm leading-relaxed text-muted">
        如果这是从收藏夹或别人分享的链接点进来的，它可能已经被改名或删掉了。
      </p>

      <RouterLink
        to="/"
        class="mt-5 flex w-full items-center justify-center gap-1.5 rounded-control bg-brand
               px-4 py-2 text-sm font-medium text-brand-ink transition hover:bg-brand-strong"
      >
        <Icon name="gauge" :size="16" />
        回到总览
      </RouterLink>

      <p class="mt-3 text-xs text-muted">或者按浏览器的后退键回到上一页。</p>
    </div>
  </AuthLayout>
</template>
