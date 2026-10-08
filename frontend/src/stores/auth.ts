/**
 * 会话与品牌状态（M5 §5.1/§5.3）
 *
 * 三件事必须集中在一处，否则每个页面都会各写一遍（legacy 的教训）
 * ==========================================================
 *
 * 1. **CSRF token 的刷新时机**：登录后、改密后、刷新页面时都要重取
 *    （后端把 token 绑在会话上，登出即失效）
 * 2. **`must_change_password` 的处置**：任何请求拿到 403 都要跳改密页，
 *    而不是在每个视图里 try/catch
 * 3. **匿名不是错误**：`/api/auth/me` 永远 200，用 `authenticated` 判断 ——
 *    所以路由守卫不用处理异常
 */
import { defineStore } from 'pinia'

import { ApiError, api, setCsrfToken } from '@/api/client'
import type { PublicUser, SitePayload } from '@/api/types'
import { applyBrandColor } from '@/theme'

interface State {
  /** 品牌信息（登录页也要用，所以与登录态解耦） */
  site: SitePayload | null
  user: PublicUser | null
  /** 首次身份探测是否完成（路由守卫靠它决定要不要等待） */
  ready: boolean
  loading: boolean
  error: string
}

export const useAuthStore = defineStore('auth', {
  state: (): State => ({
    site: null,
    user: null,
    ready: false,
    loading: false,
    error: '',
  }),

  getters: {
    authenticated: (state) => state.user !== null,
    isAdmin: (state) => state.user?.role === 'admin',
    /** 首登未改密 —— 除改密/登出/me 外所有接口都会被后端 403 拦下 */
    mustChangePassword: (state) => state.user?.must_change_password === true,
    siteName: (state) => state.site?.site_name ?? 'StarWatt 星瓦',
  },

  actions: {
    /** 品牌信息：**失败也要放行**（登录页不能因为一个接口挂了就白屏） */
    async loadSite(): Promise<void> {
      try {
        this.site = await api.site()
        applyBrandColor(this.site.theme_color)
      } catch {
        this.site = null
      }
    },

    /** 拉当前登录态 + 刷新 CSRF token（刷新页面后必调） */
    async loadMe(): Promise<void> {
      const payload = await api.me()
      setCsrfToken(payload.csrf_token)
      this.user = payload.authenticated ? payload.user : null
    },

    /** 应用启动：先拿品牌色，再探身份（顺序无所谓，但都要跑） */
    async bootstrap(): Promise<void> {
      this.loading = true
      this.error = ''
      try {
        await this.loadSite()
        await this.loadMe()
      } catch (error) {
        this.user = null
        if (!(error instanceof ApiError)) throw error
      } finally {
        this.loading = false
        this.ready = true
      }
    },

    async login(username: string, password: string): Promise<void> {
      this.error = ''
      try {
        await api.login(username, password)
        // 登录会换新会话 → CSRF token 必须重取（旧 token 已随旧会话失效）
        await this.loadMe()
      } catch (error) {
        this.error = error instanceof Error ? error.message : '登录失败'
        throw error
      }
    },

    async logout(): Promise<void> {
      try {
        await api.logout()
      } finally {
        setCsrfToken('')
        this.user = null
      }
    },

    /** 改密（含首登强制改密）；成功后后端会重签会话，所以要重取 CSRF */
    async changePassword(oldPassword: string, newPassword: string): Promise<void> {
      await api.changePassword(oldPassword, newPassword)
      await this.loadMe()
    },

    /**
     * 统一处理「接口报错该跳哪」。
     *
     * 返回 ``true`` 表示已经处理（调用方不用再显示错误）：
     * 会话没了 → 跳登录；被强制改密拦住 → 跳改密页。
     */
    handle(error: unknown): boolean {
      if (!(error instanceof ApiError)) return false
      if (error.isUnauthenticated) {
        this.user = null
        return true
      }
      if (error.mustChangePassword) {
        if (this.user) this.user.must_change_password = true
        return true
      }
      return false
    },
  },
})
