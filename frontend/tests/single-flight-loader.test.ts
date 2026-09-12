import { describe, expect, it, vi } from "vitest"
import { singleFlightLoader } from "../src/lib/singleFlightLoader"

describe("工作区资源加载", () => {
  it("失败释放单飞缓存，下一次加载成功后只初始化一次", async () => {
    const work = vi.fn().mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce("ready")
    const load = singleFlightLoader(work)
    const first = load()
    expect(load()).toBe(first)
    await expect(first).rejects.toThrow("network")
    const retry = load()
    expect(retry).not.toBe(first)
    expect(load()).toBe(retry)
    await expect(retry).resolves.toBe("ready")
    await expect(load()).resolves.toBe("ready")
    expect(work).toHaveBeenCalledTimes(2)
  })
})
