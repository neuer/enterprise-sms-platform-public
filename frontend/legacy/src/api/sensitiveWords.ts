import { apiRequest } from "./webMessages"

export interface SensitiveWordItem {
  id: number
  word: string
}

export const listSensitiveWords = () =>
  apiRequest<SensitiveWordItem[]>("/admin/sensitive-words", { method: "GET" })

export const addSensitiveWords = (words: string[]) =>
  apiRequest<SensitiveWordItem[]>("/admin/sensitive-words", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ words }),
  })

export const deleteSensitiveWord = (id: number) =>
  apiRequest<void>(`/admin/sensitive-words/${id}`, { method: "DELETE" })
