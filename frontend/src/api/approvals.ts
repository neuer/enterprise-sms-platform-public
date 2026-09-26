import { apiRequest } from "./client"
import type { Page } from "./pagination"
import type { components } from "./types.gen"
import type { Category } from "./webMessages"

export type ApprovalStatus = components["schemas"]["ApprovalListItem"]["status"]

export type ApprovalSort = "expires_asc" | "created_desc" | "decided_desc"

export type ApprovalAction = "approve" | "reject"

export type ApprovalCounts = components["schemas"]["ApprovalCounts"]

/**
 * 契约 components.schemas.ApprovalListItem 的 category 为宽松 string；前端按审批业务实际
 * 窄化为 Category（notice/market），CategoryTag 的 MessageCategory prop 与 laneLabel(Category)
 * 均依赖该窄化，直接替换为生成类型会破坏视图类型兼容，故保留手写。
 */
export interface ApprovalListItem {
  id: number
  batch_no: string
  category: Category
  applicant: string
  applicant_account_id: number | null
  dept: string
  total: number
  segments: number
  estimated_segments: number
  scheduled_at: string | null
  trigger_threshold: number | null
  trigger_threshold_source: "snapshot" | "legacy_unknown"
  status: ApprovalStatus
  approver: string | null
  reason: string | null
  expires_at: string
  decided_at: string | null
  created_at: string
  batch_status: string
  deferred_reason: string | null
}

/** 同 ApprovalListItem：契约 ApprovalDetail 的 category 同为 string，保留手写以维持 Category 窄化。 */
export interface ApprovalDetail extends ApprovalListItem {
  content: string
}

export interface ApprovalQuery {
  status: ApprovalStatus
  page?: number
  size?: number
  category?: Category
  dept?: string
  q?: string
  sort?: ApprovalSort
}

export interface ApprovalPage extends Page<ApprovalListItem> {
  counts: ApprovalCounts
}

export type DecisionOutcome = components["schemas"]["DecisionOutcome"]

export async function listApprovals(query: ApprovalQuery, signal?: AbortSignal): Promise<ApprovalPage> {
  const params = new URLSearchParams({ status: query.status })
  if (query.page !== undefined) params.set("page", String(query.page))
  if (query.size !== undefined) params.set("size", String(query.size))
  if (query.category) params.set("category", query.category)
  if (query.dept?.trim()) params.set("dept", query.dept.trim())
  if (query.q?.trim()) params.set("q", query.q.trim())
  if (query.sort) params.set("sort", query.sort)
  return apiRequest<ApprovalPage>(`/approvals?${params}`, { method: "GET", signal })
}

export async function getApproval(id: number, signal?: AbortSignal): Promise<ApprovalDetail> {
  return apiRequest<ApprovalDetail>(`/approvals/${id}`, { method: "GET", signal })
}

export async function decideApproval(id: number, action: ApprovalAction, reason?: string): Promise<DecisionOutcome> {
  return apiRequest<DecisionOutcome>(`/approvals/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, reason: reason || null }),
  })
}
