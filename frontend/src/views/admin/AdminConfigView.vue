<script setup lang="ts">
/**
 * 配置中心（5.6 / Q15）—— 完全由 ``GET /api/admin/config/schema`` 驱动
 *
 * 验收要点（计划 §M5）
 * ====================
 *
 * * **新增一个配置项只需改后端注册表、前端零改动** —— 本文件不认识任何
 *   具体键名；分组、标签、类型、校验提示、条件显示全部来自 schema
 * * 脱敏显示（secret 不回显真值；只有改动过才提交）
 * * 条件显示（``depends_on``：父开关关了就不显示子项，避免改了没用）
 * * 重置（``POST /config/reset``，会二次确认）
 * * 导入导出（导出可含/不含明文 secret；导入合并语义，脱敏值后端自动跳过）
 * * 保存后逐键回显错误（207 部分成功）
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError, api } from '@/api/client'
import type { ConfigGroup, ConfigImportResult, ConfigSchemaPayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import ConfigField from '@/components/config/ConfigField.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

const schema = ref<ConfigSchemaPayload | null>(null)
const values = ref<Record<string, unknown>>({})
const edited = ref<Record<string, unknown>>({})
const errors = ref<Record<string, string>>({})

const loading = ref(true)
const saving = ref(false)
const notice = ref('')
const fatal = ref('')
const showAdvanced = ref(false)
const importing = ref(false)
/** 导入时是否接受**明文凭据**（整机迁移用；默认拒绝，防别人灌凭据进来） */
const acceptSecrets = ref(false)

/** 有未保存改动时才允许保存 */
const dirtyKeys = computed(() => Object.keys(edited.value))

/**
 * 条件显示：``depends_on`` 指向的键为假 → 本项不显示。
 *
 * 为什么用「已保存的值」而不是「正在编辑的值」：用户关掉父开关后子项
 * 立刻消失会让人以为配置丢了；保存后再隐藏更符合直觉。
 */
function visible(group: ConfigGroup) {
  return group.settings.filter((setting) => {
    if (setting.depends_on) {
      const parent = values.value[setting.depends_on]
      if (parent !== true && parent !== '1' && parent !== 1) return false
    }
    if (setting.advanced && !showAdvanced.value) return false
    return true
  })
}

function valueOf(key: string): unknown {
  return key in edited.value ? edited.value[key] : values.value[key]
}

function onEdit(key: string, value: unknown): void {
  edited.value = { ...edited.value, [key]: value }
  if (errors.value[key]) {
    const next = { ...errors.value }
    delete next[key]
    errors.value = next
  }
  notice.value = ''
}

async function load(): Promise<void> {
  loading.value = true
  fatal.value = ''
  try {
    const [schemaPayload, valuePayload] = await Promise.all([
      api.configSchema(),
      api.configValues(),
    ])
    schema.value = schemaPayload
    values.value = valuePayload.values
    edited.value = {}
    errors.value = {}
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '配置加载失败'
  } finally {
    loading.value = false
  }
}

async function save(): Promise<void> {
  if (!dirtyKeys.value.length || saving.value) return
  saving.value = true
  notice.value = ''
  errors.value = {}
  try {
    const result = await api.configUpdate(edited.value)
    errors.value = result.errors ?? {}
    const failed = Object.keys(errors.value).length
    if (result.applied.length) {
      notice.value = `已保存 ${result.applied.length} 项${failed ? `，${failed} 项未通过校验` : ''}`
      // 重新拉一遍：服务端可能规范化了值（trim / 四舍五入）
      values.value = (await api.configValues()).values
      // 只清掉「已生效」的编辑，失败项保留给用户改
      const rest: Record<string, unknown> = {}
      for (const key of Object.keys(edited.value)) {
        if (!result.applied.includes(key)) rest[key] = edited.value[key]
      }
      edited.value = rest
    }
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '保存失败'
  } finally {
    saving.value = false
  }
}

async function reset(): Promise<void> {
  if (!window.confirm('确定把所有配置恢复成默认值吗？运行时状态（抓取进度等）不受影响。')) return
  try {
    const result = await api.configReset()
    notice.value = `已恢复默认值（${result.reset} 项）`
    await load()
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '重置失败'
  }
}

