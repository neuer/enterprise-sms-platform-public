<script setup lang="ts">
import { formatNumber } from "../lib/format"
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, ref, watch } from "vue"

import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"

import type { ReportGranularity, ReportTrend, ReportTrendMetric } from "../api/reports"
import { useChart } from "../composables/useChart"
import { getChartTheme } from "../lib/chartTheme"
import { enumerateDateKeys, shanghaiDateKey } from "../lib/time"

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{
  trend: ReportTrend
  metric: ReportTrendMetric
  start?: string
  end?: string
  granularity?: ReportGranularity
}>()
const root = ref<HTMLElement | null>(null)
let tooltipHost: HTMLDivElement | null = null

/** 趋势浮层挂到卡片外，避免 el-card 的 overflow 裁切；主题色仍读根上的图表令牌。 */
function tooltipParent(): HTMLElement {
  if (!tooltipHost) {
    tooltipHost = document.createElement("div")
    tooltipHost.className = "report-trend-tooltip-host"
    tooltipHost.style.position = "fixed"
    tooltipHost.style.left = "0"
    tooltipHost.style.top = "0"
    tooltipHost.style.width = "100vw"
    tooltipHost.style.height = "100vh"
    tooltipHost.style.overflow = "visible"
    tooltipHost.style.zIndex = "4000"
    tooltipHost.style.pointerEvents = "none"
    document.body.appendChild(tooltipHost)
  }
  tooltipHost.dataset.theme = document.documentElement.dataset.theme || ""
  return tooltipHost
}

function retireTooltip(): void {
  tooltipHost?.replaceChildren()
  tooltipHost?.remove()
  tooltipHost = null
}

/** 横轴周期列表：日粒度且范围 ≤62 天时把空日补零（日期枚举下沉 lib/time 单点），否则按数据出现的周期排序。 */
function periods(): string[] {
  const present = props.trend.periods
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
  const indexes = new Map(props.trend.periods.map((period, index) => [period, index]))
  chart.setOption(
    {
      animation: false,
      grid: { top: 4, right: 4, bottom: 2, left: 2, containLabel: true },
      tooltip: {
        trigger: "axis",
        triggerOn: "mousemove|click",
        enterable: true,
        confine: false,
        appendTo: tooltipParent,
        extraCssText:
          "max-width:calc(100vw - 16px);max-height:min(70vh,240px);overflow:auto;white-space:normal;pointer-events:auto;z-index:4000;",
        position: (
          point: number[],
          _params: unknown,
          dom: HTMLElement,
          _rect: unknown,
          size: { contentSize: number[] },
        ) => {
          dom.tabIndex = 0
          dom.setAttribute("role", "region")
          dom.setAttribute("aria-label", "趋势明细，可滚动查看全部维度")
          const chartRect = root.value?.getBoundingClientRect()
          const originX = chartRect?.left ?? 0
          const originY = chartRect?.top ?? 0
          const boxWidth = size.contentSize[0] || 220
          const boxHeight = Math.min(size.contentSize[1] || 160, window.innerHeight * 0.7, 240)
          let left = point[0] + 12
          let top = point[1] + 12
          if (originX + left + boxWidth > window.innerWidth - 8) left = point[0] - boxWidth - 12
          if (originY + top + boxHeight > window.innerHeight - 8) top = point[1] - boxHeight - 12
          if (originX + left < 8) left = 8 - originX
          if (originY + top < 8) top = 8 - originY
          return [left, top]
        },
        axisPointer: { type: "shadow" },
        valueFormatter: (value: unknown) => formatNumber(Number(value)),
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
        axisLabel: { color: theme.text, fontSize: 10, formatter: (value: number) => formatNumber(value) },
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
window.addEventListener(SESSION_CLEARING_EVENT, retireTooltip)
onBeforeUnmount(() => {
  window.removeEventListener(SESSION_CLEARING_EVENT, retireTooltip)
  retireTooltip()
})
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="按维度堆叠的发送趋势"></div></template>
