import { vi } from "vitest"

import { copyText } from "../src/lib/clipboard"

function stubClipboardApi(writeText: (value: string) => Promise<void>): void {
  Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true })
  Object.defineProperty(window, "isSecureContext", { value: true, configurable: true })
}

function stubInsecureContext(execCommand: ReturnType<typeof vi.fn>): void {
  Object.defineProperty(window, "isSecureContext", { value: false, configurable: true })
  Object.defineProperty(document, "execCommand", { value: execCommand, configurable: true })
}

describe("剪贴板单点 copyText", () => {
  it("空串直接返回 false，不触碰任何剪贴板通道", async () => {
    const writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined)
    stubClipboardApi(writeText)

    expect(await copyText("")).toBe(false)
    expect(writeText).not.toHaveBeenCalled()
  })

  it("安全上下文优先走异步剪贴板 API", async () => {
    const writeText = vi.fn<(value: string) => Promise<void>>().mockResolvedValue(undefined)
    stubClipboardApi(writeText)

    expect(await copyText("B202609050001")).toBe(true)
    expect(writeText).toHaveBeenCalledTimes(1)
    expect(writeText).toHaveBeenCalledWith("B202609050001")
  })

  it("异步剪贴板 API 抛错时返回 false 而不是向上抛出", async () => {
    const writeText = vi.fn<(value: string) => Promise<void>>().mockRejectedValue(new Error("denied"))
    stubClipboardApi(writeText)

    expect(await copyText("B202609050001")).toBe(false)
  })

  it("非安全上下文回退隐藏 textarea + execCommand，完成后移除节点", async () => {
    const execCommand = vi.fn().mockReturnValue(true)
    stubInsecureContext(execCommand)

    expect(await copyText("B202609050001")).toBe(true)
    expect(execCommand).toHaveBeenCalledWith("copy")
    expect(document.body.querySelector("textarea")).toBeNull()
  })

  it("execCommand 返回 false 时如实返回 false", async () => {
    const execCommand = vi.fn().mockReturnValue(false)
    stubInsecureContext(execCommand)

    expect(await copyText("B202609050001")).toBe(false)
    expect(document.body.querySelector("textarea")).toBeNull()
  })
})
