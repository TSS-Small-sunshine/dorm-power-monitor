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
  AdminUser,
  AuditEntry,
  CommitResult,
  ConfigExportPayload,
  ConfigImportResult,
  ConfigResetResult,
  ConfigSchemaPayload,
  ConfigTestResult,
  ConfigUpdateResult,
  ConfigValuesPayload,
  DailyPayload,
  DataPayload,
  LivePayload,
  LoginPayload,
  MePayload,
  OobeAdvanceResult,
  OobeStatePayload,
  ParseUrlResult,
  PaymentPayload,
  RefreshPayload,
  SitePayload,
  VerifyResult,
  ViolationPayload,
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
  /**
   * 额外视为成功的状态码。
   *
   * 配置 PUT / import 用 **207**（Multi-Status）表达「部分成功」：
   * 逐键独立校验，成功的生效、失败的带原因回来。对前端来说这是一次
   * **成功**的请求（有结果可展示），不能按失败抛异常。
   */
  okStatuses?: number[]
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, skipCsrf = false, okStatuses = [] } = options
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

  if (!response.ok && !okStatuses.includes(response.status)) {
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

  /** 记录区间筛选（E8）：显式 start/end 优先于 hours */
  dataRange: (start: string, end: string) =>
    request<DataPayload>(
      `/api/data?start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`,
    ),

  live: () => request<LivePayload>('/api/live'),

  violations: (days = 30) => request<ViolationPayload>(`/api/violations?days=${days}`),

  payments: (days = 90) => request<PaymentPayload>(`/api/payments?days=${days}`),

  daily: (days = 30) => request<DailyPayload>(`/api/daily?days=${days}`),

  /** 手动刷新（E13）—— 管理员会话 + CSRF；服务端抓取但**不推送飞书** */
  refresh: () => request<RefreshPayload>('/api/refresh', { method: 'POST' }),

  // -------------------------------------------------------------------------
  // 配置中心（Q15 / 5.6）—— 七个端点
  // -------------------------------------------------------------------------
  configSchema: () => request<ConfigSchemaPayload>('/api/admin/config/schema'),

  configValues: () => request<ConfigValuesPayload>('/api/admin/config'),

  /** 批量更新：**207 也算成功**（逐键独立，部分失败带原因回来） */
  configUpdate: (values: Record<string, unknown>) =>
    request<ConfigUpdateResult>('/api/admin/config', {
      method: 'PUT',
      body: values,
      okStatuses: [207],
    }),

  configTest: (target: string) =>
    request<ConfigTestResult>('/api/admin/config/test', { method: 'POST', body: { target } }),

  configExport: (includeSecrets = false) =>
    request<ConfigExportPayload>(
      `/api/admin/config/export${includeSecrets ? '?include_secrets=1' : ''}`,
    ),

  /** 导入（合并语义；脱敏值会被后端跳过） */
  configImport: (payload: unknown, allowSecrets = false) =>
    request<ConfigImportResult>('/api/admin/config/import', {
      method: 'POST',
      body: { payload, allow_secrets: allowSecrets },
      okStatuses: [207],
    }),

  configReset: () =>
    request<ConfigResetResult>('/api/admin/config/reset', { method: 'POST' }),

  // -------------------------------------------------------------------------
  // 用户 / 审计（5.5）
  // -------------------------------------------------------------------------
  users: () => request<{ users: AdminUser[] }>('/api/admin/users'),

  createUser: (body: { username: string; password: string; role?: string }) =>
    request<{ ok: boolean; user: AdminUser }>('/api/admin/users', { method: 'POST', body }),

  updateUser: (id: number, body: { role?: string; disabled?: boolean }) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}`, { method: 'PATCH', body }),

  resetUserPassword: (id: number, password: string) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}/password`, {
      method: 'POST',
      body: { password },
    }),

  deleteUser: (id: number) =>
    request<{ ok: boolean }>(`/api/admin/users/${id}`, { method: 'DELETE' }),

  audit: (limit = 100, action = '') =>
    request<{ entries: AuditEntry[] }>(
      `/api/admin/audit?limit=${limit}${action ? `&action=${encodeURIComponent(action)}` : ''}`,
    ),

  // -------------------------------------------------------------------------
  // OOBE 引导（5 步 / 只有两个端点）
  // -------------------------------------------------------------------------
  oobeState: () => request<OobeStatePayload>('/api/oobe/state'),

  /** 保存本步并移动；207 = 本步有键没通过校验（载荷里带中文原因） */
  oobeAdvance: (body: {
    direction: 'next' | 'prev'
    values?: Record<string, unknown>
    skip?: boolean
  }) =>
    request<OobeAdvanceResult>('/api/oobe/advance', {
      method: 'POST',
      body,
      okStatuses: [207],
    }),

  // -------------------------------------------------------------------------
  // 自助配置（N4）
  // -------------------------------------------------------------------------
  /** 粘贴学校 H5 地址 → 解析出 openid / roomId（**不落库**） */
  setupParseUrl: (url: string) =>
    request<ParseUrlResult>('/api/setup/parse-url', { method: 'POST', body: { url } }),

  /** 真的连一次学校接口验证凭据（**不落库**） */
  setupVerify: (body: { openid: string; room_id?: string; base_url?: string }) =>
    request<VerifyResult>('/api/setup/verify', { method: 'POST', body }),

  /** 保存自助配置（走配置中心同一套校验） */
  setupCommit: (body: Record<string, unknown>) =>
    request<CommitResult>('/api/setup/commit', {
      method: 'POST',
      body,
      okStatuses: [207],
    }),
}
