import type { ApprovalListItem } from "../api/approvals"
import { CATEGORY_LABELS } from "./labels"

export function categoryLabel(category: ApprovalListItem["category"]): string {
  return CATEGORY_LABELS[category]
}

export function triggerRule(item: ApprovalListItem): string {
  if (item.trigger_threshold_source === "legacy_unknown" || item.trigger_threshold === null) {
    return "历史阈值不可确认"
  }
  const base = `${categoryLabel(item.category)} ≥ ${item.trigger_threshold} 个号码`
  return item.trigger_threshold_source === "snapshot" ? `${base} · 提交时阈值快照` : base
}

export function formatSegments(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString()} 条`
}
