/**
 * 主题（M5 §5.2 / UI 改版 v2）—— 品牌色注入 + 深浅色切换
 *
 * 品牌色为什么不写死在 CSS 里
 * ==========================
 *
 * 「配置中心 → 站点 → 主题色」(`theme_color`) 是**运行时**配置。前端把它转成
 * `--color-brand`（RGB 通道三元组）写进 `<html>` 的 inline style，
 * 于是**不重新构建**也能生效（`tokens.css` 里那个值只是默认值）。
 *
 * v2 追加：品牌色不能只管一个变量
 * ==============================
 *
 * 旧版只设 `--color-brand`，于是按钮是 `bg-brand text-white` 写死的白字。
 * 用户把主题色改成浅色（比如 #93C5FD）之后，白字压在浅蓝上 —— 对比度不到 2:1，
 * 按钮上的字几乎看不见（docs/screenshots/01-login.png 就是这个状态）。
 *
 * 所以现在一次算三个变量：
 *
 * * `--color-brand`        —— 原色
 * * `--color-brand-ink`    —— **在这个底色上读得清**的前景色（按相对亮度选黑/白）
 * * `--color-brand-strong` —— 悬停/按下色（浅色主题压深、深色主题提亮）
 *
 * 深浅色切换后 `brand-strong` 的方向要反过来，所以 `applyTheme()` 会**重算一次**
 * 品牌色 —— 原始十六进制值存在 `data-brand` 上，不额外占 localStorage。
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

/** WCAG 相对亮度（sRGB 线性化后加权）—— 只用来决定「黑字还是白字」 */
function luminance(r: number, g: number, b: number): number {
  const channel = (value: number): number => {
    const v = value / 255
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** 品牌底上的前景色：亮度高于阈值就用深色字，否则用白字 */
function readableInk(r: number, g: number, b: number): string {
  return luminance(r, g, b) > 0.55 ? '12 18 28' : '255 255 255'
}

/** 按比例调整明度：`amount` 为负 = 压深，为正 = 提亮 */
function shift(r: number, g: number, b: number, amount: number): string {
  const mix = (value: number): number =>
    Math.max(0, Math.min(255, Math.round(value + (amount > 0 ? 255 - value : value) * amount)))
  return `${mix(r)} ${mix(g)} ${mix(b)}`
}

/**
 * 把品牌色写进 CSS 变量（非法值直接忽略，保留 CSS 里的默认色）。
 *
 * 同时算好可读前景与悬停色 —— 见文件头「v2 追加」那一节。
 */
export function applyBrandColor(themeColor: string): void {
  const channels = hexToRgbChannels(themeColor)
  if (!channels) return

  const root = document.documentElement
  const [r, g, b] = channels.split(' ').map(Number) as [number, number, number]
  const dark = root.classList.contains('dark')

  root.style.setProperty('--color-brand', channels)
  root.style.setProperty('--color-brand-ink', readableInk(r, g, b))
  root.style.setProperty('--color-brand-strong', shift(r, g, b, dark ? 0.18 : -0.14))
  // 原始值留着：切换深浅色时要按新方向重算 strong
  root.dataset.brand = themeColor
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
  const root = document.documentElement
  root.classList.toggle('dark', isDark())
  localStorage.setItem(DARK_KEY, isDark() ? '1' : '0')

  // 深浅色变了 → 品牌色的「悬停方向」跟着变，重算一次
  const brand = root.dataset.brand
  if (brand) applyBrandColor(brand)
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
