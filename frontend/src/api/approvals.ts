import { apiRequest, type Category } from "./webMessages"

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired"

export type ApprovalSort = "expires_asc" | "created_desc" | "decided_desc"

export type ApprovalAction = "approve" | "reject"

export interface ApprovalCounts {
  pending: number
  approved: number
  rejected: number
  expired: number
  pending_urgent: number
}

export interface ApprovalListItem {
  id: number
  batch_no: string
  category: Category
  applicant: string
  dept: string
  total: number
  segments: number | null
  estimated_segments: number | null
  scheduled_at: string | null
  trigger_threshold: number | null
  trigger_threshold_source: "snapshot" | "legacy_unknown"
  status: ApprovalStatus
  approver: string | null
  reason: string | null
  expires_at: string | null
  decided_at: string | null
  created_at: string
  batch_status: string
  deferred_reason: string | null
}

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

export interface ApprovalPage {
  total: number
  counts: ApprovalCounts
  items: ApprovalListItem[]
}

export interface DecisionOutcome {
  status: ApprovalStatus
  batch_status: string
  deferred_reason: string | null
}

export async function listApprovals(query: ApprovalQuery): Promise<ApprovalPage> {
  const params = new URLSearchParams({ status: query.status })
  if (query.page !== undefined) params.set("page", String(query.page))
  if (query.size !== undefined) params.set("size", String(query.size))
  if (query.category) params.set("category", query.category)
  if (query.dept?.trim()) params.set("dept", query.dept.trim())
  if (query.q?.trim()) params.set("q", query.q.trim())
  if (query.sort) params.set("sort", query.sort)
  return apiRequest<ApprovalPage>(`/approvals?${params}`, { method: "GET" })
}

export async function getApproval(id: number): Promise<ApprovalDetail> {
  return apiRequest<ApprovalDetail>(`/approvals/${id}`, { method: "GET" })
}

export async function decideApproval(
  id: number,
  action: ApprovalAction,
  reason?: string,
): Promise<DecisionOutcome> {
  return apiRequest<DecisionOutcome>(`/approvals/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, reason: reason || null }),
  })
}
