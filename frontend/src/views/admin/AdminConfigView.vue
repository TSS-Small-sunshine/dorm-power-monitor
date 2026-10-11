<script setup lang="ts">
/**
 * 配置中心（5.6 / Q15 / UI 改版 v2）—— 完全由 ``GET /api/admin/config/schema`` 驱动
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
 *
 * v2 只改**展示层**（``api.*`` 的调用签名、参数、返回值处理与上面每一条行为一字未动）
 * ==============================================================================
 *
 * * **保存栏常驻在视口底部**：旧版保存按钮在页面顶部工具条里 ——
 *   94 个字段往下翻，改完最后一项要一路滚回顶部才能保存，这是这一页最大的
 *   可用性问题。现在保存栏贴着视口底边（毛玻璃、不遮内容），
 *   任何时候都够得着，并实时显示「已修改 N 项」。
 * * **分组卡片化**：组头写组名 + ``已显示 / 总数`` 胶囊，9 个分组一眼能数清。
 * * **字段行两列布局**（见 ``ConfigField``）：左「是什么」右「控件」。
 * * **复选框有明确选中态**：勾上时整块变品牌柔底 + 品牌描边，
 *   不再只靠一个 3.5px 的方框里的小勾。
 * * **动作分主次**：保存 = primary，导出 / 导入 = ghost，恢复默认 = danger。
 * * **提示条统一走 ``Notice``**：danger 自带 ``role="alert"``，
 *   成功与失败项汇总走 ``role="status"``（``aria-live``）。
 * * **首屏骨架屏**：旧版是一行「正在加载配置…」，内容到达时整页跳一下。
 */
import { computed, onMounted, ref } from 'vue'

import { ApiError, api } from '@/api/client'
import type { ConfigGroup, ConfigImportResult, ConfigSchemaPayload } from '@/api/types'
import BaseButton from '@/components/BaseButton.vue'
import Icon from '@/components/Icon.vue'
import Notice from '@/components/Notice.vue'
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

/** 校验失败项数（来自 PUT / import 的 207 响应）—— 只用于汇总提示 */
const errorCount = computed(() => Object.keys(errors.value).length)

/** 分组数（只用于标题副行） */
const groupCount = computed(() => (schema.value?.groups ?? []).length)

/** 高级项总数（勾选框旁边标出「打开后多几项」） */
const advancedCount = computed(() =>
  (schema.value?.groups ?? []).reduce(
    (total, group) => total + group.settings.filter((setting) => setting.advanced).length,
    0,
  ),
)

