/**
 * 后端 payload 的类型（M5 §5.1）
 *
 * ⚠️ 这些字段名**不是随便起的** —— 它们来自冻结契约
 * `tests/regression/fixtures/api.json`（M0 在旧代码上真实运行产出）。
 * 改这里之前先看那个文件：字段名不一致 = 静默丢功能。
 */

/** ``/api/data`` 的统计卡（4 个值，见 fixtures/algorithms.json） */
export interface Stats {
  /** 剩余电量（kW·h）；无数据时 null */
  remain: number | null
  /** 近一小时用量；不足一小时窗口时 null */
  hourly_used: number | null
  /** 电表上报时间（字符串，可能为 null） */
  read_time: string | null
  /** 日均用量；观测跨度不足 1 天时 null */
  daily_avg: number | null
}

/** ``/api/data`` 的一行记录 */
export interface RecordRow {
  id: number
  ts: string
  read_time: string | null
  remain: number | null
}

export interface DataPayload {
  hours: number
  rows: RecordRow[]
  stats: Stats
}

/** ``/api/live`` 的电表状态（**驼峰键**，与卡片契约一致） */
export interface RunStatus {
  cur?: string
  vol?: string
  yggl?: string
  run_status?: string
  update_dt?: string
}

/** ``/api/live`` 的本月预计 */
export interface MonthlyProjection {
  used_kwh: number | null
  avg_daily: number | null
  days_observed: number
  days_left: number
  eqprice: number
  monthly_projection: number | null
}

export interface LivePayload {
  stats: Stats
  latest_ts: string | null
  run_status: RunStatus
  stale: boolean
  scrape_status: Record<string, unknown>
  eqprice: number
  monthly_breakdown: Record<string, unknown> | null
  monthly_projection: MonthlyProjection
}

/** ``/api/site`` —— 品牌信息（**公开**：登录页也要显示站点名与主题色） */
export interface SitePayload {
  site_name: string
  theme_color: string
  app_version: string
}

/** ``/api/auth/me`` —— 未登录时 ``ok=false``（**不返回 401**，供路由守卫用） */
export interface MePayload {
  ok: boolean
  authenticated: boolean
  user: PublicUser | null
  csrf_token?: string
}

export interface PublicUser {
  id: number
  username: string
  role: 'admin' | 'viewer'
  must_change_password: boolean
}

export interface LoginPayload {
  ok: true
  user: PublicUser
  token: string
  must_change_password: boolean
}

/** ``/api/violations`` —— E9（``power`` 单位是 **kW**） */
export interface ViolationRow {
  dt: string
  reason: string
  power: number | null
}

export interface ViolationPayload {
  room_id: string
  days: number
  rows: ViolationRow[]
}

/** ``/api/payments`` —— E10 */
export interface PaymentRow {
  dt: string
  pay_type: string | null
  fee_type: string | null
  money: number | null
}

export interface PaymentPayload {
  room_id: string
  days: number
  rows: PaymentRow[]
}

/** ``/api/daily`` —— E6（``used`` 已由后端从累计表码推导） */
export interface DailyRow {
  dt: string
  used: number
}

export interface DailyPayload {
  room_id: string
  days: number
  rows: DailyRow[]
}

/** ``/api/refresh`` —— E13 */
export interface RefreshPayload {
  ok: boolean
  room_id: string
  records: number
  error: string | null
}
