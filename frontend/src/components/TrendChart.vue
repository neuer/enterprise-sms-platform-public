<script setup lang="ts">
import { BarChart } from "echarts/charts"
import { GridComponent, TooltipComponent } from "echarts/components"
import * as echarts from "echarts/core"
import { CanvasRenderer } from "echarts/renderers"
import { ref, watch } from "vue"

import type { DashboardTrendPoint } from "../api/dashboard"
import { useChart } from "../composables/useChart"
import { getChartTheme } from "../lib/chartTheme"
import { shanghaiDateKey } from "../lib/time"

echarts.use([BarChart, GridComponent, TooltipComponent, CanvasRenderer])

const props = defineProps<{ points: DashboardTrendPoint[] }>()

const root = ref<HTMLElement | null>(null)

function axisLabel(statDate: string): string {
  return statDate === shanghaiDateKey() ? "今天" : statDate.slice(5)
}

const { render } = useChart(root, (chart) => {
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
      {
        name: "验证码",
        type: "bar",
        stack: "total",
        barMaxWidth: 42,
        data: props.points.map((item) => item.verify),
        itemStyle: { color: theme.green },
      },
      {
        name: "通知",
        type: "bar",
        stack: "total",
        data: props.points.map((item) => item.notice),
        itemStyle: { color: theme.blue },
      },
      {
        name: "营销",
        type: "bar",
        stack: "total",
        data: props.points.map((item) => item.market),
        itemStyle: { color: theme.amber },
      },
    ],
  })
})

watch(() => props.points, render, { deep: true })
</script>

<template>
  <div ref="root" class="trend-chart" role="img" aria-label="近七日按类目发送趋势"></div>
</template>
