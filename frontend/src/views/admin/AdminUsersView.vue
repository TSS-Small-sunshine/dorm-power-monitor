<script setup lang="ts">
/**
 * 用户管理（5.5 / F2）—— 列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除
 *
 * 三条护栏在后端（`starwatt/services/auth_service.py`），这里只是**提前**
 * 把它们显示出来，让用户知道为什么某个按钮点不动：
 *
 * 1. 不能禁用 / 删除自己
 * 2. 不能把最后一个管理员降级 / 禁用 / 删除
 * 3. 管理员重置密码 → 目标用户**首登必须再改一次**
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { AdminUser } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const users = ref<AdminUser[]>([])
const loading = ref(true)
const busy = ref<number | null>(null)
const fatal = ref('')
const notice = ref('')

// 新建表单
const newUsername = ref('')
const newPassword = ref('')
const newRole = ref<'viewer' | 'admin'>('viewer')
const creating = ref(false)
const createError = ref('')

// 重置密码
const resetTarget = ref<AdminUser | null>(null)
const resetPassword = ref('')
const resetting = ref(false)

const adminCount = computed(
  () => users.value.filter((u) => u.role === 'admin' && !u.disabled).length,
)

/** 护栏 ①/②：这些用户不能被「禁用 / 删除 / 降级」 */
function lockedReason(user: AdminUser): string {
  if (user.id === auth.user?.id) return '不能操作当前登录的账号'
  if (user.role === 'admin' && adminCount.value <= 1) return '最后一个管理员'
  return ''
}

