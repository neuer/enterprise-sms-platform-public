import { apiRequest } from "./client"

export type SignState = "pending" | "approved" | "rejected"
export interface SmsSign {
  id: number
  name: string
  vendor_sign_id: string | null
  vendor_state: SignState
  vendor_reject_reason: string | null
}

export const listSigns = () => apiRequest<SmsSign[]>("/signs", { method: "GET" })
export const createSign = (name: string) =>
  apiRequest<SmsSign>("/signs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })
export const updateSign = (id: number, name: string) =>
  apiRequest<SmsSign>("/signs/" + id, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  })
export const deleteSign = (id: number) => apiRequest<void>("/signs/" + id, { method: "DELETE" })
export const syncSign = (id: number) => apiRequest<void>("/signs/" + id + "/sync", { method: "POST" })
export const adoptExistingSign = (id: number, vendorSignId: number, confirmedName: string) =>
  apiRequest<void>("/signs/" + id + "/adopt-existing", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vendor_sign_id: vendorSignId, confirmed_name: confirmedName }),
  })
