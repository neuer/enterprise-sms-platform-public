/**
 * ECharts 配色：canvas 渲染读不到 CSS 变量，这里在运行时统一读取
 * styles/theme.css 的 --chart-* 令牌（亮/暗主题各一份），是图表与主题的唯一同步点。
 * 主题切换（lib/theme.ts 派发 sms:theme-change）后调用方应重新取色并重建 option。
 */

export interface ChartTheme {
  /** --chart-text：轴标签与图例文字 */
  text: string
  /** --chart-axis：轴线 */
  axisLine: string
  /** --chart-split：分隔网格线 */
  splitLine: string
  /** --chart-green：主序列绿 */
  green: string
  greenArea: string
  /** --chart-blue：次类目蓝 */
  blue: string
  /** --chart-amber：次序列与阈值线 */
  amber: string
  /** --chart-violet：维度堆叠第四序列 */
  violet: string
  /** --chart-teal：维度堆叠第五序列 */
  teal: string
  /** --chart-dim：维度堆叠「其他」归并序列 */
  dimOther: string
  /** 维度堆叠序列的固定取色顺序（Top 5 + 其他） */
  dimPalette: string[]
  /** tooltip 外观（--chart-tip-*） */
  tooltip: { backgroundColor: string; borderColor: string; textStyle: { color: string } }
}

/** 读取根元素令牌值；令牌均为纯颜色字面量，取不到时回退深色值（测试/非浏览器环境）。 */
function token(name: string, fallback: string): string {
  if (typeof getComputedStyle !== "function") return fallback
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return value || fallback
}

/** 维度堆叠取色对应的令牌名，顺序与 ChartTheme.dimPalette 一致；DOM 图例用 var() 引用可随主题自动切换。 */
export const CHART_DIM_VARS = [
  "--chart-green",
  "--chart-blue",
  "--chart-amber",
  "--chart-violet",
  "--chart-teal",
  "--chart-dim",
] as const

/** 按当前主题取一整套图表配色；主题切换后必须重新调用。 */
export function getChartTheme(): ChartTheme {
  const green = token("--chart-green", "#2fa184")
  const blue = token("--chart-blue", "#6f9bcf")
  const amber = token("--chart-amber", "#d8a35c")
  const violet = token("--chart-violet", "#9b7ed9")
  const teal = token("--chart-teal", "#5b8c7b")
  const dimOther = token("--chart-dim", "#6d7a72")
  return {
    text: token("--chart-text", "#8b978f"),
    axisLine: token("--chart-axis", "rgba(255, 255, 255, 0.14)"),
    splitLine: token("--chart-split", "rgba(255, 255, 255, 0.07)"),
    green,
    greenArea: token("--chart-green-area", "rgba(47, 161, 132, 0.14)"),
    blue,
    amber,
    violet,
    teal,
    dimOther,
    dimPalette: [green, blue, amber, violet, teal, dimOther],
    tooltip: {
      backgroundColor: token("--chart-tip-bg", "#141d19"),
      borderColor: token("--chart-tip-border", "rgba(255, 255, 255, 0.08)"),
      textStyle: { color: token("--chart-tip-text", "#c9d2cc") },
    },
  }
}
