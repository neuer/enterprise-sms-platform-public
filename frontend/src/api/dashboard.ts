import { apiRequest } from "./client"
import type { components } from "./types.gen"

export type DashboardCategoryMetric = components["schemas"]["DashboardCategoryModel"]

export type DashboardTrendPoint = components["schemas"]["DashboardTrendPointModel"]

export type DashboardBalancePoint = components["schemas"]["DashboardBalancePointModel"]

export type DashboardAlert = components["schemas"]["DashboardAlertModel"]

export type DashboardJob = components["schemas"]["DashboardJobModel"]

export type DashboardChannelMonitor = components["schemas"]["DashboardChannelMonitorModel"]

export type DashboardUiPolicy = components["schemas"]["DashboardUiPolicyModel"]

export type DashboardOperations = components["schemas"]["DashboardOperationsModel"]

export type DashboardSnapshot = components["schemas"]["DashboardModel"]

export async function getDashboard(signal?: AbortSignal): Promise<DashboardSnapshot> {
  return apiRequest<DashboardSnapshot>("/reports/dashboard", { method: "GET", signal })
}

export type BalanceSnapshot = components["schemas"]["BalanceSnapshotModel"]

/** 管理员顶栏只读取最新余额事实，不触发完整仪表盘聚合。 */
export function getBalance(signal?: AbortSignal): Promise<BalanceSnapshot> {
  return apiRequest<BalanceSnapshot>("/reports/balance", { method: "GET", signal })
}
