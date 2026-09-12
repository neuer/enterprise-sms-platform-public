import { flushPromises } from "@vue/test-utils"
import { afterEach, expect, it, vi } from "vitest"
import { apiRequest } from "../src/api/client"
import { REFRESH_TAB_ID_KEY, setAccessSession } from "../src/api/sessionTokens"
const admin = {
  account_id: 8,
  identity_id: 18,
  provider_code: "local",
  username: "admin",
  display_name: "合成",
  dept: "",
  role: "admin" as const,
}
afterEach(() => {
  vi.unstubAllGlobals()
  sessionStorage.clear()
})
it.each([false, true])("取消 401 等锁请求；仍有其他等待者=%s", async (shared) => {
  sessionStorage.setItem(REFRESH_TAB_ID_KEY, "b".repeat(32))
  setAccessSession("expired", admin)
  let queueSignal!: AbortSignal
  let grant!: () => Promise<void>
  vi.stubGlobal("navigator", {
    locks: {
      request: (_name: string, options: { signal: AbortSignal }, run: () => Promise<unknown>) => {
        queueSignal = options.signal
        return new Promise((resolve, reject) => {
          const cancel = () => reject(queueSignal.reason)
          queueSignal.addEventListener("abort", cancel, { once: true })
          grant = async () => {
            if (queueSignal.aborted) return
            queueSignal.removeEventListener("abort", cancel)
            try {
              resolve(await run())
            } catch (error) {
              reject(error)
            }
          }
        })
      },
    },
  })
  let refreshed = false
  const fetch = vi.fn(async (url: string) => {
    if (url === "/api/v1/web/auth/refresh") {
      refreshed = true
      return new Response(
        JSON.stringify({
          token: "fresh",
          session_mode: "refresh",
          expires_in: 900,
          refresh_expires_in: 604800,
          user: admin,
        }),
      )
    }
    return new Response(JSON.stringify(refreshed ? { ok: true } : { code: "UNAUTHORIZED" }), {
      status: refreshed ? 200 : 401,
    })
  })
  vi.stubGlobal("fetch", fetch)
  const controller = new AbortController()
  const first = apiRequest("/api/v1/web/synthetic", { signal: controller.signal })
  const rejected = expect(first).rejects.toMatchObject({ name: "AbortError" })
  await flushPromises()
  const second = shared ? apiRequest("/api/v1/web/synthetic", {}) : undefined
  await flushPromises()
  controller.abort()
  await rejected
  expect(queueSignal.aborted).toBe(!shared)
  await grant()
  if (second) await expect(second).resolves.toEqual({ ok: true })
  expect(fetch.mock.calls.filter(([url]) => url === "/api/v1/web/auth/refresh")).toHaveLength(shared ? 1 : 0)
})
