import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { apiRequest } from "./client"

export type BlacklistSource = "manual" | "reply_optout" | "import"

export interface BlacklistItem {
  phone_hmac: string
  phone_mask: string
  source: BlacklistSource
  remark: string | null
  created_at: string | null
}

export interface BlacklistPage {
  total: number
  items: BlacklistItem[]
}

export interface BlacklistFilters {
  source: BlacklistSource | ""
  keyword: string
  page: number
}

export const listBlacklist = (filters: BlacklistFilters) => {
  const query = new URLSearchParams({ page: String(filters.page), size: String(DEFAULT_PAGE_SIZE) })
  if (filters.source) query.set("source", filters.source)
  if (filters.keyword) query.set("keyword", filters.keyword)
  return apiRequest<BlacklistPage>(`/admin/blacklist?${query}`, { method: "GET" })
}

export const addBlacklist = (phones: string[], remark: string | null) =>
  apiRequest<{ added: number; updated: number; items: BlacklistItem[] }>("/admin/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phones, source: "manual", remark }),
  })

export const deleteBlacklist = (phoneHmac: string) =>
  apiRequest<void>(`/admin/blacklist/${encodeURIComponent(phoneHmac)}`, { method: "DELETE" })
