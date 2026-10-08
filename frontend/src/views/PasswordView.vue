<script setup lang="ts">
/**
 * 修改密码（M5 §5.3）—— 同时承担**首登强制改密**（B4/Q19）
 *
 * 为什么首登改密与普通改密是同一个页面
 * ====================================
 *
 * 两者只差一个「当前密码」的提示文案：强制改密时用户刚用临时密码登录，
 * 他心里想的是「临时密码是多少来着」。做成两个页面会出现两套校验与两套样式，
 * 正是 Q21 要消灭的东西。
 *
 * 密码强度提示与后端同源
 * ======================
 *
 * 后端 `starwatt/auth/password.py` 的策略（长度 + 大写 + 数字 + 特殊字符）在
 * 这里用**同一条规则**做前端预校验：只为「少一次往返」，**不替代**后端校验
 * （后端仍然会拒绝弱密码并回中文原因）。
 */
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'

import AuthLayout from '@/components/AuthLayout.vue'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const router = useRouter()

const oldPassword = ref('')
const newPassword = ref('')
const confirm = ref('')
const submitting = ref(false)
const serverError = ref('')
const done = ref(false)

/** 与后端 `pwd.validate_strength` 对齐的**本地**预检（仅用于即时反馈） */
function strengthProblem(value: string): string {
  if (value.length < 8) return '密码至少 8 位'
  if (!/[A-Z]/.test(value)) return '密码需包含大写字母'
  if (!/[0-9]/.test(value)) return '密码需包含数字'
  if (!/[^A-Za-z0-9]/.test(value)) return '密码需包含特殊字符'
  return ''
}

const newError = computed(() => (newPassword.value ? strengthProblem(newPassword.value) : ''))
const confirmError = computed(() =>
  confirm.value && confirm.value !== newPassword.value ? '两次输入的新密码不一致' : '',
)

const canSubmit = computed(
  () =>
    oldPassword.value !== '' &&
    newPassword.value !== '' &&
    confirm.value !== '' &&
    !newError.value &&
    !confirmError.value,
)

const forced = computed(() => auth.mustChangePassword)

async function submit(): Promise<void> {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  serverError.value = ''
  try {
    await auth.changePassword(oldPassword.value, newPassword.value)
    done.value = true
    // 改完密就进主界面（强制改密解除后守卫也会放行）
    await router.replace('/')
  } catch (error) {
    serverError.value = error instanceof Error ? error.message : '修改失败'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <AuthLayout
    :title="forced ? '设置新密码' : '修改密码'"
    :subtitle="
      forced
        ? '这是首次登录，请把初始密码换成只有你知道的密码'
        : '修改后其它设备上的登录会立即失效'
    "
  >
    <p v-if="forced" class="mb-4 rounded-control bg-warning/10 px-3 py-2 text-xs text-warning">
      初始密码来自安装日志或管理员，属于「临时凭据」—— 换掉它才能继续使用其它功能。
    </p>

    <form class="space-y-4" novalidate @submit.prevent="submit">
      <BaseField
        v-model="oldPassword"
        :label="forced ? '当前（初始）密码' : '当前密码'"
        type="password"
        autocomplete="current-password"
        :disabled="submitting"
        autofocus
      />

      <BaseField
        v-model="newPassword"
        label="新密码"
        type="password"
        autocomplete="new-password"
        hint="至少 8 位，含大写字母、数字与特殊字符"
        :error="newError"
        :disabled="submitting"
      />

      <BaseField
        v-model="confirm"
        label="确认新密码"
        type="password"
        autocomplete="new-password"
        :error="confirmError"
        :disabled="submitting"
      />

      <p v-if="serverError" class="sw-error" role="alert">{{ serverError }}</p>
      <p v-else-if="done" class="text-xs text-success" role="status">已修改，正在进入…</p>

      <BaseButton type="submit" block :loading="submitting" :disabled="!canSubmit">
        {{ submitting ? '提交中…' : '确认修改' }}
      </BaseButton>
    </form>
  </AuthLayout>
</template>
