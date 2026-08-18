<script setup lang="ts">
import { LineChart } from "echarts/charts"
import { GridComponent, MarkLineComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import type { DashboardBalancePoint } from "../api/dashboard"
import { CHART_COLORS, CHART_TOOLTIP_STYLE } from "../lib/chartTheme"

echarts.use([LineChart, GridComponent, MarkLineComponent, TooltipComponent, CanvasRenderer])

const props = withDefaults(defineProps<{ points: DashboardBalancePoint[]; threshold?: number | null }>(), {
  threshold: null,
})

const root = ref<HTMLElement | null>(null)
let chart: echarts.ECharts | null = null

function render(): void {
  if (!chart) return
  chart.setOption({
    animation: false,
    grid: { top: 24, right: 22, bottom: 30, left: 58 },
    tooltip: {
      trigger: "axis",
      valueFormatter: (value: unknown) => Number(value).toLocaleString(),
      ...CHART_TOOLTIP_STYLE,
    },
    xAxis: {
      type: "category",
      boundaryGap: false,
      data: props.points.map((item) => item.stat_date.slice(5)),
      axisLine: { lineStyle: { color: CHART_COLORS.axisLine } },
      axisLabel: { color: CHART_COLORS.text, fontFamily: "IBM Plex Mono" },
    },
    yAxis: {
      type: "value",
      scale: true,
      splitLine: { lineStyle: { color: CHART_COLORS.splitLine, type: "dashed" } },
      axisLabel: { color: CHART_COLORS.text, formatter: (value: number) => value.toLocaleString() },
    },
    series: [{
      name: "剩余计费条",
      type: "line",
      smooth: 0.2,
      symbol: "circle",
      symbolSize: 6,
      data: props.points.map((item) => item.balance),
      lineStyle: { width: 2, color: CHART_COLORS.green },
      itemStyle: { color: CHART_COLORS.green },
      areaStyle: { color: CHART_COLORS.greenArea },
      ...(props.threshold === null ? {} : { markLine: {
        silent: true,
        symbol: "none",
        label: { formatter: "告警阈值", color: CHART_COLORS.amber },
        lineStyle: { color: CHART_COLORS.amber, type: "dashed" },
        data: [{ yAxis: props.threshold }],
      } }),
    }],
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

watch(() => [props.points, props.threshold], render, { deep: true })

onBeforeUnmount(() => {
  window.removeEventListener("resize", resize)
  chart?.dispose()
  chart = null
})
</script>

<template>
  <div ref="root" class="balance-chart" role="img" aria-label="最近十四日厂商余额走势"></div>
</template>
