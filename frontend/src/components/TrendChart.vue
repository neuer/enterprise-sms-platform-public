<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { DashboardTrendPoint } from "../api/dashboard"
import { getChartTheme } from "../lib/chartTheme"
import { THEME_CHANGE_EVENT } from "../lib/theme"
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
  const theme = getChartTheme()
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
      data: props.points.map((item) => axisLabel(item.stat_date)),
      axisLine: { lineStyle: { color: theme.axisLine } },
      axisTick: { show: false },
      axisLabel: { color: theme.text, fontFamily: "IBM Plex Mono", fontSize: 10 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: theme.splitLine, type: "dashed" } },
      axisLabel: { color: theme.text, fontSize: 10, formatter: (value: number) => value.toLocaleString() },
    },
    series: [
      { name: "验证码", type: "bar", stack: "total", barMaxWidth: 42, data: props.points.map((item) => item.verify), itemStyle: { color: theme.green } },
      { name: "通知", type: "bar", stack: "total", data: props.points.map((item) => item.notice), itemStyle: { color: theme.blue } },
      { name: "营销", type: "bar", stack: "total", data: props.points.map((item) => item.market), itemStyle: { color: theme.amber } },
    ],
  })
}

function resize(): void {
  chart?.resize()
}

/** 主题切换后 canvas 需按新令牌重建配色。 */
function onThemeChange(): void {
  render()
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
  window.addEventListener(THEME_CHANGE_EVENT, onThemeChange)
})

watch(() => props.points, render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  window.removeEventListener(THEME_CHANGE_EVENT, onThemeChange)
  observer?.disconnect()
  observer = null
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="root" class="trend-chart" role="img" aria-label="近七日按类目发送趋势"></div>
</template>
