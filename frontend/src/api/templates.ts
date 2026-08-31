import { apiRequest } from "./client"

export type TemplateState = "draft" | "pending" | "approved" | "rejected"
export interface VarSpec { pos: number; max_len: number }
export interface SmsTemplate {
  id: number
  name: string
  content: string
  var_specs: VarSpec[]
  /** 历史兼容字段；模板为全局资源，不参与权限或发送判断。 */
  dept: string
  vendor_template_id: string | null
  vendor_state: TemplateState
  vendor_reject_reason: string | null
}
export interface TemplatePayload { name: string; content: string; var_specs: VarSpec[] }

export const listTemplates = () =>
  apiRequest<SmsTemplate[]>("/templates", { method: "GET" })
export const createTemplate = (payload: TemplatePayload) =>
  apiRequest<SmsTemplate>("/templates", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })
export const updateTemplate = (id: number, payload: TemplatePayload) =>
  apiRequest<SmsTemplate>("/templates/" + id, {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
  })
export const deleteTemplate = (id: number) =>
  apiRequest<void>("/templates/" + id, { method: "DELETE" })
export const syncTemplate = (id: number) =>
  apiRequest<void>("/templates/" + id + "/sync", { method: "POST" })
