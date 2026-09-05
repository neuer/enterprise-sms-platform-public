import { afterEach, vi } from "vitest"

import {
  hasWebLocks,
  isSafeSingleTabMode,
  withRefreshLock,
} from "../src/api/refreshLock"
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

  it("没有 Web Locks 时本页串行且进入安全单标签页", async () => {
    vi.stubGlobal("navigator", {})
    expect(hasWebLocks()).toBe(false)
    expect(isSafeSingleTabMode()).toBe(true)

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
      request: async (_name: string, callback: () => Promise<string>) => {
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

  it("安全单标签页不把 refresh 绑定写入 sessionStorage，刷新后不得复活", () => {
    vi.stubGlobal("navigator", {})
    sessionStorage.setItem(REFRESH_TAB_ID_KEY, "a".repeat(32))

    const tabId = beginRefreshTabBinding()
    expect(tabId).toMatch(/^[0-9a-f]{32}$/)
    expect(sessionStorage.getItem(REFRESH_TAB_ID_KEY)).toBe("a".repeat(32))
    expect(getRefreshTabBinding()).toBe(tabId)

    resetAccessSessionModule()
    expect(getRefreshTabBinding()).toBeNull()
  })
})
