<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { ref, watch } from "vue"

import type { ReportGranularity, ReportRow, ReportTrendMetric } from "../api/reports"
import { useChart } from "../composables/useChart"
import { getChartTheme } from "../lib/chartTheme"
import { REPORT_TREND_OTHER_LABEL, reportTrendDims } from "../lib/reportTrend"
import { enumerateDateKeys, shanghaiDateKey } from "../lib/time"

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  items: ReportRow[]
  metric: ReportTrendMetric
  start?: string
  end?: string
  granularity?: ReportGranularity
}>()
const root = ref<HTMLElement | null>(null)

/** 横轴周期列表：日粒度且范围 ≤62 天时把空日补零（日期枚举下沉 lib/time 单点），否则按数据出现的周期排序。 */
function periods(): string[] {
  const present = [...new Set(props.items.map((item) => item.period_start))].sort()
  if (props.granularity !== "day" || !props.start || !props.end) return present
  const days = enumerateDateKeys(props.start, props.end)
  return days ?? present
}

function axisLabel(period: string): string {
  if (period === shanghaiDateKey()) return "今天"
  return period.length >= 10 ? period.slice(5) : period
}

const { render } = useChart(root, (chart) => {
  const theme = getChartTheme()
  const axis = periods()
  const seriesDims = reportTrendDims(props.items, props.metric)
  const topKeys = new Set(seriesDims.map((item) => item.key))
  const cells = new Map<string, number>()
  for (const item of props.items) {
    const key = topKeys.has(item.dim_value) ? item.dim_value : REPORT_TREND_OTHER_LABEL
    const cell = `${item.period_start}${key}`
    cells.set(cell, (cells.get(cell) ?? 0) + item[props.metric])
  }
  chart.setOption({
    animation: false,
    grid: { top: 4, right: 4, bottom: 2, left: 2, containLabel: true },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      valueFormatter: (value: unknown) => Number(value).toLocaleString(),
      ...theme.tooltip,
    },
    xAxis: {
      type: "category",
      data: axis,
      axisLabel: {
        color: theme.text,
        fontFamily: "IBM Plex Mono",
        fontSize: 10,
        hideOverlap: true,
        formatter: (value: string) => axisLabel(value),
      },
      axisLine: { lineStyle: { color: theme.axisLine } },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLabel: { color: theme.text, fontSize: 10, formatter: (value: number) => value.toLocaleString() },
      splitLine: { lineStyle: { color: theme.splitLine, type: "dashed" } },
    },
    series: seriesDims.map((dim, index) => ({
      name: dim.label,
      type: "bar",
      stack: "total",
      barMaxWidth: 30,
      itemStyle: { color: theme.dimPalette[index % theme.dimPalette.length] },
      data: axis.map((period) => cells.get(`${period}${dim.key}`) ?? 0),
    })),
  })
})

// 查询结果每次整体替换（result 重新赋值），引用变化即代表数据变化，无需深比较。
watch(() => [props.items, props.metric, props.start, props.end, props.granularity], render)
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="按维度堆叠的发送趋势"></div></template>
