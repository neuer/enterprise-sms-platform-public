import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { apiRequest } from "./client"
import type { Page } from "./pagination"
import type { components } from "./types.gen"

export type BlacklistItem = components["schemas"]["BlacklistItem"]
export type BlacklistSource = BlacklistItem["source"]

export type BlacklistPage = Page<BlacklistItem>

export interface BlacklistFilters {
  source: BlacklistSource | ""
  keyword: string
  page: number
}

export const listBlacklist = (filters: BlacklistFilters, signal?: AbortSignal) => {
  const query = new URLSearchParams({ page: String(filters.page), size: String(DEFAULT_PAGE_SIZE) })
  if (filters.source) query.set("source", filters.source)
  if (filters.keyword) query.set("keyword", filters.keyword)
  return apiRequest<BlacklistPage>(`/admin/blacklist?${query}`, { method: "GET", signal })
}

export const addBlacklist = (phones: string[], remark: string | null) =>
  apiRequest<{ added: number; updated: number; items: BlacklistItem[] }>("/admin/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phones, source: "manual", remark }),
  })

export const deleteBlacklist = (phoneHmac: string) =>
  apiRequest<void>(`/admin/blacklist/${encodeURIComponent(phoneHmac)}`, { method: "DELETE" })
