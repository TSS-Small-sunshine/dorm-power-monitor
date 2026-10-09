<script setup lang="ts">
/**
 * 忘记密码（5.3 第 6 个认证界面）
 *
 * 为什么这里只有「怎么在服务器上重置」而没有表单
 * ============================================
 *
 * legacy 有一个**无认证**的 ``/admin/set-password``：任何能访问到页面的人都能
 * 改管理员密码（缺陷 L20 / 审计分歧点 1）。重写把它删了，且**不会再做**一个
 * 「输入用户名就能重置」的网页入口 —— 个人项目没有短信/邮件通道，那种入口
 * 等于把后台钥匙挂在门口。
 *
 * 所以这条路的正确形态是：**在服务器上执行一条命令**（能 SSH 就说明身份可信）。
 * 本页面负责把命令写清楚，而不是提供一个假的「在线找回」。
 */
import { ref } from 'vue'

import AuthLayout from '@/components/AuthLayout.vue'

const copied = ref('')

const COMMAND = `python - <<'PY'
from starwatt.db.connection import init
from starwatt.auth import service as auth
init()
auth.create_user("admin2", "换成你的强密码", role="admin")
print("已创建新管理员 admin2：登录后在「管理 → 用户」里改密码或删掉旧账号")
PY`

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(COMMAND)
    copied.value = '已复制到剪贴板'
  } catch {
    copied.value = '复制失败，请手动选中上面的命令'
  }
}
</script>

<template>
  <AuthLayout title="忘记密码" subtitle="密码只能在服务器上重置（这是有意的）">
    <div class="space-y-4 text-sm">
      <p class="text-muted">
        这个系统没有、也不会有「网页上输入用户名就能重置密码」的入口 ——
        那等于把后台钥匙挂在门口。能登录服务器，就说明身份可信。
      </p>

      <div>
        <p class="mb-1.5 text-xs font-medium text-content">
          在服务器上（项目目录内）执行：
        </p>
        <pre
          class="overflow-x-auto rounded-control bg-surface p-3 text-xs leading-relaxed text-content"
        >{{ COMMAND }}</pre>
        <button
          type="button"
          class="mt-2 rounded-control border border-border px-3 py-1.5 text-xs text-content transition hover:bg-surface"
          @click="copy"
        >
          复制命令
        </button>
        <p v-if="copied" class="sw-hint" role="status">{{ copied }}</p>
      </div>

      <p class="text-xs text-muted">
        更简单的办法：如果你还有另一个管理员账号，用它登录后在
        「管理 → 用户 → 改密码」里重置。实在都不行了，删除
        <code class="rounded bg-surface px-1">records.db</code> 重新首启也可以
        （会丢失历史数据，请先备份）。
      </p>

      <RouterLink
        to="/login"
        class="block w-full rounded-control bg-brand px-4 py-2 text-center text-sm font-medium text-white transition hover:brightness-110"
      >
        返回登录
      </RouterLink>
    </div>
  </AuthLayout>
</template>
