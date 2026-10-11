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
 *
 * v2 新增的都是**语义层**，不是新色板：`brand.ink/strong`（可读前景与悬停）、
 * `sunken`（页面底）、`border-strong`（需要更硬的分隔线）、`*-soft`（语义色的
 * 柔和底，替代散落各处的 `bg-success/10` 手写透明度）。
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
          strong: 'rgb(var(--color-brand-strong) / <alpha-value>)',
          ink: 'rgb(var(--color-brand-ink) / <alpha-value>)',
          soft: 'rgb(var(--color-brand-soft) / <alpha-value>)',
        },
        sunken: 'rgb(var(--color-sunken) / <alpha-value>)',
        surface: 'rgb(var(--color-surface) / <alpha-value>)',
        elevated: 'rgb(var(--color-elevated) / <alpha-value>)',
        border: 'rgb(var(--color-border) / <alpha-value>)',
        'border-strong': 'rgb(var(--color-border-strong) / <alpha-value>)',
        content: 'rgb(var(--color-content) / <alpha-value>)',
        muted: 'rgb(var(--color-muted) / <alpha-value>)',
        success: 'rgb(var(--color-success) / <alpha-value>)',
        'success-soft': 'rgb(var(--color-success-soft) / <alpha-value>)',
        warning: 'rgb(var(--color-warning) / <alpha-value>)',
        'warning-soft': 'rgb(var(--color-warning-soft) / <alpha-value>)',
        danger: 'rgb(var(--color-danger) / <alpha-value>)',
        'danger-soft': 'rgb(var(--color-danger-soft) / <alpha-value>)',
      },
      borderRadius: {
        card: 'var(--radius-card)',
        control: 'var(--radius-control)',
      },
      boxShadow: {
        card: 'var(--shadow-card)',
        pop: 'var(--shadow-pop)',
      },
      fontFamily: {
        sans: ['MiSans', 'system-ui', 'sans-serif'],
        emoji: ['NotoEmoji', 'sans-serif'],
      },
      spacing: {
        gutter: 'var(--space-gutter)',
      },
      keyframes: {
        // 骨架屏的扫光：只动 background-position，不触发重排
        shimmer: {
          '0%': { backgroundPosition: '-160% 0' },
          '100%': { backgroundPosition: '260% 0' },
        },
        'fade-in-up': {
          '0%': { opacity: '0', transform: 'translateY(4px)' },
          '100%': { opacity: '1', transform: 'translateY(0)' },
        },
      },
      animation: {
        shimmer: 'shimmer 1.4s ease-in-out infinite',
        'fade-in-up': 'fade-in-up 0.22s ease-out both',
      },
    },
  },
  plugins: [],
} satisfies Config
