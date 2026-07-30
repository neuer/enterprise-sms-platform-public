import { apiRequest } from "./webMessages"

export interface BlacklistItem {
  phone_hmac: string
  phone_mask: string
  source: "manual" | "reply_optout" | "import"
  remark: string | null
  created_at: string | null
}

export const listBlacklist = () =>
  apiRequest<BlacklistItem[]>("/admin/blacklist", { method: "GET" })

export const addBlacklist = (phones: string[], remark: string | null) =>
  apiRequest<{ added: number; items: BlacklistItem[] }>("/admin/blacklist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ phones, source: "manual", remark }),
  })

export const deleteBlacklist = (phoneHmac: string) =>
  apiRequest<void>(`/admin/blacklist/${encodeURIComponent(phoneHmac)}`, { method: "DELETE" })
