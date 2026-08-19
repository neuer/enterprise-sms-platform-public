<script setup lang="ts">
import { computed } from "vue"

import type { DashboardBalancePoint } from "../api/dashboard"
import { CHART_COLORS } from "../lib/chartTheme"

const props = defineProps<{ points: DashboardBalancePoint[] }>()

const WIDTH = 280
const HEIGHT = 64
const PAD_TOP = 4
const PAD_BOTTOM = 12

const spark = computed(() => {
  const values = props.points.map((item) => item.balance)
  if (values.length === 0) return { line: "", area: "" }
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM
  const last = values.length - 1
  const coords = values.map((value, index) => {
    const x = last === 0 ? WIDTH / 2 : (index / last) * WIDTH
    const y = PAD_TOP + (1 - (value - min) / span) * plotHeight
    return `${x.toFixed(1)},${y.toFixed(1)}`
  })
  const line = coords.join(" ")
  return { line, area: `${line} ${WIDTH},${HEIGHT} 0,${HEIGHT}` }
})
</script>

<template>
  <svg
    class="balance-spark"
    data-testid="balance-spark"
    viewBox="0 0 280 64"
    width="100%"
    height="64"
    role="img"
    aria-label="最近十四日厂商余额走势"
  >
    <polygon :points="spark.area" :fill="CHART_COLORS.greenArea" />
    <polyline :points="spark.line" fill="none" :stroke="CHART_COLORS.green" stroke-width="1.6" />
  </svg>
</template>
