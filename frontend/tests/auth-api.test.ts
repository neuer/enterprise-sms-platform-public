import { afterEach, describe, expect, it, vi } from "vitest"

import { loginRequest, refreshRequest } from "../src/api/auth"
import { beginRefreshTabBinding, getRefreshTabBinding, setAccessSession } from "../src/api/sessionTokens"

const USER = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "operator01",
  display_name: "测试用户",
  dept: "研发部",
  role: "operator" as const,
}

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe("认证请求边界", () => {
  it("仅对明确准入拒绝有界重试，保留同一请求和 tab binding", async () => {
    vi.useFakeTimers()
    vi.stubGlobal("navigator", { locks: { request: vi.fn() } })
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            code: "RATE_LIMITED",
            message: "busy",
            detail: { auth_admission_retry: true, retry_after_seconds: 1 },
          }),
          { status: 429 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            session_mode: "refresh",
            token: "access.jwt",
            expires_in: 900,
            user: USER,
          }),
          { status: 200 },
        ),
      )
    vi.stubGlobal("fetch", fetchMock)
    const result = loginRequest("local", "operator01", "password")
    await vi.advanceTimersByTimeAsync(1300)
    await expect(result).resolves.toMatchObject({ token: "access.jwt" })
    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect(fetchMock.mock.calls[0][1].body).toEqual(fetchMock.mock.calls[1][1].body)
    expect(JSON.parse(fetchMock.mock.calls[0][1].body).tab_id).toEqual(getRefreshTabBinding())
  })

  it.each([401, 423, 429, 503])("不会自动重试普通 %i 认证错误", async (status) => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: status === 429 ? "RATE_LIMITED" : "UNAUTHORIZED",
          message: "denied",
        }),
        { status },
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    await expect(loginRequest("local", "operator01", "wrong")).rejects.toMatchObject({ status })
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("页面取消终止准入等待，不发送迟到登录", async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          code: "RATE_LIMITED",
          message: "busy",
          detail: { auth_admission_retry: true, retry_after_seconds: 1 },
        }),
        { status: 429 },
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    const controller = new AbortController()
    const result = loginRequest("local", "operator01", "password", controller.signal)
    const rejected = expect(result).rejects.toMatchObject({ name: "AbortError" })
    await vi.advanceTimersByTimeAsync(100)
    controller.abort(new DOMException("cancelled", "AbortError"))
    await vi.advanceTimersByTimeAsync(2000)
    await rejected
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it("持续准入拒绝在三十秒等待预算内停止", async () => {
    vi.useFakeTimers()
    const fetchMock = vi.fn().mockImplementation(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            code: "RATE_LIMITED",
            message: "busy",
            detail: { auth_admission_retry: true, retry_after_seconds: 1 },
          }),
          { status: 429 },
        ),
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    const result = loginRequest("local", "operator01", "password")
    const rejected = expect(result).rejects.toMatchObject({ status: 429 })
    await vi.advanceTimersByTimeAsync(30000)
    await rejected
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(30)
  })

  it("无 Web Locks 登录请求 access_only 且不建立 tab binding", async () => {
    vi.stubGlobal("navigator", {})
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          session_mode: "access_only",
          token: "access.jwt",
          expires_in: 900,
          user: USER,
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    )
    vi.stubGlobal("fetch", fetchMock)

    await loginRequest("local", "operator01", "password")

    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({
      provider_code: "local",
      username: "operator01",
      password: "password",
      session_mode: "access_only",
    })
    expect(String(fetchMock.mock.calls[0][1].body)).not.toContain("tab_id")
    expect(getRefreshTabBinding()).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_tab_id")).toBeNull()
  })

  it("Access-Only 在客户端阻断 refresh", async () => {
    vi.stubGlobal("navigator", {})
    const fetchMock = vi.fn()
    vi.stubGlobal("fetch", fetchMock)
    setAccessSession("access.jwt", USER, "access_only")

    await expect(refreshRequest()).rejects.toMatchObject({
      name: "AuthApiError",
      status: 401,
      code: "UNAUTHORIZED",
    })
    expect(fetchMock).not.toHaveBeenCalled()
  })

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
    const oversized = `{${'"k":'.padEnd(33 * 1024, "1")}}`
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
