import type { Config } from 'tailwindcss'

/**
 * Tailwind 配置 —— **只做映射，不写死颜色**（M5 §5.2）
 *
 * 所有颜色都指向 `src/styles/tokens.css` 里的 CSS 变量：
 *
 * * 用户在「配置中心 → 站点 → 主题色」改 `theme_color` 时，前端只需覆盖
 *   `--color-brand-*` 几个变量，**不用重新构建**
 * * 深色模式同理：`tokens.css` 在 `html.dark` 下换一套变量值
 *
 * 若把色值写在这里（`colors: { brand: '#1677ff' }`），上面两件事都做不到。
 */
export default {
  content: ['./index.html', './src/**/*.{vue,ts}'],
  darkMode: 'class',
  theme: {
    // 4 个断点（REQUIREMENTS §595）：≤767 / 768–1023 / 1024–1439 / ≥1440
    screens: {
      sm: '768px',
      md: '1024px',
      lg: '1440px',
    },
    extend: {
      colors: {
        brand: {
          DEFAULT: 'rgb(var(--color-brand) / <alpha-value>)',
          soft: 'rgb(var(--color-brand-soft) / <alpha-value>)',
        },
        surface: 'rgb(var(--color-surface) / <alpha-value>)',
        elevated: 'rgb(var(--color-elevated) / <alpha-value>)',
        border: 'rgb(var(--color-border) / <alpha-value>)',
        content: 'rgb(var(--color-content) / <alpha-value>)',
        muted: 'rgb(var(--color-muted) / <alpha-value>)',
        success: 'rgb(var(--color-success) / <alpha-value>)',
        warning: 'rgb(var(--color-warning) / <alpha-value>)',
        danger: 'rgb(var(--color-danger) / <alpha-value>)',
      },
      borderRadius: {
        card: 'var(--radius-card)',
        control: 'var(--radius-control)',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
      },
      fontFamily: {
        sans: ['MiSans', 'system-ui', 'sans-serif'],
        emoji: ['NotoEmoji', 'sans-serif'],
      },
      spacing: {
        gutter: 'var(--space-gutter)',
      },
    },
  },
  plugins: [],
} satisfies Config
