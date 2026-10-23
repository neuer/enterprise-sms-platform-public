import * as echarts from "echarts/core"
import { onBeforeUnmount, onMounted, type Ref } from "vue"

import { THEME_CHANGE_EVENT } from "../lib/theme"

/**
 * ECharts 实例装配单点：挂载时初始化并首次渲染，监听容器与窗口 resize，
 * 主题切换按新令牌重渲染，组件卸载时断开监听并销毁实例。
 * render 由调用方提供（接收当前实例写 setOption），实例未就绪时调度方自动跳过。
 * resize 合并到同一动画帧，尺寸未变或为 0 时不调用，避免尺寸反馈循环。
 */
export function useChart(
  root: Ref<HTMLElement | null>,
  render: (chart: echarts.ECharts) => void,
): { render: () => void } {
  let chart: echarts.ECharts | null = null
  let observer: ResizeObserver | null = null
  let frame = 0
  let pendingWidth = 0
  let pendingHeight = 0
  let appliedWidth = -1
  let appliedHeight = -1

  function renderIfReady(): void {
    if (!chart) return
    render(chart)
  }

  function applyResize(): void {
    frame = 0
    if (!chart) return
    const width = pendingWidth
    const height = pendingHeight
    if (width <= 0 || height <= 0) return
    if (width === appliedWidth && height === appliedHeight) return
    appliedWidth = width
    appliedHeight = height
    chart.resize({ width, height })
  }

  /** 记录最新尺寸，同一帧内的多次通知只 resize 一次。 */
  function scheduleResize(width: number, height: number): void {
    pendingWidth = width
    pendingHeight = height
    if (frame) return
    frame = window.requestAnimationFrame(applyResize)
  }

  function resizeFromElement(): void {
    const element = root.value
    if (!element) return
    const rect = element.getBoundingClientRect()
    scheduleResize(rect.width, rect.height)
  }

  function resizeFromObserver(entries: ResizeObserverEntry[]): void {
    const entry = entries[entries.length - 1]
    if (!entry) return
    scheduleResize(entry.contentRect.width, entry.contentRect.height)
  }

  onMounted(() => {
    const element = root.value
    if (!element) return
    chart = echarts.init(element)
    const rect = element.getBoundingClientRect()
    appliedWidth = rect.width
    appliedHeight = rect.height
    render(chart)
    if (typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver(resizeFromObserver)
      observer.observe(element)
    }
    window.addEventListener("resize", resizeFromElement)
    window.addEventListener(THEME_CHANGE_EVENT, renderIfReady)
  })

  onBeforeUnmount(() => {
    window.removeEventListener("resize", resizeFromElement)
    window.removeEventListener(THEME_CHANGE_EVENT, renderIfReady)
    observer?.disconnect()
    observer = null
    if (frame) {
      window.cancelAnimationFrame(frame)
      frame = 0
    }
    chart?.dispose()
    chart = null
  })

  return { render: renderIfReady }
}
