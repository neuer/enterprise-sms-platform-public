import { getCurrentInstance, onBeforeUnmount, toValue, watch, type MaybeRefOrGetter } from "vue"

/**
 * 轮询任务：返回 true 表示到达终态，轮询立即停止；
 * 返回 void / false 表示继续，等待下一周期。
 */
export type PollingTask = () => boolean | void | Promise<boolean | void>

export interface UsePollingOptions {
  /** 相邻两次执行的间隔（自上一次执行完成后起算，不会在途重入）。 */
  intervalMs: number
  /** 条件暂停：为 false 时挂起计时，恢复为 true 时按 immediate 语义补一次或等下个周期。 */
  enabled?: MaybeRefOrGetter<boolean>
  /** start() 时立即执行一次；enabled 由 false 恢复为 true 时同样立即补一次。默认 false。 */
  immediate?: boolean
  /** 页面由隐藏恢复可见时立即执行一次（默认 true）；false 时等下个周期。 */
  resumeImmediate?: boolean
  /** 终态轮询兜底：最多执行次数，达到后停止并回调 onTimeout。 */
  maxAttempts?: number
  /** 终态轮询兜底：自 start / restart 起算的总时长上限（毫秒）。 */
  maxDurationMs?: number
  /** 达到次数或时长上限时回调一次并停止；任务自行返回终态时不触发。 */
  onTimeout?: () => void
}

export interface PollingController {
  /** 幂等启动；已启动时调用为空操作。 */
  start: () => void
  /** 停止并清理计时器与可见性监听；任务在途时等待其完成但不再安排下一次。 */
  stop: () => void
  /** 重置执行计数与计时起点并立即重新开始（立即执行一次，不受 immediate 选项约束）。 */
  restart: () => void
}

/**
 * 全站统一轮询：页面隐藏自动暂停、恢复可见可立即补刷、支持 enabled 条件暂停与终态上限兜底。
 * 组件作用域内创建时随卸载自动停止；Pinia store 等非组件作用域由调用方手动 start / stop。
 */
export function usePolling(task: PollingTask, options: UsePollingOptions): PollingController {
  const { intervalMs, maxAttempts, maxDurationMs, onTimeout } = options
  const immediate = options.immediate ?? false
  const resumeImmediate = options.resumeImmediate ?? true
  const enabledSource = options.enabled ?? true

  let started = false
  let timer: number | undefined
  let attempts = 0
  let startedAt = 0
  let firing = false

  function isEnabled(): boolean {
    return toValue(enabledSource)
  }

  function pageVisible(): boolean {
    return document.visibilityState === "visible"
  }

  function canRun(): boolean {
    return started && isEnabled() && pageVisible()
  }

  function clearTimer(): void {
    if (timer === undefined) return
    window.clearTimeout(timer)
    timer = undefined
  }

  /** 次数与总时长上限：达到任一上限即视为兜底超时。 */
  function exceeded(): boolean {
    if (maxAttempts !== undefined && attempts >= maxAttempts) return true
    return maxDurationMs !== undefined && Date.now() - startedAt >= maxDurationMs
  }

  function schedule(): void {
    clearTimer()
    if (!canRun()) return
    timer = window.setTimeout(() => void fire(), intervalMs)
  }

  function timeoutStop(): void {
    stop()
    onTimeout?.()
  }

  /** 执行一次任务；完成后按终态 / 上限 / 暂停状态决定下一跳。 */
  async function fire(): Promise<void> {
    clearTimer()
    if (!canRun() || firing) return
    if (exceeded()) {
      timeoutStop()
      return
    }
    firing = true
    attempts += 1
    let terminal = false
    try {
      terminal = (await task()) === true
    } finally {
      firing = false
    }
    if (!started) return
    if (terminal) {
      stop()
      return
    }
    if (exceeded()) {
      timeoutStop()
      return
    }
    schedule()
  }

  function handleVisibilityChange(): void {
    if (!started) return
    if (!pageVisible()) {
      clearTimer()
      return
    }
    if (!isEnabled()) return
    if (resumeImmediate) void fire()
    else schedule()
  }

  function attachVisibility(): void {
    document.addEventListener("visibilitychange", handleVisibilityChange)
  }

  function detachVisibility(): void {
    document.removeEventListener("visibilitychange", handleVisibilityChange)
  }

  function begin(fireNow: boolean): void {
    started = true
    attempts = 0
    startedAt = Date.now()
    attachVisibility()
    if (fireNow) void fire()
    else schedule()
  }

  function start(): void {
    if (started) return
    begin(immediate)
  }

  function stop(): void {
    clearTimer()
    started = false
    attempts = 0
    startedAt = 0
    detachVisibility()
  }

  function restart(): void {
    stop()
    begin(true)
  }

  // enabled 恢复为 true 且页面可见时补一次或重排周期；变为 false 时挂起计时。
  watch(
    () => toValue(enabledSource),
    (enabled) => {
      if (!started) return
      if (!enabled) {
        clearTimer()
        return
      }
      if (!pageVisible()) return
      if (immediate) void fire()
      else schedule()
    },
  )

  if (getCurrentInstance()) {
    onBeforeUnmount(stop)
  }

  return { start, stop, restart }
}
