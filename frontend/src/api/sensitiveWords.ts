import { apiRequest } from "./client"

export interface SensitiveWordItem {
  id: number
  word: string
  created_at: string | null
}

export interface SensitiveWordPage {
  total: number
  items: SensitiveWordItem[]
}

export interface SensitiveWordFilters {
  keyword: string
  page: number
}

export const listSensitiveWords = (filters: SensitiveWordFilters) => {
  // 词条墙密度高，每页 60（服务端 size 上限 100 内）。
  const query = new URLSearchParams({ page: String(filters.page), size: "60" })
  if (filters.keyword) query.set("keyword", filters.keyword)
  return apiRequest<SensitiveWordPage>(`/admin/sensitive-words?${query}`, { method: "GET" })
}

export const addSensitiveWords = (words: string[]) =>
  apiRequest<{ added: number; skipped: number; items: SensitiveWordItem[] }>("/admin/sensitive-words", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ words }),
  })

export const deleteSensitiveWord = (id: number) =>
  apiRequest<void>(`/admin/sensitive-words/${id}`, { method: "DELETE" })
