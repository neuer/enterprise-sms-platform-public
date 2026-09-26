import { apiRequest } from "./client"
import type { components } from "./types.gen"

export type TemplateState = components["schemas"]["Template"]["vendor_state"]
/** 契约未提供命名 schema，取 Template.var_specs 的内联元素类型。 */
export type VarSpec = components["schemas"]["Template"]["var_specs"][number]
export type SmsTemplate = components["schemas"]["Template"]
export interface TemplatePayload {
  name: string
  content: string
  var_specs: VarSpec[]
}

export const listTemplates = (signal?: AbortSignal) =>
  apiRequest<SmsTemplate[]>("/templates", { method: "GET", signal })
export const createTemplate = (payload: TemplatePayload) =>
  apiRequest<SmsTemplate>("/templates", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
export const updateTemplate = (id: number, payload: TemplatePayload) =>
  apiRequest<SmsTemplate>(`/templates/${encodeURIComponent(id)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  })
export const deleteTemplate = (id: number) =>
  apiRequest<void>(`/templates/${encodeURIComponent(id)}`, { method: "DELETE" })
export const syncTemplate = (id: number) =>
  apiRequest<void>(`/templates/${encodeURIComponent(id)}/sync`, { method: "POST" })
