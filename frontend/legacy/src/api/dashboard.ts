import { apiRequest } from "./webMessages"

export type DashboardCategory = "verify" | "notice" | "market"

export interface DashboardCategoryMetric {
  category: DashboardCategory
  total: number
  total_segments: number
  delivered: number
  failed: number
  unknown: number
  success_rate: number
}

export interface DashboardBalancePoint {
  stat_date: string
  balance: number
}

export interface DashboardAlert {
  level: "info" | "warn" | "crit"
  title: string
  created_at: string
}

export interface DashboardJob {
  job_name: string
  last_run_at: string | null
  last_status: "running" | "success" | "failed" | null
  stalled: boolean
}

export interface DashboardChannelMonitor {
  realtime_queue: number | null
  bulk_queue: number | null
  qps_used: number | null
  qps_rate: number | null
  reserved_realtime_qps: number | null
  stale: boolean
}

export interface DashboardUiPolicy {
  test_send_max: number | null
}

export interface DashboardOperations {
  current_balance: number | null
  balances: DashboardBalancePoint[]
  alerts: DashboardAlert[]
  dispositions: { uncertain: number; unmatched: number; callback_dead: number }
  jobs: DashboardJob[]
  channel_monitor: DashboardChannelMonitor
  balance_alert_threshold: number
}

export interface DashboardSnapshot {
  refreshed_at: string
  categories: DashboardCategoryMetric[]
  overall_success_rate: number
  pending_approvals: number
  ui_policy: DashboardUiPolicy
  operations?: DashboardOperations
}

export async function getDashboard(): Promise<DashboardSnapshot> {
  return apiRequest<DashboardSnapshot>("/reports/dashboard", { method: "GET" })
}
