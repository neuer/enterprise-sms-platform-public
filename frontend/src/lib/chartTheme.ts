/**
 * ECharts 深色配色：与 styles/theme.css 的设计令牌保持一致。
 * canvas 渲染无法读取 CSS 变量，此处为唯一同步点；调整主题时同步更新。
 */
export const CHART_COLORS = {
  /** --tx-2：轴标签与图例文字 */
  text: "#8b978f",
  /** 轴线，略亮于 --hair */
  axisLine: "rgba(255, 255, 255, 0.14)",
  /** 分隔网格线，介于 --hair 与 --hair-2 之间 */
  splitLine: "rgba(255, 255, 255, 0.07)",
  /** --verdi-l：深底上的主序列绿 */
  green: "#2fa184",
  greenArea: "rgba(47, 161, 132, 0.14)",
  /** 次类目蓝：与 category-strip 的 --slate 同源、深底下调亮 */
  blue: "#6f9bcf",
  /** --amber：次序列与阈值线 */
  amber: "#d8a35c",
  /** 维度堆叠第四序列紫（--violet 同源） */
  violet: "#9b7ed9",
  /** 维度堆叠第五序列灰绿 */
  teal: "#5b8c7b",
  /** 维度堆叠「其他」归并序列：--tx-3 同源灰 */
  dimOther: "#6d7a72",
} as const

/** 报表趋势图维度堆叠序列的固定取色顺序（Top 5 + 其他）。 */
export const CHART_DIM_PALETTE = [
  CHART_COLORS.green,
  CHART_COLORS.blue,
  CHART_COLORS.amber,
  CHART_COLORS.violet,
  CHART_COLORS.teal,
  CHART_COLORS.dimOther,
] as const

/** 深色面板风格的 tooltip 外观（--panel-2 / --hair / --tx）。 */
export const CHART_TOOLTIP_STYLE = {
  backgroundColor: "#141d19",
  borderColor: "rgba(255, 255, 255, 0.08)",
  textStyle: { color: "#c9d2cc" },
} as const
