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

// ---------------------------------------------------------------------------
// 配置中心（Q15 / 5.6）—— 契约来自 starwatt/config_registry/registry.py 的 schema()
// ---------------------------------------------------------------------------

/** 10 种字段类型（每种对应一个控件，见 components/config/ConfigField.vue） */
export type ConfigValueType =
  | 'bool'
  | 'duration' // 秒数（整数）
  | 'enum' // choices 里选一个
  | 'float'
  | 'int'
  | 'json' // 对象 / 数组（textarea）
  | 'secret' // 脱敏显示；只在改动时提交
  | 'str'
  | 'time' // HH:MM
  | 'url'

/** ``kind``：``config`` 可改；``state`` 是运行时状态（只读展示） */
export type ConfigKind = 'config' | 'state'

export interface ConfigSetting {
  key: string
  label: string
  group: string
  type: ConfigValueType
  kind: ConfigKind
  default: unknown
  /** 中文说明（可能带 📌/🔒 等标记） */
  help: string
  /** 校验规则串（``len:1..32`` / ``regex:...`` / ``url`` / ``enum:a|b`` / ``json`` / ``none``） */
  validate: string
  /** 改动后需要重启才生效（UI 上要明确提示） */
  requires_restart: boolean
  /** 非空时：只有该键为真才显示本项（条件显示） */
  depends_on: string | null
  /** 高级项（默认折叠） */
  advanced: boolean
  choices: string[]
  secret: boolean
  /** ``false`` = 只读（state 项） */
  user_editable: boolean
}

export interface ConfigGroup {
  key: string
  label: string
  settings: ConfigSetting[]
}

export interface ConfigSchemaPayload {
  groups: ConfigGroup[]
  total: number
}

/** ``GET /api/admin/config`` —— secret 默认是 ``••••1234`` */
export interface ConfigValuesPayload {
  values: Record<string, unknown>
  /** 当前开启的开关 key 列表（5.7 用） */
  flags: string[]
}

/** ``PUT /api/admin/config`` —— 逐键独立，部分失败回 207 */
export interface ConfigUpdateResult {
  applied: string[]
  errors: Record<string, string>
}

export interface ConfigExportPayload {
  format: string
  exported_at: string
  include_secrets: boolean
  config: Record<string, unknown>
}

/** ``POST /api/admin/config/import`` —— 合并语义 */
export interface ConfigImportResult {
  applied: string[]
  skipped: string[]
  errors: Record<string, string>
}

export interface ConfigTestResult {
  ok: boolean
  target: string
  error: string | null
}

export interface ConfigResetResult {
  reset: number
}

/** ``GET /api/admin/users`` —— 管理后台的用户行（**不含密码哈希**） */
export interface AdminUser {
  id: number
  username: string
  role: 'admin' | 'viewer'
  created_at: string | null
  last_login_at: string | null
  disabled: boolean
  must_change_password: boolean
}

/** ``GET /api/admin/audit`` —— 审计行 */
export interface AuditEntry {
  id: number
  action: string
  user_id: number | null
  target: string | null
  ip: string | null
  created_at: string
  details: Record<string, unknown> | null
}
