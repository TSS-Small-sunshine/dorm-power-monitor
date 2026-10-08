/**
 * HTTP 客户端（M5 §5.1）—— 全站**唯一**的 fetch 出口
 *
 * 与后端的三条约定（每条都有对应的后端实现，改前端前先读它们）
 * ========================================================
 *
 * 1. **会话在 Cookie 里**（`dorm_session`，HttpOnly + SameSite=Lax）
 *    → 每个请求必须 `credentials: 'same-origin'`。
 *    前端**看不到也存不到** token：不要试图写 localStorage。
 *
 * 2. **写操作要 CSRF**（`starwatt/auth/csrf.py`：头名 `X-CSRF-Token`）
 *    → token 从 `GET /api/auth/me` 的 `csrf_token` 取，登录/登出后都要刷新。
 *    缺这个头会拿到 403 `csrf_failed`。
 *
 * 3. **错误是 JSON**：`{"ok": false, "error": "<code>"}`，
 *    状态码 401 未登录 / 403 权限或 CSRF 或强制改密。
 *
 * 为什么要包一层
 * ==============
 *
 * `must_change_password` 这条分支（403）必须**在一处**处理：任何页面拿到它
 * 都应该跳改密页，而不是各自 try/catch。见 `stores/auth.ts` 的 `handle()`。
 */
import type {
  DataPayload,
  LivePayload,
  LoginPayload,
  MePayload,
  SitePayload,
} from './types'

/** 后端返回的错误码 → 中文提示（未知码回落到 HTTP 状态说明） */
const ERROR_TEXT: Record<string, string> = {
  unauthorized: '请先登录',
  forbidden: '当前账号没有权限',
  must_change_password: '请先修改初始密码',
  csrf_failed: '会话已失效，请重新登录',
  not_found: '接口不存在',
  method_not_allowed: '请求方法不被支持',
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message?: string,
  ) {
    super(message ?? ERROR_TEXT[code] ?? `请求失败（HTTP ${status}）`)
    this.name = 'ApiError'
  }

  /** 需要重新登录（会话没了） */
  get isUnauthenticated(): boolean {
    return this.status === 401
  }

  /** 被强制改密拦住（B4/Q19） */
  get mustChangePassword(): boolean {
    return this.status === 403 && this.code === 'must_change_password'
  }
}

//: CSRF token 由 auth store 注入 —— 这里只保存，不做持久化（刷新即重取）
let csrfToken = ''

export function setCsrfToken(token: string | undefined | null): void {
  csrfToken = token ?? ''
}

export function getCsrfToken(): string {
  return csrfToken
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  /** 显式跳过 CSRF（登录不需要 —— 那时还没有会话） */
  skipCsrf?: boolean
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipCsrf = false } = options
  const headers: Record<string, string> = { Accept: 'application/json' }

  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (!skipCsrf && csrfToken && method !== 'GET') {
    headers['X-CSRF-Token'] = csrfToken
  }

  const response = await fetch(path, {
    method,
    headers,
    credentials: 'same-origin',
    body: body === undefined ? undefined : JSON.stringify(body),
  })

  // 204 / 空体：按空对象处理（后端目前都返回 JSON，但别假设）
  const text = await response.text()
  let payload: unknown = {}
  if (text) {
    try {
      payload = JSON.parse(text)
    } catch {
      throw new ApiError(response.status, 'invalid_json', '服务端返回了非 JSON 响应')
    }
  }

  if (!response.ok) {
    const data = payload as { error?: string }
    throw new ApiError(response.status, data.error ?? 'unknown')
  }
  return payload as T
}

export const api = {
  /** 品牌信息（公开：登录页也要用） */
  site: () => request<SitePayload>('/api/site'),

  /** 当前登录态（**永远 200**，靠 ok/authenticated 判断） */
  me: () => request<MePayload>('/api/auth/me'),

  login: (username: string, password: string) =>
    request<LoginPayload>('/api/auth/login', {
      method: 'POST',
      body: { username, password },
      skipCsrf: true, // 还没有会话，拿不到 token
    }),

  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),

  changePassword: (oldPassword: string, newPassword: string) =>
    request<{ ok: boolean; token: string }>('/api/auth/password', {
      method: 'POST',
      body: { old_password: oldPassword, new_password: newPassword },
    }),

  data: (hours = 24) => request<DataPayload>(`/api/data?hours=${hours}`),

  live: () => request<LivePayload>('/api/live'),
}