async function load(): Promise<void> {
  loading.value = true
  fatal.value = ''
  try {
    users.value = (await api.users()).users
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function run(user: AdminUser, action: () => Promise<unknown>, message: string): Promise<void> {
  busy.value = user.id
  fatal.value = ''
  notice.value = ''
  try {
    await action()
    notice.value = message
    await load()
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '操作失败'
  } finally {
    busy.value = null
  }
}

async function create(): Promise<void> {
  creating.value = true
  createError.value = ''
  try {
    await api.createUser({
      username: newUsername.value.trim(),
      password: newPassword.value,
      role: newRole.value,
    })
    notice.value = `已创建 ${newUsername.value.trim()}`
    newUsername.value = ''
    newPassword.value = ''
    await load()
  } catch (err) {
    if (auth.handle(err)) return
    createError.value = err instanceof Error ? err.message : '创建失败'
  } finally {
    creating.value = false
  }
}

/** 删除前二次确认（模板里拿不到 ``window``，所以放在脚本里） */
function removeUser(user: AdminUser): void {
  if (!window.confirm(`删除 ${user.username}？该操作不可撤销。`)) return
  void run(user, () => api.deleteUser(user.id), `已删除 ${user.username}`)
}

async function submitReset(): Promise<void> {
  if (!resetTarget.value) return
  resetting.value = true
  try {
    await api.resetUserPassword(resetTarget.value.id, resetPassword.value)
    notice.value = `已重置 ${resetTarget.value.username} 的密码（该用户下次登录须改密）`
    resetTarget.value = null
    resetPassword.value = ''
    await load()
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '重置失败'
  } finally {
    resetting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <p v-if="fatal" class="rounded-control bg-danger/10 px-3 py-2 text-sm text-danger" role="alert">
      {{ fatal }}
    </p>
    <p
      v-if="notice"
      class="rounded-control bg-success/10 px-3 py-2 text-sm text-success"
      role="status"
    >
      {{ notice }}
    </p>

    <!-- 新建 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">新建账号</h2>
      <form class="grid gap-3 sm:grid-cols-4" @submit.prevent="create">
        <BaseField v-model="newUsername" label="用户名" :disabled="creating" />
        <BaseField
          v-model="newPassword"
          label="初始密码"
          type="password"
          hint="≥8 位 + 大写 + 数字 + 特殊字符"
          :disabled="creating"
        />
        <label class="block">
          <span class="sw-label">角色</span>
          <select v-model="newRole" class="sw-input" :disabled="creating">
            <option value="viewer">普通用户（只看数据）</option>
            <option value="admin">管理员（可改配置）</option>
          </select>
        </label>
        <div class="flex items-end">
          <BaseButton
            type="submit"
            block
            :loading="creating"
            :disabled="!newUsername.trim() || !newPassword"
          >
            创建
          </BaseButton>
        </div>
      </form>
      <p v-if="createError" class="sw-error" role="alert">{{ createError }}</p>
    </section>

    <!-- 列表 -->
    <section class="sw-card p-4">
      <h2 class="mb-3 text-sm font-semibold text-content">
        用户 <span class="text-xs font-normal text-muted">{{ users.length }} 个</span>
      </h2>

      <p v-if="loading" class="py-6 text-center text-sm text-muted">加载中…</p>

      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <thead>
            <tr class="border-b border-border text-left text-xs text-muted">
              <th class="py-2 pr-3 font-medium">用户名</th>
              <th class="py-2 pr-3 font-medium">角色</th>
              <th class="py-2 pr-3 font-medium">状态</th>
              <th class="py-2 pr-3 font-medium">最后登录</th>
              <th class="py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="user in users" :key="user.id" class="border-b border-border/60 last:border-0">
              <td class="py-2 pr-3 text-content">
                {{ user.username }}
                <span v-if="user.id === auth.user?.id" class="ml-1 text-xs text-muted">(我)</span>
              </td>
              <td class="py-2 pr-3">
                <select
                  :value="user.role"
                  class="sw-input w-28 py-1 text-xs"
                  :disabled="busy === user.id || lockedReason(user) !== ''"
                  :title="lockedReason(user)"
                  @change="
                    (e) =>
                      run(
                        user,
                        () =>
                          api.updateUser(user.id, { role: (e.target as HTMLSelectElement).value }),
                        `已更新 ${user.username} 的角色`,
                      )
                  "
                >
                  <option value="viewer">普通</option>
                  <option value="admin">管理员</option>
                </select>
              </td>
              <td class="py-2 pr-3">
                <span v-if="user.disabled" class="text-xs text-danger">已禁用</span>
                <span v-else-if="user.must_change_password" class="text-xs text-warning">待改密</span>
                <span v-else class="text-xs text-success">正常</span>
              </td>
              <td class="py-2 pr-3 text-xs text-muted">{{ user.last_login_at ?? '—' }}</td>
              <td class="py-2">
                <div class="flex flex-wrap gap-1.5">
                  <button
                    type="button"
                    class="rounded-control border border-border px-2 py-1 text-xs text-content transition hover:bg-surface disabled:opacity-50"
                    :disabled="busy === user.id"
                    @click="resetTarget = user"
                  >
                    改密码
                  </button>
                  <button
                    type="button"
                    class="rounded-control border border-border px-2 py-1 text-xs text-content transition hover:bg-surface disabled:opacity-50"
                    :disabled="busy === user.id || lockedReason(user) !== ''"
                    :title="lockedReason(user)"
                    @click="
                      run(
                        user,
                        () => api.updateUser(user.id, { disabled: !user.disabled }),
                        user.disabled ? `已启用 ${user.username}` : `已禁用 ${user.username}`,
                      )
                    "
                  >
                    {{ user.disabled ? '启用' : '禁用' }}
                  </button>
                  <button
                    type="button"
                    class="rounded-control border border-danger/40 px-2 py-1 text-xs text-danger transition hover:bg-danger/10 disabled:opacity-50"
                    :disabled="busy === user.id || lockedReason(user) !== ''"
                    :title="lockedReason(user)"
                    @click="removeUser(user)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <!-- 重置密码弹层 -->
    <div
      v-if="resetTarget"
      class="fixed inset-0 z-10 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      :aria-label="`重置 ${resetTarget.username} 的密码`"
    >
      <div class="w-full max-w-sm rounded-card border border-border bg-elevated p-5 shadow-card">
        <h3 class="text-sm font-semibold text-content">重置 {{ resetTarget.username }} 的密码</h3>
        <p class="mt-1 text-xs text-muted">
          该用户的登录会立即失效，且下次登录必须再改一次密码。
        </p>
        <form class="mt-4 space-y-3" @submit.prevent="submitReset">
          <BaseField v-model="resetPassword" label="新密码" type="password" :disabled="resetting" />
          <div class="flex gap-2">
            <BaseButton type="submit" block :loading="resetting" :disabled="!resetPassword">
              确认重置
            </BaseButton>
            <BaseButton variant="ghost" block @click="resetTarget = null">取消</BaseButton>
          </div>
        </form>
      </div>
    </div>
  </div>
</template>
