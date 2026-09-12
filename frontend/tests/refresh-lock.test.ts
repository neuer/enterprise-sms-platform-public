import { afterEach, vi } from "vitest"

import { detectSessionMode, hasWebLocks, isSafeSingleTabMode, withRefreshLock } from "../src/api/refreshLock"
import {
  beginRefreshTabBinding,
  getRefreshTabBinding,
  REFRESH_TAB_ID_KEY,
  resetAccessSessionModule,
} from "../src/api/sessionTokens"

describe("跨标签页 Refresh Lock", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    resetAccessSessionModule()
  })

  it("没有 Web Locks 时本页串行且进入 Access-Only", async () => {
    vi.stubGlobal("navigator", {})
    expect(hasWebLocks()).toBe(false)
    expect(isSafeSingleTabMode()).toBe(true)
    expect(detectSessionMode()).toBe("access_only")

    const order: string[] = []
    let releaseFirst!: () => void
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    const first = withRefreshLock(async () => {
      order.push("first-enter")
      await firstGate
      order.push("first-leave")
      return "one"
    })
    const second = withRefreshLock(async () => {
      order.push("second")
      return "two"
    })

    await Promise.resolve()
    expect(order).toEqual(["first-enter"])
    releaseFirst()
    await expect(Promise.all([first, second])).resolves.toEqual(["one", "two"])
    expect(order).toEqual(["first-enter", "first-leave", "second"])
  })

  it("两个标签页共享同一把锁时串行且只让第二个等待", async () => {
    let current: Promise<void> = Promise.resolve()
    const order: string[] = []
    const locks = {
      request: async (_name: string, _options: { signal?: AbortSignal }, callback: () => Promise<string>) => {
        const previous = current
        let release!: () => void
        current = new Promise<void>((resolve) => {
          release = resolve
        })
        await previous
        try {
          return await callback()
        } finally {
          release()
        }
      },
    }
    vi.stubGlobal("navigator", { locks })
    expect(isSafeSingleTabMode()).toBe(false)

    let releaseFirst!: () => void
    const firstGate = new Promise<void>((resolve) => {
      releaseFirst = resolve
    })
    const first = withRefreshLock(async () => {
      order.push("first-enter")
      await firstGate
      order.push("first-leave")
      return "one"
    })
    const second = withRefreshLock(async () => {
      order.push("second")
      return "two"
    })

    await Promise.resolve()
    expect(order).toEqual(["first-enter"])
    releaseFirst()
    await expect(Promise.all([first, second])).resolves.toEqual(["one", "two"])
    expect(order).toEqual(["first-enter", "first-leave", "second"])
  })

  it("Access-Only 拒绝建立 Refresh 绑定且不得消费旧 sessionStorage", () => {
    vi.stubGlobal("navigator", {})
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, "a".repeat(32))

    expect(() => beginRefreshTabBinding()).toThrow("短会话模式不得建立 Refresh 标签页绑定")
    expect(sessionStorage.getItem(REFRESH_TAB_ID_KEY)).toBe("a".repeat(32))
    expect(getRefreshTabBinding()).toBeNull()
  })
})

it.each(["timeout", "cancel"])("本地队列 %s 后不会在持锁者释放时补做请求", async (mode) => {
  vi.stubGlobal("navigator", {})
  vi.useFakeTimers()
  let release!: () => void
  const holder = withRefreshLock(
    () =>
      new Promise<void>((done) => {
        release = done
      }),
  )
  const controller = new AbortController()
  const write = vi.fn()
  const waiter = withRefreshLock(write, { signal: controller.signal })
  const rejected = expect(waiter).rejects.toMatchObject({ name: mode === "timeout" ? "TimeoutError" : "AbortError" })
  if (mode === "timeout") await vi.advanceTimersByTimeAsync(10_000)
  else controller.abort()
  await rejected
  release()
  await holder
  await withRefreshLock(async () => {})
  expect(write).not.toHaveBeenCalled()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

it("Web Locks 等待超时通过 signal 撤销真实排队请求", async () => {
  vi.useFakeTimers()
  let signal!: AbortSignal
  const write = vi.fn()
  vi.stubGlobal("navigator", {
    locks: {
      request: (_name: string, options: { signal: AbortSignal }) => {
        signal = options.signal
        return new Promise((_resolve, reject) =>
          signal.addEventListener("abort", () => reject(signal.reason), { once: true }),
        )
      },
    },
  })
  const pending = withRefreshLock(write)
  const rejected = expect(pending).rejects.toMatchObject({ name: "TimeoutError" })
  await vi.advanceTimersByTimeAsync(10_000)
  await rejected
  expect(signal.aborted).toBe(true)
  expect(write).not.toHaveBeenCalled()
  vi.useRealTimers()
  vi.unstubAllGlobals()
})
