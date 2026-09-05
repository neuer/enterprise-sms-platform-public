import type { ReportRow, ReportTrendMetric } from "../api/reports"

/** 维度序列上限：Top 5 + 「其他」归并。 */
export const REPORT_TREND_TOP_DIMS = 5
export const REPORT_TREND_OTHER_LABEL = "其他"

/** 区间内按所选指标加总（纯加法），超过 6 个维度时 Top 5 之外归并为「其他」。 */
export function reportTrendDims(items: ReportRow[], metric: ReportTrendMetric): Array<{ key: string; label: string }> {
  const totals = new Map<string, { label: string; value: number }>()
  for (const item of items) {
    const entry = totals.get(item.dim_value) ?? { label: item.dim_label, value: 0 }
    entry.value += item[metric]
    totals.set(item.dim_value, entry)
  }
  const ranked = [...totals.entries()]
    .map(([key, entry]) => ({ key, label: entry.label, value: entry.value }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label))
  if (ranked.length <= REPORT_TREND_TOP_DIMS + 1) return ranked.map(({ key, label }) => ({ key, label }))
  return [
    ...ranked.slice(0, REPORT_TREND_TOP_DIMS).map(({ key, label }) => ({ key, label })),
    { key: REPORT_TREND_OTHER_LABEL, label: REPORT_TREND_OTHER_LABEL },
  ]
}
