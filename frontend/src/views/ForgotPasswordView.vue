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
 *
 * v2 改动（只动展示层）
 * ===================
 *
 * * 那段「为什么没有网页重置」的说明从裸文字换成 `Notice`：它是这个页面的
 *   **结论**，不是背景噪音，值得一块底色
 * * 命令块加 `tabindex="0"`：横向滚动区键盘也必须能滚（否则窄屏 + 纯键盘
 *   用户根本看不到命令的后半段）
 * * 「复制命令」换成 `BaseButton`（带复制图标），结果提示带 `aria-live`
 * * 底部链接改回 `text-brand-ink` —— 旧版写死 `text-white`，用户把主题色
 *   改成浅色时白字会看不见
 */
import { ref } from 'vue'

import AuthLayout from '@/components/AuthLayout.vue'
import BaseButton from '@/components/BaseButton.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'

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
      <Notice tone="info" icon="shield">
        这个系统没有、也不会有「网页上输入用户名就能重置密码」的入口 ——
        那等于把后台钥匙挂在门口。能登录服务器，就说明身份可信。
      </Notice>

      <div>
        <p class="mb-1.5 text-xs font-medium text-content">
          在服务器上（项目目录内）执行：
        </p>
        <pre
          class="overflow-x-auto rounded-control border border-border bg-sunken p-3 font-mono
                 text-xs leading-relaxed text-content"
          tabindex="0"
        >{{ COMMAND }}</pre>

        <div class="mt-2 flex flex-wrap items-center gap-2">
          <BaseButton variant="ghost" size="sm" @click="copy">
            <template #icon><Icon name="copy" :size="14" /></template>
            复制命令
          </BaseButton>
          <p v-if="copied" class="text-xs text-muted" role="status" aria-live="polite">
            {{ copied }}
          </p>
        </div>
      </div>

      <p class="text-xs leading-relaxed text-muted">
        更简单的办法：如果你还有另一个管理员账号，用它登录后在
        「管理 → 用户 → 改密码」里重置。实在都不行了，删除
        <code class="rounded bg-sunken px-1">records.db</code> 重新首启也可以
        （会丢失历史数据，请先备份）。
      </p>

      <RouterLink
        to="/login"
        class="flex w-full items-center justify-center gap-1.5 rounded-control bg-brand px-4 py-2
               text-sm font-medium text-brand-ink transition hover:bg-brand-strong"
      >
        <Icon name="arrowLeft" :size="16" />
        返回登录
      </RouterLink>
    </div>
  </AuthLayout>
</template>
