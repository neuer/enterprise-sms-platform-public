import { apiRequest } from "./client"

export type SignState = "pending" | "approved" | "rejected"
export interface SmsSign {
  id: number
  name: string
  vendor_sign_id: string | null
  vendor_state: SignState
  vendor_reject_reason: string | null
}

export const listSigns = (signal?: AbortSignal) => apiRequest<SmsSign[]>("/signs", { method: "GET", signal })
export const createSign = (name: string) =>
  apiRequest<SmsSign>("/signs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })
export const updateSign = (id: number, name: string) =>
  apiRequest<SmsSign>(`/signs/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })
export const deleteSign = (id: number) => apiRequest<void>(`/signs/${encodeURIComponent(id)}`, { method: "DELETE" })
export const syncSign = (id: number) => apiRequest<void>(`/signs/${encodeURIComponent(id)}/sync`, { method: "POST" })
export const adoptExistingSign = (id: number, vendorSignId: number, confirmedName: string) =>
  apiRequest<void>(`/signs/${encodeURIComponent(id)}/adopt-existing`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vendor_sign_id: vendorSignId, confirmed_name: confirmedName }),
  })
