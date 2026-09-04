import * as echarts from "echarts/core"
import { onBeforeUnmount, onMounted, type Ref } from "vue"

import { THEME_CHANGE_EVENT } from "../lib/theme"

/**
 * ECharts 实例装配单点：挂载时初始化并首次渲染，监听容器与窗口 resize，
 * 主题切换按新令牌重渲染，组件卸载时断开监听并销毁实例。
 * render 由调用方提供（接收当前实例写 setOption），实例未就绪时调度方自动跳过。
 */
export function useChart(
  root: Ref<HTMLElement | null>,
  render: (chart: echarts.ECharts) => void,
): { render: () => void } {
  let chart: echarts.ECharts | null = null
  let observer: ResizeObserver | null = null

  function renderIfReady(): void {
    if (!chart) return
    render(chart)
  }

  function resize(): void {
    chart?.resize()
  }

  onMounted(() => {
    if (!root.value) return
    chart = echarts.init(root.value)
    render(chart)
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(resize)
      observer.observe(root.value)
    }
    window.addEventListener("resize", resize)
    window.addEventListener(THEME_CHANGE_EVENT, renderIfReady)
  })

  onBeforeUnmount(() => {
    window.removeEventListener("resize", resize)
    window.removeEventListener(THEME_CHANGE_EVENT, renderIfReady)
    observer?.disconnect()
    observer = null
    chart?.dispose()
    chart = null
  })

  return { render: renderIfReady }
}
