<script setup lang="ts">
/**
 * 登录（M5 §5.3）
 *
 * 三条 legacy 教训（都体现在这里）
 * =================================
 *
 * 1. **不弹浏览器原生 Basic Auth**：全部自己画（Q21）
 * 2. **失败要能看见**：错误行内显示，且**不区分**「用户名不存在 / 密码错误」
 *    （后端已经统一成一句中文，前端不要自作聪明补细节）
 * 3. **锁定要能解释**：后端回 429 时把它的中文提示原样显示
 *    （「失败次数过多，请 15 分钟后再试」）
 */
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import AuthLayout from '@/components/AuthLayout.vue'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const username = ref('')
const password = ref('')
const submitting = ref(false)

const canSubmit = computed(() => username.value.trim() !== '' && password.value !== '')

async function submit(): Promise<void> {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  try {
    await auth.login(username.value.trim(), password.value)
    // 首登未改密时，守卫会自动改道到 /password
    const target = typeof route.query.redirect === 'string' ? route.query.redirect : '/'
    await router.replace(target)
  } catch {
    password.value = '' // 密码清空，用户名留着（少打一次字）
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <AuthLayout title="登录" subtitle="用管理员或舍友账号登录">
    <form class="space-y-4" novalidate @submit.prevent="submit">
      <BaseField
        v-model="username"
        label="用户名"
        autocomplete="username"
        placeholder="请输入用户名"
        autofocus
        :disabled="submitting"
      />

      <BaseField
        v-model="password"
        label="密码"
        type="password"
        autocomplete="current-password"
        placeholder="请输入密码"
        :disabled="submitting"
      />

      <p v-if="auth.error" class="sw-error" role="alert">{{ auth.error }}</p>

      <BaseButton type="submit" block :loading="submitting" :disabled="!canSubmit">
        {{ submitting ? '登录中…' : '登录' }}
      </BaseButton>
    </form>
  </AuthLayout>
</template>
