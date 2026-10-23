import type {
  ExportTask,
  ReportDimSummary,
  ReportFilters,
  ReportGranularity,
  ReportGroupBy,
  ReportRow,
  ReportTrendMetric,
} from "../../api/reports"
import { CHART_DIM_VARS } from "../../lib/chartTheme"
import { formatPercent } from "../../lib/format"

export type ReportAudience = "all" | "normal" | "test"

/** 全部不传 is_test；普通必须传 false，不能当成缺省。 */
export function audienceToIsTest(value: ReportAudience): boolean | undefined {
  if (value === "test") return true
  if (value === "normal") return false
  return undefined
}

export function reportQuery(
  base: Pick<ReportFilters, "granularity" | "groupBy" | "category" | "start" | "end">,
  value: ReportAudience,
): ReportFilters {
  const query: ReportFilters = {
    granularity: base.granularity,
    groupBy: base.groupBy,
    category: base.category,
    start: base.start,
    end: base.end,
  }
  const isTest = audienceToIsTest(value)
  if (isTest !== undefined) query.isTest = isTest
  return query
}

export const granularityLabel: Record<ReportGranularity, string> = { day: "日", week: "周", month: "月" }

export const granularityOptions: Array<{ label: string; value: ReportGranularity }> = [
  { label: "日", value: "day" },
  { label: "周", value: "week" },
  { label: "月", value: "month" },
]
export const groupByOptions: Array<{ label: string; value: ReportGroupBy }> = [
  { label: "应用", value: "app" },
  { label: "部门", value: "dept" },
]
export const audienceOptions: Array<{ label: string; value: ReportAudience }> = [
  { label: "全部", value: "all" },
  { label: "普通", value: "normal" },
  { label: "测试", value: "test" },
]
export const metricOptions: Array<{ label: string; value: ReportTrendMetric }> = [
  { label: "消息数", value: "total" },
  { label: "计费条", value: "total_segments" },
]
export const statusLabel: Record<ExportTask["status"], string> = {
  pending: "等待生成",
  running: "生成中",
  done: "已完成",
  failed: "生成失败",
}

export function dimColor(index: number): string {
  // 图例色块是 DOM 元素，用 var() 引用令牌即可随主题自动切换
  return `var(${CHART_DIM_VARS[index % CHART_DIM_VARS.length]})`
}

/** 明细行稳定键：周期 × 维度值，与移动端列表同一口径。 */
export function reportRowKey(row: Pick<ReportRow, "period_start" | "dim_value">): string {
  return `${row.period_start}-${row.dim_value}`
}

/** 成功率展示换算（ratio→百分比文本）走 lib/format 单点；口径仍只用服务端 success_rate。 */
export function formatRate(rate: number): string {
  return formatPercent(rate)
}

/** 成功率芯片着色阈值（≥98 绿 / 95–98 黄 / <95 红）：纯展示逻辑，口径仍来自服务端。 */
export function rateClass(rate: number): string {
  if (rate >= 0.98) return "g"
  if (rate >= 0.95) return "y"
  return "r"
}

/** 构成占比（加法 + 除法，非成功率口径）。 */
export function shareOf(value: number, total: number): string {
  if (total === 0) return formatPercent(0)
  return formatPercent(value / total)
}

export function composeWidth(value: number, total: number): string {
  if (total === 0 || value === 0) return "0%"
  return `${Math.max((value / total) * 100, 0.4)}%`
}

export function rankWidth(
  value: number,
  rows: ReadonlyArray<Pick<ReportDimSummary, "total" | "total_segments">>,
  metric: ReportTrendMetric,
): string {
  const max = Math.max(0, ...rows.map((item) => item[metric]))
  if (max === 0) return "0%"
  return `${Math.max((value / max) * 100, 2)}%`
}
