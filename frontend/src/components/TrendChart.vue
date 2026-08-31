<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { DashboardTrendPoint } from "../api/dashboard"
import { CHART_COLORS, CHART_TOOLTIP_STYLE } from "../lib/chartTheme"
import { shanghaiDateKey } from "../lib/time"

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ points: DashboardTrendPoint[] }>()

const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null
let observer: ResizeObserver | null = null

function axisLabel(statDate: string): string {
  return statDate === shanghaiDateKey() ? "今天" : statDate.slice(5)
}

function render(): void {
  if (!chart) return
  chart.setOption({
    animation: false,
    grid: { top: 4, right: 4, bottom: 2, left: 2, containLabel: true },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      valueFormatter: (value: unknown) => Number(value).toLocaleString(),
      ...CHART_TOOLTIP_STYLE,
    },
    xAxis: {
      type: "category",
      data: props.points.map((item) => axisLabel(item.stat_date)),
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisTick: { show: false },
      axisLabel: { color: CHART_COLORS.text, fontFamily: "IBM Plex Mono", fontSize: 10 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
      axisLabel: { color: CHART_COLORS.text, fontSize: 10, formatter: (value: number) => value.toLocaleString() },
    },
    series: [
      { name: "验证码", type: "bar", stack: "total", barMaxWidth: 42, data: props.points.map((item) => item.verify), itemStyle: { color: CHART_COLORS.green } },
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
  if (typeof ResizeObserver !== "undefined") {
    observer = new ResizeObserver(resize)
    observer.observe(root.value)
  }
  window.addEventListener("resize", resize)
})

watch(() => props.points, render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="root" class="trend-chart" role="img" aria-label="近七日按类目发送趋势"></div>
</template>
