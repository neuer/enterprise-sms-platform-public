import { flushPromises, mount } from "@vue/test-utils"
import { defineComponent, h, nextTick, onMounted, ref } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { usePolling } from "../src/composables/usePolling"

/** 模拟页面可见性切换并派发 visibilitychange 事件。 */
function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", { value: state, configurable: true })
  document.dispatchEvent(new Event("visibilitychange"))
}

describe("统一轮询 usePolling", () => {
  beforeEach(() => {
    vi.useFakeTimers()
    setVisibility("visible")
  })

  afterEach(() => {
    setVisibility("visible")
    vi.useRealTimers()
  })

  it("按固定间隔执行，stop 后不再执行", async () => {
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000 })
    polling.start()
    expect(task).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(task).toHaveBeenCalledTimes(3)

    polling.stop()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).toHaveBeenCalledTimes(3)
  })

  it("start 幂等：重复调用不产生第二条轮询链", async () => {
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, immediate: true })
    polling.start()
    polling.start()
    polling.start()
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2_000)
    expect(task).toHaveBeenCalledTimes(3)
    polling.stop()
  })

  it("immediate 启动时立即执行一次", async () => {
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, immediate: true })
    polling.start()
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(2)
    polling.stop()
  })

  it("页面隐藏时暂停，恢复可见时默认立即补刷一次", async () => {
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000 })
    polling.start()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)

    setVisibility("hidden")
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).toHaveBeenCalledTimes(1)

    setVisibility("visible")
    expect(task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(3)
    polling.stop()
  })

  it("resumeImmediate 为 false 时恢复可见后等待下个周期", async () => {
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, resumeImmediate: false })
    polling.start()
    setVisibility("hidden")
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).not.toHaveBeenCalled()

    setVisibility("visible")
    expect(task).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)
    polling.stop()
  })

  it("enabled 为 false 时挂起，恢复为 true 且开启 immediate 时立即补一次", async () => {
    const enabled = ref(true)
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, immediate: true, enabled })
    polling.start()
    expect(task).toHaveBeenCalledTimes(1)

    enabled.value = false
    await nextTick()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).toHaveBeenCalledTimes(1)

    enabled.value = true
    await nextTick()
    expect(task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(3)
    polling.stop()
  })

  it("enabled 恢复为 true 且未开启 immediate 时等待下个周期", async () => {
    const enabled = ref(false)
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, enabled })
    polling.start()
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).not.toHaveBeenCalled()

    enabled.value = true
    await nextTick()
    expect(task).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)
    polling.stop()
  })

  it("任务在途时不会重叠执行，完成后才安排下一周期", async () => {
    let releaseTask: (() => void) | undefined
    const task = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          releaseTask = resolve
        }),
    )
    const polling = usePolling(task, { intervalMs: 1_000 })
    polling.start()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(3_000)
    expect(task).toHaveBeenCalledTimes(1)

    releaseTask?.()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(2)
    polling.stop()
  })

  it("组件卸载时自动停止并移除可见性监听", async () => {
    const task = vi.fn()
    const Probe = defineComponent({
      setup() {
        const polling = usePolling(task, { intervalMs: 1_000 })
        onMounted(polling.start)
        return () => h("div")
      },
    })
    const wrapper = mount(Probe)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(1)

    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(5_000)
    setVisibility("hidden")
    setVisibility("visible")
    await flushPromises()
    expect(task).toHaveBeenCalledTimes(1)
  })

  it("任务返回 true 视为终态：停止轮询且不触发 onTimeout，可再次 start", async () => {
    const onTimeout = vi.fn()
    const task = vi.fn().mockReturnValueOnce(undefined).mockReturnValueOnce(true).mockReturnValue(undefined)
    const polling = usePolling(task, { intervalMs: 1_000, maxAttempts: 5, onTimeout })
    polling.start()
    await vi.advanceTimersByTimeAsync(1_000)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(10_000)
    expect(task).toHaveBeenCalledTimes(2)
    expect(onTimeout).not.toHaveBeenCalled()

    polling.start()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(3)
    polling.stop()
  })

  it("达到 maxAttempts 上限触发 onTimeout 并停止", async () => {
    const onTimeout = vi.fn()
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, maxAttempts: 3, onTimeout })
    polling.start()
    await vi.advanceTimersByTimeAsync(3_000)
    expect(task).toHaveBeenCalledTimes(3)
    expect(onTimeout).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(10_000)
    expect(task).toHaveBeenCalledTimes(3)
    expect(onTimeout).toHaveBeenCalledTimes(1)
  })

  it("达到 maxDurationMs 上限后不再发请求，直接触发 onTimeout", async () => {
    const onTimeout = vi.fn()
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, maxDurationMs: 2_500, onTimeout })
    polling.start()
    await vi.advanceTimersByTimeAsync(1_000)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(2)

    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(2)
    expect(onTimeout).toHaveBeenCalledTimes(1)
  })

  it("restart 重置执行计数并立即执行一次", async () => {
    const onTimeout = vi.fn()
    const task = vi.fn()
    const polling = usePolling(task, { intervalMs: 1_000, maxAttempts: 2, onTimeout })
    polling.start()
    await vi.advanceTimersByTimeAsync(2_000)
    expect(task).toHaveBeenCalledTimes(2)
    expect(onTimeout).toHaveBeenCalledTimes(1)

    polling.restart()
    expect(task).toHaveBeenCalledTimes(3)
    expect(onTimeout).toHaveBeenCalledTimes(1)

    // 新一轮独立计数：第 2 次执行后再次到达上限并停止
    await vi.advanceTimersByTimeAsync(1_000)
    expect(task).toHaveBeenCalledTimes(4)
    expect(onTimeout).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(task).toHaveBeenCalledTimes(4)
  })
})
