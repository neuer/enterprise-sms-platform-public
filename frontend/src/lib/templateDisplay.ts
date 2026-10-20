import type { SmsTemplate, TemplateState } from "../api/templates"
import { VENDOR_REVIEW_LABELS } from "./labels"

export function stateLabel(state: TemplateState): string {
  return VENDOR_REVIEW_LABELS[state]
}

export const canSync = (item: SmsTemplate) => item.vendor_state !== "draft" && item.vendor_template_id !== null
export const canEdit = (item: SmsTemplate) =>
  ["draft", "rejected"].includes(item.vendor_state) && item.vendor_template_id === null
export const canDelete = (item: SmsTemplate) => item.vendor_state !== "approved" && item.vendor_template_id === null
export const canUse = (item: SmsTemplate) => item.vendor_state === "approved"
