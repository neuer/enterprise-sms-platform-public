<script setup lang="ts">
import { LineChart } from "echarts/charts"
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { ReportGranularity, ReportRow } from "../api/reports"
import { CHART_COLORS, CHART_TOOLTIP_STYLE } from "../lib/chartTheme"

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  items: ReportRow[]
  start?: string
  end?: string
  granularity?: ReportGranularity
}>()
const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

function dayKey(value: string): string {
  return value.slice(0, 10)
}

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

function trend(): Array<{ period: string; total: number; segments: number }> {
  const buckets = new Map<string, { period: string; total: number; segments: number }>()
  for (const item of props.items) {
    const bucket = buckets.get(item.period_start) ?? { period: item.period_start, total: 0, segments: 0 }
    bucket.total += item.total
    bucket.segments += item.total_segments
    buckets.set(item.period_start, bucket)
  }
  const series = [...buckets.values()].sort((left, right) => left.period.localeCompare(right.period))
  if (props.granularity !== "day" || !props.start || !props.end) return series
  const days = enumerateDays(props.start, props.end)
  if (!days) return series
  const byDay = new Map(series.map((item) => [dayKey(item.period), item]))
  return days.map((day) => byDay.get(day) ?? { period: day, total: 0, segments: 0 })
}

function render(): void {
  if (!chart) return
  const values = trend()
  const compact = values.length > 10
  chart.setOption({
    animation: false,
    color: [CHART_COLORS.green, CHART_COLORS.amber],
    grid: { top: 42, right: 24, bottom: 32, left: 54 },
    legend: { top: 3, right: 4, textStyle: { color: CHART_COLORS.text, fontSize: 10 } },
    tooltip: { trigger: "axis", ...CHART_TOOLTIP_STYLE },
    xAxis: {
      type: "category", boundaryGap: false, data: values.map((item) => item.period),
      axisLabel: {
        color: CHART_COLORS.text,
        fontFamily: "IBM Plex Mono",
        hideOverlap: true,
        formatter: (value: string) => compact ? value.slice(5) : value,
      },
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
    },
    yAxis: {
      type: "value", minInterval: 1,
      axisLabel: { color: CHART_COLORS.text },
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
    },
    series: [
      { name: "消息数", type: "line", data: values.map((item) => item.total), symbolSize: compact ? 4 : 6, lineStyle: { width: 2 } },
      { name: "计费条", type: "line", data: values.map((item) => item.segments), symbolSize: compact ? 4 : 6, lineStyle: { width: 2 } },
    ],
  })
}

function resize(): void { chart?.resize() }

onMounted(() => {
  if (!root.value) return
  chart = echarts.init(root.value)
  render()
  window.addEventListener("resize", resize)
})
watch(() => [props.items, props.start, props.end, props.granularity], render, { deep: true })
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  chart?.dispose()
  chart = null
})
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="消息数与计费条趋势"></div></template>
