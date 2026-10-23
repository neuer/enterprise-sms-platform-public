import { flushPromises, mount } from "@vue/test-utils"
import { defineComponent, h, ref } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

const chart = vi.hoisted(() => ({ resize: vi.fn(), dispose: vi.fn(), setOption: vi.fn() }))
vi.mock("echarts/core", () => ({ init: vi.fn(() => chart), use: vi.fn() }))

import { useChart } from "../src/composables/useChart"

class FakeObserver {
  static latest: FakeObserver | null = null
  callback: ResizeObserverCallback
  disconnected = false
  constructor(callback: ResizeObserverCallback) {
    this.callback = callback
    FakeObserver.latest = this
  }
  observe(): void {}
  disconnect(): void {
    this.disconnected = true
  }
  notify(width: number, height: number): void {
    this.callback([{ contentRect: { width, height } } as ResizeObserverEntry], this as unknown as ResizeObserver)
  }
}

describe("useChart 尺寸调度", () => {
  let frame: FrameRequestCallback | null = null

  afterEach(() => {
    frame = null
    chart.resize.mockClear()
    chart.dispose.mockClear()
    vi.unstubAllGlobals()
  })

  function runFrame(): void {
    const callback = frame
    frame = null
    callback?.(0)
  }

  function mountChart() {
    vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
      frame = callback
      return 1
    })
    vi.stubGlobal("cancelAnimationFrame", () => {
      frame = null
    })
    vi.stubGlobal("ResizeObserver", FakeObserver)
    const root = ref<HTMLElement | null>(null)
    const wrapper = mount(
      defineComponent({
        setup() {
          useChart(root, (instance) => {
            instance.setOption({ series: [] })
          })
          return () => h("div", { ref: root })
        },
      }),
    )
    return wrapper
  }

  it("同一帧多次通知只 resize 一次，相同尺寸不重复", async () => {
    const wrapper = mountChart()
    await flushPromises()
    const observer = FakeObserver.latest
    expect(observer).toBeTruthy()
    observer!.notify(180, 90)
    observer!.notify(180, 90)
    expect(chart.resize).not.toHaveBeenCalled()
    runFrame()
    expect(chart.resize).toHaveBeenCalledTimes(1)
    expect(chart.resize).toHaveBeenCalledWith({ width: 180, height: 90 })

    observer!.notify(180, 90)
    runFrame()
    expect(chart.resize).toHaveBeenCalledTimes(1)

    observer!.notify(0, 0)
    runFrame()
    expect(chart.resize).toHaveBeenCalledTimes(1)
    wrapper.unmount()
    expect(chart.dispose).toHaveBeenCalledTimes(1)
    expect(observer!.disconnected).toBe(true)
  })
})
