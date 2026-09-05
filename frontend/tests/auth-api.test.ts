import { afterEach, describe, expect, it, vi } from "vitest"

import { loginRequest, refreshRequest } from "../src/api/auth"
import { beginRefreshTabBinding } from "../src/api/sessionTokens"

const USER = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "operator01",
  display_name: "测试用户",
  dept: "研发部",
  role: "operator",
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("认证请求边界", () => {
  it("refresh 只发送 tab_id 并显式使用同源 Cookie", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          token: "access.jwt",
          expires_in: 900,
          refresh_expires_in: 604800,
          user: USER,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    const tabId = beginRefreshTabBinding()

    await refreshRequest()

    const [, init] = fetchMock.mock.calls[0]
    expect(init.credentials).toBe("same-origin")
    expect(JSON.parse(String(init.body))).toEqual({ tab_id: tabId })
    expect(String(init.body)).not.toContain("refresh_token")
  })

  it("调用方 signal 不能绕过 55 秒登录截止线", async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    let requestSignal: AbortSignal | undefined
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        requestSignal = init?.signal ?? undefined
        return new Promise<Response>((_resolve, reject) => {
          requestSignal?.addEventListener("abort", () => reject(requestSignal?.reason), {
            once: true,
          })
        })
      }),
    )

    const request = loginRequest("ad", "user01", "password", caller.signal)
    const rejected = expect(request).rejects.toMatchObject({ name: "TimeoutError" })
    await vi.advanceTimersByTimeAsync(54_999)
    expect(requestSignal?.aborted).toBe(false)
    await vi.advanceTimersByTimeAsync(1)
    await rejected
    expect(requestSignal?.aborted).toBe(true)
    expect(caller.signal.aborted).toBe(false)
  })

  it("调用方取消会传递到实际请求", async () => {
    vi.useFakeTimers()
    const caller = new AbortController()
    let requestSignal: AbortSignal | undefined
    vi.stubGlobal(
      "fetch",
      vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
        requestSignal = init?.signal ?? undefined
        return new Promise<Response>((_resolve, reject) => {
          requestSignal?.addEventListener("abort", () => reject(requestSignal?.reason), {
            once: true,
          })
        })
      }),
    )

    const request = loginRequest("ad", "user01", "password", caller.signal)
    const rejected = expect(request).rejects.toMatchObject({ name: "AbortError" })
    caller.abort(new DOMException("页面已切换", "AbortError"))
    await rejected

    expect(requestSignal?.aborted).toBe(true)
  })

  it("登录响应头已到但正文停滞时仍于 55 秒截止", async () => {
    vi.useFakeTimers()
    beginRefreshTabBinding()
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(new TextEncoder().encode('{"token":'))
              },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        ),
      ),
    )

    const request = loginRequest("ad", "user01", "password")
    const rejected = expect(request).rejects.toMatchObject({ name: "TimeoutError" })
    await vi.advanceTimersByTimeAsync(54_999)
    await vi.advanceTimersByTimeAsync(1)
    await rejected
  })

  it("Refresh 正文停滞时于 10 秒内退出", async () => {
    vi.useFakeTimers()
    beginRefreshTabBinding()
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(
            new ReadableStream({
              start(controller) {
                controller.enqueue(new TextEncoder().encode('{"token":'))
              },
            }),
            { status: 200, headers: { "Content-Type": "application/json" } },
          ),
        ),
      ),
    )

    const request = refreshRequest()
    const rejected = expect(request).rejects.toMatchObject({ name: "TimeoutError" })
    await vi.advanceTimersByTimeAsync(10_000)
    await rejected
  })

  it("认证 JSON 超过正文上限时受控失败且不回显正文", async () => {
    beginRefreshTabBinding()
    const oversized = `{${"\"k\":".padEnd(33 * 1024, "1")}}`
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          new Response(oversized, {
            status: 200,
            headers: { "Content-Type": "application/json" },
          }),
        ),
      ),
    )

    await expect(refreshRequest()).rejects.toMatchObject({
      name: "AuthApiError",
      code: "RESPONSE_TOO_LARGE",
      message: "响应正文超过允许大小",
    })
  })
})