/** 顶栏 / 保存栏里的次级按钮样式（与 BaseButton ghost sm 一致） */
const GHOST_ACTION =
  'inline-flex items-center gap-1.5 rounded-control border border-border bg-elevated px-2.5 py-1.5 ' +
  'text-xs font-medium text-content transition duration-150 hover:border-border-strong hover:bg-surface'

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
    <Notice v-if="fatal" tone="danger">{{ fatal }}</Notice>
    <Notice v-if="notice" tone="success" aria-live="polite">{{ notice }}</Notice>
    <Notice v-if="errorCount" tone="warning" aria-live="polite">
      有 <strong class="tabular">{{ errorCount }}</strong> 项没通过校验：已在下面对应字段标出中文原因，
      改完再点一次保存即可（其余项已生效）。
    </Notice>

    <!-- 工具条：说明 + 导入导出（保存不在这里，见页面底部的常驻保存栏） -->
    <section class="sw-card p-4">
      <div class="flex flex-wrap items-start justify-between gap-3">
        <div class="min-w-0">
          <h2 class="sw-section-title">配置中心</h2>
          <p class="mt-0.5 text-xs text-muted">
            <span class="tabular">{{ schema?.total ?? 0 }}</span> 个配置项 ·
            <span class="tabular">{{ groupCount }}</span> 个分组 · 逐项独立校验，失败项会保留下来让你修正
          </p>
        </div>

        <div class="flex flex-wrap items-center gap-2">
          <BaseButton variant="ghost" size="sm" @click="exportConfig(false)">
            <template #icon><Icon name="download" :size="14" /></template>
            导出
          </BaseButton>
          <BaseButton variant="ghost" size="sm" @click="exportConfig(true)">
            <template #icon><Icon name="download" :size="14" /></template>
            导出（含凭据）
          </BaseButton>

          <label
            :class="[GHOST_ACTION, 'cursor-pointer', importing ? 'pointer-events-none opacity-60' : '']"
          >
            <Icon :name="importing ? 'refresh' : 'upload'" :size="14" />
            {{ importing ? '导入中…' : '导入' }}
            <input
              type="file"
              accept="application/json,.json"
              class="hidden"
              :disabled="importing"
              @change="importConfig"
            />
          </label>

          <BaseButton variant="danger" size="sm" @click="reset">
            <template #icon><Icon name="reset" :size="14" /></template>
            恢复默认
          </BaseButton>
        </div>
      </div>

      <!-- 显示选项：勾上时整块变成品牌柔底，选中态一眼可见 -->
      <div class="mt-3 flex flex-wrap items-center gap-2 border-t border-border pt-3">
        <label
          class="inline-flex cursor-pointer items-center gap-2 rounded-control border px-2.5 py-1.5
                 text-xs font-medium transition duration-150"
          :class="
            showAdvanced
              ? 'border-brand/40 bg-brand-soft text-brand'
              : 'border-border bg-elevated text-muted hover:border-border-strong'
          "
        >
          <input v-model="showAdvanced" type="checkbox" class="h-4 w-4 accent-brand" />
          显示高级项
          <span class="tabular" :class="showAdvanced ? 'text-brand' : 'text-muted/70'">
            {{ advancedCount }}
          </span>
        </label>

        <label
          class="inline-flex cursor-pointer items-center gap-2 rounded-control border px-2.5 py-1.5
                 text-xs font-medium transition duration-150"
          :class="
            acceptSecrets
              ? 'border-warning/40 bg-warning-soft text-warning'
              : 'border-border bg-elevated text-muted hover:border-border-strong'
          "
          title="整机迁移用：勾选后导入会写入文件里的明文凭据；不勾选时明文凭据一律被拒绝"
        >
          <input v-model="acceptSecrets" type="checkbox" class="h-4 w-4 accent-warning" />
          导入接受凭据
        </label>

        <p class="text-xs text-muted">
          「导入接受凭据」默认关闭 —— 打开后导入会写入文件里的明文凭据（openid / 飞书 / QQ）。
        </p>
      </div>
    </section>

    <!-- 首屏骨架：把布局先占住，数据到达时不会整页跳 -->
    <div v-if="loading" class="space-y-4" aria-busy="true" aria-live="polite">
      <div v-for="n in 3" :key="n" class="sw-card p-4">
        <div class="sw-skeleton h-4 w-28" />
        <div class="mt-4 space-y-5">
          <div
            v-for="m in 3"
            :key="m"
            class="grid gap-x-6 gap-y-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,18rem)]"
          >
            <div>
              <div class="sw-skeleton h-3.5 w-40" />
              <div class="sw-skeleton mt-2 h-3 w-56" />
            </div>
            <div class="sw-skeleton h-9 w-full" />
          </div>
        </div>
      </div>
      <span class="sr-only">正在加载配置…</span>
    </div>

    <!-- 分组表单：分组与字段全部来自 schema（新增配置项前端零改动） -->
    <template v-else>
      <section
        v-for="group in schema?.groups ?? []"
        :key="group.key"
        class="sw-card overflow-hidden"
      >
        <header
          class="flex flex-wrap items-center justify-between gap-2 border-b border-border
                 bg-sunken/60 px-4 py-3"
        >
          <div class="flex min-w-0 flex-wrap items-center gap-2">
            <h3 class="sw-section-title truncate">{{ group.label }}</h3>
            <span class="sw-chip bg-elevated text-muted">
              <span class="tabular">{{ visible(group).length }}</span>
              /
              <span class="tabular">{{ group.settings.length }}</span>
              项
            </span>
          </div>
          <code class="rounded bg-elevated px-1.5 py-0.5 font-mono text-[11px] text-muted">
            {{ group.key }}
          </code>
        </header>

        <div class="px-4">
          <ConfigField
            v-for="setting in visible(group)"
            :key="setting.key"
            :setting="setting"
            :value="valueOf(setting.key)"
            :error="errors[setting.key]"
            @update:model-value="(v) => onEdit(setting.key, v)"
          />

          <p v-if="!visible(group).length" class="py-4 text-xs text-muted">
            这一组的项都被隐藏了（依赖的开关未开启，或都是高级项）。
          </p>
        </div>
      </section>
    </template>

    <!--
      常驻保存栏：贴着视口底边（毛玻璃，不硬切页面）。
      旧版的保存按钮只在页面顶部，改完最后一个字段要滚回顶部才能保存。
    -->
    <div class="sw-glass sticky bottom-0 z-10 -mx-4 border-t border-border px-4 py-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <p class="text-xs text-muted">
          <template v-if="dirtyKeys.length">
            已修改
            <strong class="tabular text-content">{{ dirtyKeys.length }}</strong>
            项，尚未保存
          </template>
          <template v-else>没有未保存的改动</template>
          <span v-if="errorCount" class="ml-1 text-warning">
            · {{ errorCount }} 项待修正
          </span>
        </p>

        <BaseButton :loading="saving" :disabled="!dirtyKeys.length" @click="save">
          <template #icon><Icon name="save" :size="14" /></template>
          保存{{ dirtyKeys.length ? `（${dirtyKeys.length}）` : '' }}
        </BaseButton>
      </div>
    </div>
  </div>
</template>
