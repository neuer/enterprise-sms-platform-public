import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  activateVendorTest,
  addVendorTestRecipient,
  createVendorSealSession,
  getVendorTestOperation,
  getVendorTestStatus,
  installVendorCredentials,
  listVendorTestRecipients,
  pauseVendorTest,
  resetVendorTest,
  resumeVendorTest,
  sendVendorTestUat,
  type VendorStepUpOperation,
  type VendorTestOperation,
} from "../src/api/admin"

function response(body: unknown, cacheControl = "no-store") {
  return {
    ok: true,
    status: 200,
    headers: {
      get: (name: string) => (name.toLowerCase() === "cache-control" ? cacheControl : null),
    },
    json: async () => body,
  }
}

describe("真实联调前端 API", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    vi.unstubAllGlobals()
  })

  it("使用固定页面路由和严格字段，正式 Key 不进入请求或存储", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    const fetch = vi.fn().mockResolvedValue(response({ operation_id: "op-1" }))
    vi.stubGlobal("fetch", fetch)
    const envelope = {
      session_id: "seal-1",
      wrapped_key: "wrapped",
      nonce: "nonce",
      ciphertext: "ciphertext",
      aad: "aad",
      algorithm: "RSA-OAEP-256+A256GCM" as const,
    }

    await getVendorTestStatus()
    await createVendorSealSession("install_credentials")
    await installVendorCredentials("install_credentials", "step-token", envelope)
    await listVendorTestRecipients()
    await addVendorTestRecipient("值班机", "13900000001")
    await activateVendorTest("activate-token")
    await pauseVendorTest()
    await resumeVendorTest("critical-token")
    await getVendorTestOperation("op-1")
    await sendVendorTestUat({
      recipient_id: 9,
      app_id: 7,
      biz_id: "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
      category: "notice",
      content: "维护通知",
      consent_confirmed: false,
    })

    const calls = fetch.mock.calls
    expect(calls[0][0]).toBe("/api/v1/web/admin/vendor-test/status")
    expect(calls[1][0]).toBe("/api/v1/web/admin/vendor-test/seal-sessions")
    expect(JSON.parse(String(calls[1][1].body))).toEqual({ operation: "install_credentials" })
    expect(calls[2][0]).toBe("/api/v1/web/admin/vendor-test/credentials")
    expect(JSON.parse(String(calls[2][1].body))).toEqual({
      operation: "install_credentials",
      step_up_token: "step-token",
      ...envelope,
    })
    expect(calls[8][0]).toBe("/api/v1/web/admin/vendor-test/operations/op-1")
    expect(calls[9][0]).toBe("/api/v1/web/admin/vendor-test/messages")
    expect(JSON.stringify(calls)).not.toContain("formal-secret-key")
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.getItem("formal-secret-key")).toBeNull()
  })

  it("拒绝缺少 no-store 的控制响应", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({}, "public, max-age=60")))

    await expect(getVendorTestStatus()).rejects.toThrow("缓存策略")
  })

  it("清空联调设置只提交单用途令牌且类型契约包含 reset_configuration", async () => {
    const stepUpOperation: VendorStepUpOperation = "reset_configuration"
    const operationType: VendorTestOperation["operation_type"] = "reset_configuration"
    const fetch = vi.fn().mockResolvedValue(response({
      operation_id: "00000000-0000-4000-8000-000000000066",
      operation_type: operationType,
      status: "requested",
    }))
    vi.stubGlobal("fetch", fetch)

    await resetVendorTest("reset-step-token")

    expect(stepUpOperation).toBe("reset_configuration")
    expect(fetch).toHaveBeenCalledOnce()
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web/admin/vendor-test/reset")
    expect(fetch.mock.calls[0][1].method).toBe("POST")
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toEqual({
      step_up_token: "reset-step-token",
    })
    expect(String(fetch.mock.calls[0][1].body)).not.toContain("password")
    expect(String(fetch.mock.calls[0][1].body)).not.toContain("清空联调设置")
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })
})
