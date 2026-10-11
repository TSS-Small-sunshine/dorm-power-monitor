<script setup lang="ts">
/**
 * 用户管理（5.5 / F2 / UI 改版 v2）—— 列表 / 新建 / 改角色 / 改密码 / 禁用 / 删除
 *
 * 三条护栏在后端（`starwatt/services/auth_service.py`），这里只是**提前**
 * 把它们显示出来，让用户知道为什么某个按钮点不动：
 *
 * 1. 不能禁用 / 删除自己
 * 2. 不能把最后一个管理员降级 / 禁用 / 删除
 * 3. 管理员重置密码 → 目标用户**首登必须再改一次**
 *
 * v2 只改**展示层**（``api.*`` 的调用签名、参数与返回值处理一字未动）
 * ==================================================================
 *
 * * **列表真的是张表**：补上「创建时间」一列（``created_at`` 本来就在 payload 里，
 *   旧版没显示），时间列全部用等宽数字对齐，长用户名不会把角色列挤变形。
 * * **状态从彩色小字改成胶囊**：旧版「已禁用 / 待改密 / 正常」只是一串
 *   12px 的彩色文字，色觉障碍用户区分不出 —— 现在有底色 + 图标 + 文字。
 * * **操作按钮分主次**：改密码 / 启用禁用 = ghost（普通描边），
 *   删除 = danger（柔和红底 + 红字），一眼看出哪个是不可逆的。
 * * **空列表用 ``EmptyState``**：旧版空表只有一行「加载中…」之后什么都没有。
 * * **首屏骨架屏**：旧版是一行「加载中…」，行数到达时整页跳一下。
 */
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { AdminUser } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import BaseField from '@/components/BaseField.vue'
import EmptyState from '@/components/EmptyState.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
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
    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>
    <Notice v-if="notice" tone="success" aria-live="polite">{{ notice }}</Notice>

    <!-- 新建 -->
    <section class="sw-card overflow-hidden">
      <header class="border-b border-border bg-sunken/60 px-4 py-3">
        <h2 class="sw-section-title">新建账号</h2>
        <p class="mt-0.5 text-xs text-muted">
          普通用户只能看数据；管理员可以改配置。新建的账号首次登录必须自行改密。
        </p>
      </header>

      <form class="grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-4" @submit.prevent="create">
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
            <template #icon><Icon name="plus" :size="14" /></template>
            创建
          </BaseButton>
        </div>
      </form>

      <p v-if="createError" class="sw-error px-4 pb-3" role="alert">{{ createError }}</p>
    </section>

    <!-- 列表 -->
    <section class="sw-card overflow-hidden">
      <header
        class="flex flex-wrap items-center justify-between gap-2 border-b border-border
               bg-sunken/60 px-4 py-3"
      >
        <h2 class="sw-section-title">
          用户
          <span class="text-xs font-normal text-muted">
            <span class="tabular">{{ users.length }}</span> 个
          </span>
        </h2>
        <p class="text-xs text-muted">灰掉的按钮是后端护栏不允许的操作（悬停看原因）</p>
      </header>

      <div v-if="loading" class="space-y-3 p-4" aria-busy="true" aria-live="polite">
        <div v-for="n in 3" :key="n" class="flex items-center gap-3">
          <div class="sw-skeleton h-4 w-28" />
          <div class="sw-skeleton h-4 w-16" />
          <div class="sw-skeleton h-4 w-20" />
          <div class="sw-skeleton ml-auto h-7 w-40" />
        </div>
        <span class="sr-only">正在加载用户列表…</span>
      </div>

      <EmptyState
        v-else-if="!users.length"
        title="还没有任何账号"
        description="用上面的表单创建第一个账号；至少保留一个启用中的管理员。"
        icon="users"
      />

      <div v-else class="overflow-x-auto">
        <table class="w-full text-sm">
          <caption class="sr-only">
            管理员账号列表：用户名、角色、状态、创建时间、最后登录时间，以及改密码 / 启用禁用 /
            删除三个操作
          </caption>
          <thead>
            <tr class="border-b border-border text-left text-xs text-muted">
              <th scope="col" class="px-4 py-2 font-medium">用户名</th>
              <th scope="col" class="px-3 py-2 font-medium">角色</th>
              <th scope="col" class="px-3 py-2 font-medium">状态</th>
              <th scope="col" class="px-3 py-2 font-medium">创建时间</th>
              <th scope="col" class="px-3 py-2 font-medium">最后登录</th>
              <th scope="col" class="px-4 py-2 font-medium">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="user in users"
              :key="user.id"
              class="border-b border-border last:border-0"
              :class="busy === user.id ? 'opacity-60' : ''"
            >
              <td class="px-4 py-2.5 text-content">
                <span class="font-medium">{{ user.username }}</span>
                <span v-if="user.id === auth.user?.id" class="sw-chip ml-1.5 bg-brand-soft text-brand">
                  <Icon name="user" :size="11" />
                  当前账号
                </span>
              </td>

              <td class="px-3 py-2.5">
                <select
                  :value="user.role"
                  class="sw-input w-28 py-1 text-xs"
                  :disabled="busy === user.id || lockedReason(user) !== ''"
                  :title="lockedReason(user)"
                  :aria-label="`${user.username} 的角色`"
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

              <td class="px-3 py-2.5">
                <span v-if="user.disabled" class="sw-chip bg-danger-soft text-danger">
                  <Icon name="x" :size="11" />
                  已禁用
                </span>
                <span v-else-if="user.must_change_password" class="sw-chip bg-warning-soft text-warning">
                  <Icon name="key" :size="11" />
                  待改密
                </span>
                <span v-else class="sw-chip bg-success-soft text-success">
                  <Icon name="check" :size="11" />
                  正常
                </span>
              </td>

              <td class="whitespace-nowrap px-3 py-2.5 text-xs tabular text-muted">
                {{ user.created_at ?? '—' }}
              </td>
              <td class="whitespace-nowrap px-3 py-2.5 text-xs tabular text-muted">
                {{ user.last_login_at ?? '—' }}
              </td>

              <td class="px-4 py-2.5">
                <div class="flex flex-wrap gap-1.5">
                  <BaseButton
                    variant="ghost"
                    size="sm"
                    :disabled="busy === user.id"
                    @click="resetTarget = user"
                  >
                    <template #icon><Icon name="key" :size="13" /></template>
                    改密码
                  </BaseButton>

                  <BaseButton
                    variant="ghost"
                    size="sm"
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
                    <template #icon>
                      <Icon :name="user.disabled ? 'check' : 'power'" :size="13" />
                    </template>
                    {{ user.disabled ? '启用' : '禁用' }}
                  </BaseButton>

                  <BaseButton
                    variant="danger"
                    size="sm"
                    :disabled="busy === user.id || lockedReason(user) !== ''"
                    :title="lockedReason(user)"
                    @click="removeUser(user)"
                  >
                    <template #icon><Icon name="trash" :size="13" /></template>
                    删除
                  </BaseButton>
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
      class="fixed inset-0 z-30 flex items-center justify-center bg-black/40 p-4"
      role="dialog"
      aria-modal="true"
      :aria-label="`重置 ${resetTarget.username} 的密码`"
    >
      <div class="w-full max-w-sm rounded-card border border-border bg-elevated p-5 shadow-pop">
        <h3 class="flex items-center gap-2 text-sm font-semibold text-content">
          <Icon name="key" :size="15" />
          重置 {{ resetTarget.username }} 的密码
        </h3>
        <p class="mt-1 text-xs leading-relaxed text-muted">
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
