/**
 * 主题（M5 §5.2）—— 品牌色注入 + 深浅色切换
 *
 * 品牌色为什么不写死在 CSS 里
 * ==========================
 *
 * 「配置中心 → 站点 → 主题色」(`theme_color`) 是**运行时**配置。前端把它转成
 * `--color-brand`（RGB 通道三元组）写进 `<html>` 的 inline style，
 * 于是**不重新构建**也能生效（`tokens.css` 里那个值只是默认值）。
 *
 * 深浅色
 * ======
 *
 * `html.dark` + `localStorage`：用户手动切换优先，没选过就跟随系统
 * （`prefers-color-scheme`）。用 class 而不是纯 media query，是因为
 * 「跟随系统」与「用户选择」是两回事 —— 用户选了就必须记住。
 */

const THEME_KEY = 'starwatt.theme'
const DARK_KEY = 'starwatt.dark'

export type ThemeChoice = 'light' | 'dark' | 'system'

/** `#1677ff` / `1677ff` / `#abc` → `22 119 255`（Tailwind 的 alpha 语法要用通道） */
export function hexToRgbChannels(hex: string): string | null {
  const raw = hex.trim().replace(/^#/, '')
  const full = raw.length === 3 ? raw.replace(/(.)/g, '$1$1') : raw
  if (!/^[0-9a-fA-F]{6}$/.test(full)) return null

  const value = Number.parseInt(full, 16)
  return `${(value >> 16) & 255} ${(value >> 8) & 255} ${value & 255}`
}

/** 把品牌色写进 CSS 变量（非法值直接忽略，保留 CSS 里的默认色） */
export function applyBrandColor(themeColor: string): void {
  const channels = hexToRgbChannels(themeColor)
  if (!channels) return
  document.documentElement.style.setProperty('--color-brand', channels)
}

export function currentTheme(): ThemeChoice {
  const saved = localStorage.getItem(THEME_KEY)
  return saved === 'light' || saved === 'dark' ? saved : 'system'
}

function systemPrefersDark(): boolean {
  return window.matchMedia('(prefers-color-scheme: dark)').matches
}

export function isDark(): boolean {
  const choice = currentTheme()
  if (choice === 'system') return systemPrefersDark()
  return choice === 'dark'
}

export function applyTheme(choice: ThemeChoice = currentTheme()): void {
  if (choice === 'system') {
    localStorage.removeItem(THEME_KEY)
  } else {
    localStorage.setItem(THEME_KEY, choice)
  }
  document.documentElement.classList.toggle('dark', isDark())
  localStorage.setItem(DARK_KEY, isDark() ? '1' : '0')
}

/** 在应用挂载前调用一次：先定深浅色，避免首帧闪白 */
export function initTheme(): void {
  applyTheme()
  window
    .matchMedia('(prefers-color-scheme: dark)')
    .addEventListener('change', () => {
      if (currentTheme() === 'system') applyTheme('system')
    })
}
