<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { ref, watch } from "vue"

import type { ReportGranularity, ReportTrend, ReportTrendMetric } from "../api/reports"
import { useChart } from "../composables/useChart"
import { getChartTheme } from "../lib/chartTheme"
import { shanghaiDateKey } from "../lib/time"

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  trend: ReportTrend
  metric: ReportTrendMetric
  start?: string
  end?: string
  granularity?: ReportGranularity
}>()
const root = ref<HTMLElement | null>(null)

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
    days.push(
      `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`,
    )
  }
  return days.length > 62 ? null : days
}

/** 横轴周期列表：日粒度且范围 ≤62 天时把空日补零，否则按数据出现的周期排序。 */
function periods(): string[] {
  const present = props.trend.periods
  if (props.granularity !== "day" || !props.start || !props.end) return present
  const days = enumerateDays(props.start, props.end)
  return days ?? present
}

function axisLabel(period: string): string {
  if (period === shanghaiDateKey()) return "今天"
  return period.length >= 10 ? period.slice(5) : period
}

const { render } = useChart(root, (chart) => {
  const theme = getChartTheme()
  const axis = periods()
  const indexes = new Map(props.trend.periods.map((period, index) => [period, index]))
  chart.setOption(
    {
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
      series: props.trend.series.map((dim, index) => ({
        name: dim.dim_label,
        type: "bar",
        stack: "total",
        barMaxWidth: 30,
        itemStyle: { color: theme.dimPalette[index % theme.dimPalette.length] },
        data: axis.map((period) => {
          const cell = indexes.get(period)
          return cell === undefined ? 0 : (dim[props.metric][cell] ?? 0)
        }),
      })),
    },
    { replaceMerge: ["series"] },
  )
})

// 查询结果每次整体替换（result 重新赋值），引用变化即代表数据变化，无需深比较。
watch(() => [props.trend, props.metric, props.start, props.end, props.granularity], render)
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="按维度堆叠的发送趋势"></div></template>
