<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { ReportGranularity, ReportRow, ReportTrendMetric } from "../api/reports"
import { CHART_COLORS, CHART_DIM_PALETTE, CHART_TOOLTIP_STYLE } from "../lib/chartTheme"

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

/** 维度序列上限：Top 5 + 「其他」归并，取色固定来自 CHART_DIM_PALETTE。 */
const TOP_DIMS = 5
const OTHER_LABEL = "其他"

const props = defineProps<{
  items: ReportRow[]
  metric: ReportTrendMetric
  start?: string
  end?: string
  granularity?: ReportGranularity
}>()
const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

function enumerateDays(start: string, end: string): string[] | null {
  const startMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(start)
  const endMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(end)
  if (!startMatch || !endMatch) return null
  const first = Date.UTC(Number(startMatch[1]), Number(startMatch[2]) - 1, Number(startMatch[3]))
  const last = Date.UTC(Number(endMatch[1]), Number(endMatch[2]) - 1, Number(endMatch[3]))
  if (last < first) return null
  const days: string[] = []
  for (let current = first; current <= last && days.length <= 62; current += 86_400_000) {
    const date = new Date(current)
    days.push(`${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`)
  }
  return days.length > 62 ? null : days
}

/** 横轴周期列表：日粒度且范围 ≤62 天时把空日补零，否则按数据出现的周期排序。 */
function periods(): string[] {
  const present = [...new Set(props.items.map((item) => item.period_start))].sort()
  if (props.granularity !== "day" || !props.start || !props.end) return present
  const days = enumerateDays(props.start, props.end)
  return days ?? present
}

/** 维度展示序列：区间内按所选指标加总（纯加法），超过 6 个维度时 Top 5 之外归并为「其他」。 */
function dimSeries(): Array<{ key: string; label: string }> {
  const totals = new Map<string, { label: string; value: number }>()
  for (const item of props.items) {
    const entry = totals.get(item.dim_value) ?? { label: item.dim_label, value: 0 }
    entry.value += item[props.metric]
    totals.set(item.dim_value, entry)
  }
  const ranked = [...totals.entries()]
    .map(([key, entry]) => ({ key, label: entry.label, value: entry.value }))
    .sort((left, right) => right.value - left.value || left.label.localeCompare(right.label))
  if (ranked.length <= TOP_DIMS + 1) return ranked.map(({ key, label }) => ({ key, label }))
  return [
    ...ranked.slice(0, TOP_DIMS).map(({ key, label }) => ({ key, label })),
    { key: OTHER_LABEL, label: OTHER_LABEL },
  ]
}

function render(): void {
  if (!chart) return
  const axis = periods()
  const seriesDims = dimSeries()
  const topKeys = new Set(seriesDims.map((item) => item.key))
  const cells = new Map<string, number>()
  for (const item of props.items) {
    const key = topKeys.has(item.dim_value) ? item.dim_value : OTHER_LABEL
    const cell = `${item.period_start}${key}`
    cells.set(cell, (cells.get(cell) ?? 0) + item[props.metric])
  }
  const compact = axis.length > 10
  chart.setOption({
    animation: false,
    grid: { top: 34, right: 16, bottom: 32, left: 56 },
    legend: {
      top: 0,
      left: 0,
      itemWidth: 10,
      itemHeight: 10,
      textStyle: { color: CHART_COLORS.text, fontSize: 11 },
      icon: "rect",
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      valueFormatter: (value: unknown) => Number(value).toLocaleString(),
      ...CHART_TOOLTIP_STYLE,
    },
    xAxis: {
      type: "category",
      data: axis,
      axisLabel: {
        color: CHART_COLORS.text,
        fontFamily: "IBM Plex Mono",
        hideOverlap: true,
        formatter: (value: string) => compact ? value.slice(5) : value,
      },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: CHART_COLORS.text, formatter: (value: number) => value.toLocaleString() },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    series: seriesDims.map((dim, index) => ({
      name: dim.label,
      type: "bar",
      stack: "total",
      barMaxWidth: 34,
      itemStyle: { color: CHART_DIM_PALETTE[index % CHART_DIM_PALETTE.length] },
      data: axis.map((period) => cells.get(`${period}${dim.key}`) ?? 0),
    })),
  })
}

function resize(): void { chart?.resize() }

onMounted(() => {
  if (!root.value) return
  chart = echarts.init(root.value)
  render()
  window.addEventListener("resize", resize)
})
watch(() => [props.items, props.metric, props.start, props.end, props.granularity], render, { deep: true })
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  chart?.dispose()
  chart = null
})
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="按维度堆叠的发送趋势"></div></template>
