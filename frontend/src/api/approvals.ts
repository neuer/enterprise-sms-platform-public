import { apiRequest, type Category } from "./webMessages"

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired"

export interface ApprovalItem {
  id: number
  batch_no: string
  category: Category
  applicant: string
  dept: string
  total: number
  segments: number | null
  estimated_segments: number | null
  scheduled_at?: string | null
  trigger_threshold: number | null
  trigger_threshold_source: "snapshot" | "legacy_unknown"
  content: string
  status: ApprovalStatus
  approver: string | null
  reason: string | null
  created_at: string
}

export interface ApprovalPage {
  total: number
  items: ApprovalItem[]
}

type ApprovalWireItem = Omit<
  ApprovalItem,
  "segments" | "estimated_segments" | "scheduled_at" | "trigger_threshold" | "trigger_threshold_source"
> & Partial<Pick<
  ApprovalItem,
  "segments" | "estimated_segments" | "scheduled_at" | "trigger_threshold" | "trigger_threshold_source"
>>

export async function listApprovals(status: ApprovalStatus, page = 1): Promise<ApprovalPage> {
  const query = new URLSearchParams({ status, page: String(page) })
  const result = await apiRequest<{ total: number; items: ApprovalWireItem[] }>(
    `/approvals?${query}`,
    { method: "GET" },
  )
  return {
    total: result.total,
    items: result.items.map((item) => ({
      ...item,
      segments: item.segments ?? null,
      estimated_segments: item.estimated_segments ?? null,
      trigger_threshold: item.trigger_threshold ?? null,
      trigger_threshold_source: item.trigger_threshold_source ?? "legacy_unknown",
    })),
  }
}

export async function decideApproval(
  id: number,
  action: "approve" | "reject",
  reason?: string,
): Promise<void> {
  await apiRequest<unknown>(`/approvals/${id}/decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, reason: reason || null }),
  })
}
