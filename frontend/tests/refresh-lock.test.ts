import { afterEach, vi } from "vitest"

import { withRefreshLock } from "../src/api/refreshLock"

describe("跨标签页 Refresh Lock", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("没有 Web Locks 时直接执行", async () => {
    vi.stubGlobal("navigator", {})
    const run = vi.fn().mockResolvedValue("ok")
    await expect(withRefreshLock(run)).resolves.toBe("ok")
    expect(run).toHaveBeenCalledOnce()
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
})
