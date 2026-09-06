import { afterEach, describe, expect, it, vi } from "vitest"

import { saveBlob } from "../src/lib/download"

describe("Blob 下载单点 saveBlob", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("Blob 经临时 ObjectURL 触发锚点下载后立即回收", () => {
    const createObjectURL = vi.fn(() => "blob:mock-url")
    const revokeObjectURL = vi.fn()
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL, revokeObjectURL }))
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)

    const blob = new Blob(["csv"])
    saveBlob(blob, "sms-report-abc.csv")

    expect(createObjectURL).toHaveBeenCalledWith(blob)
    expect(click).toHaveBeenCalledTimes(1)
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:mock-url")
    // createObjectURL → click → revokeObjectURL 的调用次序
    const order = [createObjectURL, click, revokeObjectURL].map((spy) => spy.mock.invocationCallOrder[0])
    expect(order[0]).toBeLessThan(order[1])
    expect(order[1]).toBeLessThan(order[2])
  })
})
