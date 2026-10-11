<script setup lang="ts">
/**
 * 登录（M5 §5.3 / UI 改版 v2）
 *
 * 三条 legacy 教训（都体现在这里）
 * =================================
 *
 * 1. **不弹浏览器原生 Basic Auth**：全部自己画（Q21）
 * 2. **失败要能看见**：错误行内显示，且**不区分**「用户名不存在 / 密码错误」
 *    （后端已经统一成一句中文，前端不要自作聪明补细节）
 * 3. **锁定要能解释**：后端回 429 时把它的中文提示原样显示
 *    （「失败次数过多，请 15 分钟后再试」）
 *
 * v2 改动（只动展示层，登录逻辑一行没变）
 * =====================================
 *
 * * **错误挂在表单级，不挂到某个输入框上**：把「用户名或密码不正确」标到
 *   密码框下面，等于告诉试探的人「用户名是对的」—— Q21 要的正好相反。
 * * **锁定与「输错了」分开播报**：429 是「等一会儿再来」的状态，不是一次
 *   输入错误，用 `aria-live="polite"` 常驻提示；普通失败用 `role="alert"`
 *   立刻打断。
 *   ⚠️ 后端只回固定文案、**没有 `retry_after`**，所以这里不做倒计时 ——
 *   锁定期是按「最近 N 分钟内的失败次数」判定的，从本次响应时刻起算一定
 *   偏长，编一个倒计时比不显示更糟。
 * * 会话过期提示走 `Notice`（旧版是一行手写的黄底小字，没有图标）
 * * 首屏自动聚焦用户名 + 显式 `id`/`name`：密码管理器与读屏都靠它们认字段
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { ApiError } from '@/api/client'
import AuthLayout from '@/components/AuthLayout.vue'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const submitting = ref(false)
/** 429 = 账号被临时锁定；只用来决定「怎么播报」，不改任何提交逻辑 */
const locked = ref(false)

/** 会话过期（被 401 踢回登录页时带 ``?expired=1``） */
const expired = computed(() => route.query.expired === '1')

const canSubmit = computed(() => username.value.trim() !== '' && password.value !== '')

async function submit(): Promise<void> {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  locked.value = false
  try {
    await auth.login(username.value.trim(), password.value)
    // 首登未改密时，守卫会自动改道到 /password
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(target)
  } catch (error) {
    locked.value = error instanceof ApiError && error.status === 429
    password.value = '' // 密码清空，用户名留着（少打一次字）
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <AuthLayout title="登录" subtitle="用管理员或舍友账号登录">
    <form class="space-y-4" novalidate :aria-busy="submitting" @submit.prevent="submit">
      <Notice v-if="expired" tone="warning" icon="clock">
        登录状态已过期，请重新登录（原目标页面会在登录后自动打开）。
      </Notice>

      <BaseField
        id="login-username"
        v-model="username"
        name="username"
        label="用户名"
        autocomplete="username"
        placeholder="请输入用户名"
        autofocus
        required
        :disabled="submitting"
      />

      <BaseField
        id="login-password"
        v-model="password"
        name="password"
        label="密码"
        type="password"
        autocomplete="current-password"
        placeholder="请输入密码"
        required
        :disabled="submitting"
      />

      <!--
        锁定态自己写块，而不是用 `Notice`：`Notice` 的 warning/danger 固定
        `role="alert"`（打断式），而「等 15 分钟」是个需要常驻、慢慢播报的状态。
        配色仍与 `Notice` 同源（.sw-notice + warning token），不引入第二套观感。
      -->
      <div
        v-if="auth.error && locked"
        class="sw-notice border-warning/30 bg-warning-soft text-warning"
        role="status"
        aria-live="polite"
      >
        <Icon name="clock" :size="16" class="mt-0.5" />
        <div class="min-w-0 flex-1 leading-relaxed">{{ auth.error }}</div>
      </div>
      <Notice v-else-if="auth.error" tone="danger">{{ auth.error }}</Notice>

      <BaseButton type="submit" block :loading="submitting" :disabled="!canSubmit">
        {{ submitting ? '登录中…' : '登录' }}
      </BaseButton>

      <div class="sw-divider" aria-hidden="true" />

      <RouterLink
        to="/forgot"
        class="flex items-center justify-center gap-1.5 text-xs text-muted transition hover:text-content"
      >
        <Icon name="key" :size="14" />
        忘记密码？
      </RouterLink>
    </form>
  </AuthLayout>
</template>
