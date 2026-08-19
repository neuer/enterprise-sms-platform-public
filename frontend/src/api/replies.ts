import { apiRequest } from "./webMessages"

export interface ReplyItem {
  id: number
  phone: string
  content: string
  batch_no: string | null
  reply_time: string
  blacklisted: boolean
}

export interface ReplyPage {
  total: number
  items: ReplyItem[]
}

/** 处置口径：all 全部 / pending_optout 退订语未加黑 / blacklisted 已加黑 */
export type ReplyDisposition = "all" | "pending_optout" | "blacklisted"

export interface ReplyFilters {
  phone?: string
  start?: string
  end?: string
  disposition?: ReplyDisposition
  page: number
}

export function listReplies(filters: ReplyFilters): Promise<ReplyPage> {
  const body: {
    page: number
    phone?: string
    start?: string
    end?: string
    disposition: ReplyDisposition
  } = { page: filters.page, disposition: filters.disposition ?? "all" }
  if (filters.phone) body.phone = filters.phone
  if (filters.start) body.start = filters.start
  if (filters.end) body.end = filters.end
  return apiRequest<ReplyPage>("/replies", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  })
}

export function blacklistReply(id: number): Promise<void> {
  return apiRequest<void>(`/replies/${id}/blacklist`, { method: "POST" })
}
