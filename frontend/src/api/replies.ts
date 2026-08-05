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
  const query = new URLSearchParams({ page: String(filters.page) })
  if (filters.phone) query.set("phone", filters.phone)
  if (filters.start) query.set("start", filters.start)
  if (filters.end) query.set("end", filters.end)
  return apiRequest<ReplyPage>(`/replies?${query}`, { method: "GET" })
}

export function blacklistReply(id: number): Promise<void> {
  return apiRequest<void>(`/replies/${id}/blacklist`, { method: "POST" })
}