/** 导出：默认不含明文 secret（浏览器里带明文凭据太危险） */
async function exportConfig(includeSecrets: boolean): Promise<void> {
  if (includeSecrets && !window.confirm('导出将包含飞书 / QQ 的明文凭据，文件请勿外传。继续？')) {
    return
  }
  try {
    const payload = await api.configExport(includeSecrets)
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `starwatt-config-${payload.exported_at.replace(/[: ]/g, '-')}.json`
    link.click()
    URL.revokeObjectURL(url)
    notice.value = includeSecrets ? '已导出（含明文凭据）' : '已导出（secret 已脱敏）'
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof Error ? err.message : '导出失败'
  }
}

async function importConfig(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return

  // 明文凭据默认**拒绝**（后端 allow_secrets 默认 false）——
  // 整机迁移时才显式打开，并且再确认一次
  if (
    acceptSecrets.value &&
    !window.confirm('这次导入会写入文件里的明文凭据（openid / 飞书 / QQ）。确认继续？')
  ) {
    input.value = ''
    return
  }

  importing.value = true
  fatal.value = ''
  try {
    const parsed = JSON.parse(await file.text())
    const result: ConfigImportResult = await api.configImport(parsed, acceptSecrets.value)
    const failed = Object.keys(result.errors).length
    notice.value =
      `导入完成：生效 ${result.applied.length} 项` +
      `、跳过 ${result.skipped.length} 项（脱敏值 / 未知键 / 运行时状态${
        acceptSecrets.value ? '' : ' / 明文凭据'
      }）` +
      (failed ? `、失败 ${failed} 项` : '')
    errors.value = result.errors ?? {}
    await load()
  } catch (err) {
    if (auth.handle(err)) return
    fatal.value = err instanceof ApiError ? err.message : '导入失败：文件不是合法 JSON 或结构不符'
  } finally {
    importing.value = false
    input.value = '' // 同一个文件可以再选一次
  }
}

onMounted(load)
</script>

<template>
  <div class="space-y-4">
    <!-- 工具条 -->
    <div class="sw-card flex flex-wrap items-center justify-between gap-3 p-4">
      <div>
        <h2 class="text-sm font-semibold text-content">配置中心</h2>
        <p class="text-xs text-muted">
          {{ schema?.total ?? 0 }} 个配置项 · 逐项独立校验，失败项会保留下来让你修正
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <label class="flex items-center gap-1.5 text-xs text-muted">
          <input v-model="showAdvanced" type="checkbox" class="h-3.5 w-3.5" />
          显示高级项
        </label>
        <label
          class="flex items-center gap-1.5 text-xs text-muted"
          title="整机迁移用：勾选后导入会写入文件里的明文凭据；不勾选时明文凭据一律被拒绝"
        >
          <input v-model="acceptSecrets" type="checkbox" class="h-3.5 w-3.5" />
          导入接受凭据
        </label>
        <BaseButton variant="ghost" @click="exportConfig(false)">导出</BaseButton>
        <BaseButton variant="ghost" @click="exportConfig(true)">导出（含凭据）</BaseButton>
        <label
          class="cursor-pointer rounded-control border border-border px-4 py-2 text-sm text-content
                 transition hover:bg-surface"
        >
          {{ importing ? '导入中…' : '导入' }}
          <input type="file" accept="application/json,.json" class="hidden" @change="importConfig" />
        </label>
        <BaseButton variant="ghost" @click="reset">恢复默认</BaseButton>
        <BaseButton :loading="saving" :disabled="!dirtyKeys.length" @click="save">
          保存{{ dirtyKeys.length ? `（${dirtyKeys.length}）` : '' }}
        </BaseButton>
      </div>
    </div>

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

    <p v-if="loading" class="py-10 text-center text-sm text-muted">正在加载配置…</p>

    <!-- 分组表单：分组与字段全部来自 schema（新增配置项前端零改动） -->
    <section v-for="group in schema?.groups ?? []" :key="group.key" class="sw-card p-4">
      <h3 class="mb-1 text-sm font-semibold text-content">{{ group.label }}</h3>
      <p class="mb-2 text-xs text-muted">{{ group.key }} · 显示 {{ visible(group).length }} 项</p>

      <ConfigField
        v-for="setting in visible(group)"
        :key="setting.key"
        :setting="setting"
        :value="valueOf(setting.key)"
        :error="errors[setting.key]"
        @update:model-value="(v) => onEdit(setting.key, v)"
      />

      <p v-if="!visible(group).length" class="py-3 text-xs text-muted">
        这一组的项都被隐藏了（依赖的开关未开启，或都是高级项）。
      </p>
    </section>
  </div>
</template>
