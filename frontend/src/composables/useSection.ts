/**
 * 四段式导航的「当前段」（E1）
 *
 * legacy 用 `localStorage["dorm-power-monitor.section"]` 记住用户在哪一段 ——
 * 刷新页面后不该把他踢回「概览」。这里保持**同一个键名**，这样从旧版升级
 * 上来的用户（浏览器里已经有这个键）不会感觉被重置。
 */
import { ref, watch } from 'vue'

export type SectionKey = 'overview' | 'history' | 'violations' | 'meter'

export const SECTIONS: { key: SectionKey; label: string; icon: string }[] = [
  { key: 'overview', label: '概览', icon: '📈' },
  { key: 'history', label: '历史', icon: '📅' },
  { key: 'violations', label: '违规', icon: '⚠' },
  { key: 'meter', label: '电表', icon: '🔌' },
]

//: 与 legacy 完全一致的存储键（升级不丢用户习惯）
const STORAGE_KEY = 'dorm-power-monitor.section'

const VALID = new Set<string>(SECTIONS.map((s) => s.key))

function initial(): SectionKey {
  const saved = localStorage.getItem(STORAGE_KEY)
  return saved && VALID.has(saved) ? (saved as SectionKey) : 'overview'
}

/** 当前段（模块级单例 —— 段切换是全局状态，不该每处各存一份） */
const current = ref<SectionKey>(initial())

watch(current, (value) => localStorage.setItem(STORAGE_KEY, value))

export function useSection() {
  return { section: current, sections: SECTIONS }
}
