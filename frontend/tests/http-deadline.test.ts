import { afterEach, describe, expect, it, vi } from "vitest"

import {
  AUTH_JSON_MAX_BYTES,
  HttpBodyError,
  createDeadline,
  fetchJsonWithDeadline,
  readJsonBody,
  readLimitedBlob,
} from "../src/api/httpDeadline"

function hangingBodyResponse(prefix = '{"ok":'): Response {
  return new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode(prefix))
      },
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  )
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("端到端 JSON Deadline", () => {
  it("响应头已到但正文停滞时仍在截止线内失败", async () => {
    vi.useFakeTimers()
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(hangingBodyResponse())),
    )

    const pending = fetchJsonWithDeadline(
      "/api/v1/web/auth/refresh",
      { method: "POST" },
      {
        timeoutMs: 10_000,
        maxBodyBytes: AUTH_JSON_MAX_BYTES,
        timeoutMessage: "认证请求超时",
      },
    )
    const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError", message: "认证请求超时" })
    await vi.advanceTimersByTimeAsync(9_999)
    await vi.advanceTimersByTimeAsync(1)
    await assertion
  })

  it("外部取消在响应头到达后仍停止正文读取", async () => {
    const caller = new AbortController()
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(hangingBodyResponse())),
    )

    const pending = fetchJsonWithDeadline(
      "/api/v1/web/auth/login",
      { method: "POST" },
      {
        timeoutMs: 55_000,
        callerSignal: caller.signal,
        maxBodyBytes: AUTH_JSON_MAX_BYTES,
      },
    )
    const assertion = expect(pending).rejects.toMatchObject({ name: "AbortError" })
    await Promise.resolve()
    caller.abort(new DOMException("会话已切换", "AbortError"))
    await assertion
  })

  it("声明的 Content-Length 超限时立即拒绝且不解析正文", async () => {
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new TextEncoder().encode('{"ok":true}'))
          controller.close()
        },
      }),
      {
        status: 200,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": String(AUTH_JSON_MAX_BYTES + 1),
        },
      },
    )
    const signal = new AbortController().signal
    await expect(readJsonBody(response, signal, AUTH_JSON_MAX_BYTES)).rejects.toMatchObject({
      name: "HttpBodyError",
      code: "RESPONSE_TOO_LARGE",
    })
  })

  it("实际正文超限时失败关闭", async () => {
    const response = new Response("x".repeat(AUTH_JSON_MAX_BYTES + 1), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
    await expect(readJsonBody(response, new AbortController().signal, AUTH_JSON_MAX_BYTES)).rejects.toBeInstanceOf(
      HttpBodyError,
    )
  })

  it("无效 JSON 不回显正文", async () => {
    const response = new Response("{", {
      status: 200,
      headers: { "Content-Type": "application/json" },
    })
    await expect(readJsonBody(response, new AbortController().signal, AUTH_JSON_MAX_BYTES)).rejects.toMatchObject({
      code: "INVALID_JSON_RESPONSE",
      message: "响应不是有效 JSON",
    })
  })

  it("204 与空正文视为无 JSON", async () => {
    const empty = new Response(null, { status: 204 })
    await expect(readJsonBody(empty, new AbortController().signal, AUTH_JSON_MAX_BYTES)).resolves.toBeNull()
  })
})

describe("标准 Response 正文", () => {
  it.each([null, ""])("200 空正文 %j 仍视为无 JSON", async (body) => {
    await expect(readJsonBody(new Response(body), new AbortController().signal, 32)).resolves.toBeNull()
  })

  it("拒绝无效 UTF-8，不回显原始字节", async () => {
    await expect(
      readJsonBody(new Response(new Uint8Array([0xff])), new AbortController().signal, 32),
    ).rejects.toMatchObject({ code: "INVALID_JSON_RESPONSE", message: "响应不是有效 JSON" })
  })

  it("下载按字节边界保留正文和 Content-Type，空正文返回空 Blob", async () => {
    const text = "测试,csv"
    const bytes = new TextEncoder().encode(text)
    const blob = await readLimitedBlob(
      new Response(bytes, { headers: { "Content-Type": "text/csv" } }),
      new AbortController().signal,
      bytes.byteLength,
    )
    expect(blob.size).toBe(bytes.byteLength)
    expect(blob.type).toBe("text/csv")
    const content = new Promise((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(reader.result)
      reader.onerror = () => reject(reader.error)
      reader.readAsText(blob)
    })
    await expect(content).resolves.toBe(text)
    const empty = await readLimitedBlob(new Response(null), new AbortController().signal, 32)
    expect(empty.size).toBe(0)
  })

  it("下载声明超限时在读取前拒绝", async () => {
    const response = new Response("csv", { headers: { "Content-Length": "33" } })
    await expect(readLimitedBlob(response, new AbortController().signal, 32)).rejects.toMatchObject({
      code: "RESPONSE_TOO_LARGE",
    })
    expect(response.bodyUsed).toBe(false)
  })

  it("下载实际多段正文超限时取消流", async () => {
    const cancel = vi.fn()
    const response = new Response(
      new ReadableStream({
        start(controller) {
          controller.enqueue(new Uint8Array(16))
          controller.enqueue(new Uint8Array(17))
        },
        cancel,
      }),
    )
    await expect(readLimitedBlob(response, new AbortController().signal, 32)).rejects.toMatchObject({
      code: "RESPONSE_TOO_LARGE",
    })
    expect(cancel).toHaveBeenCalledOnce()
  })

  it("下载正文停滞仍受截止线约束", async () => {
    vi.useFakeTimers()
    const deadline = createDeadline(10)
    try {
      const pending = readLimitedBlob(hangingBodyResponse("csv"), deadline.signal, 32)
      const assertion = expect(pending).rejects.toMatchObject({ name: "TimeoutError" })
      await vi.advanceTimersByTimeAsync(10)
      await assertion
    } finally {
      deadline.cleanup()
    }
  })
})
