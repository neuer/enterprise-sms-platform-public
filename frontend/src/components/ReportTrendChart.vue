<script setup lang="ts">
import { LineChart } from "echarts/charts"
import { GridComponent, LegendComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { ReportRow } from "../api/reports"

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ items: ReportRow[] }>()
const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

function trend(): Array<{ period: string; total: number; segments: number }> {
  const buckets = new Map<string, { period: string; total: number; segments: number }>()
  for (const item of props.items) {
    const bucket = buckets.get(item.period_start) ?? { period: item.period_start, total: 0, segments: 0 }
    bucket.total += item.total
    bucket.segments += item.total_segments
    buckets.set(item.period_start, bucket)
  }
  return [...buckets.values()].sort((left, right) => left.period.localeCompare(right.period))
}

function render(): void {
  if (!chart) return
  const values = trend()
  chart.setOption({
    animation: false,
    color: ["#0e7a63", "#a8650b"],
    grid: { top: 42, right: 24, bottom: 32, left: 54 },
    legend: { top: 3, right: 4, textStyle: { color: "#5b6862", fontSize: 10 } },
    tooltip: { trigger: "axis" },
    xAxis: {
      type: "category", boundaryGap: false, data: values.map((item) => item.period),
      axisLabel: { color: "#8a948e", fontFamily: "IBM Plex Mono" },
      axisLine: { lineStyle: { color: "#d3d8d1" } },
    },
    yAxis: {
      type: "value", minInterval: 1,
      axisLabel: { color: "#8a948e" },
      splitLine: { lineStyle: { color: "#e9ece8", type: "dashed" } },
    },
    series: [
      { name: "消息数", type: "line", data: values.map((item) => item.total), symbolSize: 6, lineStyle: { width: 2 } },
      { name: "计费条", type: "line", data: values.map((item) => item.segments), symbolSize: 6, lineStyle: { width: 2 } },
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
watch(() => props.items, render, { deep: true })
onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  chart?.dispose()
  chart = null
})
</script>

<template><div ref="root" class="report-trend-chart" role="img" aria-label="消息数与计费条趋势"></div></template>
