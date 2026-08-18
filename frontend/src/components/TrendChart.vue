<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { DashboardTrendPoint } from "../api/dashboard"
import { CHART_COLORS, CHART_TOOLTIP_STYLE } from "../lib/chartTheme"

echarts.use([BarChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ points: DashboardTrendPoint[] }>()

const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

function render(): void {
  if (!chart) return
  chart.setOption({
    animation: false,
    grid: { top: 34, right: 16, bottom: 28, left: 52 },
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
      data: props.points.map((item) => item.stat_date.slice(5)),
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
      axisLabel: { color: CHART_COLORS.text, fontFamily: "IBM Plex Mono" },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
      axisLabel: { color: CHART_COLORS.text, formatter: (value: number) => value.toLocaleString() },
    },
    series: [
      { name: "验证码", type: "bar", stack: "total", barMaxWidth: 34, data: props.points.map((item) => item.verify), itemStyle: { color: CHART_COLORS.green } },
      { name: "通知", type: "bar", stack: "total", data: props.points.map((item) => item.notice), itemStyle: { color: CHART_COLORS.blue } },
      { name: "营销", type: "bar", stack: "total", data: props.points.map((item) => item.market), itemStyle: { color: CHART_COLORS.amber } },
    ],
  })
}

function resize(): void {
  chart?.resize()
}

onMounted(() => {
  if (!root.value) return
  chart = echarts.init(root.value)
  render()
  window.addEventListener("resize", resize)
})

watch(() => props.points, render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="root" class="trend-chart" role="img" aria-label="近七日按类目发送趋势"></div>
</template>
