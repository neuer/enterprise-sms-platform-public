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

export interface ReplyFilters {
  phone?: string
  start?: string
  end?: string
  page: number
}

export function listReplies(filters: ReplyFilters): Promise<ReplyPage> {
  const body: { page: number; phone?: string; start?: string; end?: string } = { page: filters.page }
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
