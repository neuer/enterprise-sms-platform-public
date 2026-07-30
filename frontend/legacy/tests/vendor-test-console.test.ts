import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { afterEach, vi } from "vitest"

import type { VendorTestStatus } from "../src/api/admin"
import VendorTestConsole from "../src/components/VendorTestConsole.vue"

const baseStatus: VendorTestStatus = {
  mode: "inactive",
  heartbeat_at: "2026-07-17T09:30:00+08:00",
  credential_configured: true,
  active_recipient_count: 1,
  pause_kind: null,
  daily_limit: 100,
}

const recipients = [
  {
    id: 9,
    label: "值班机",
    phone_mask: "139****0001",
    status: "active",
    created_at: "2026-07-17T09:00:00+08:00",
    disabled_at: null,
  },
]

const apps = [
  {
    id: 7,
    name: "业务通知",
    dept: "平台部",
    allowed_categories: ["verify", "notice", "market"],
    default_sign: "平台",
    daily_quota: 1000,
    rate_limit_per_min: 10,
    blacklist_check: true,
    freq_override: null,
    callback_url: null,
    callback_report_enabled: false,
    status: 1,
  },
]

const templates = [
  {
    id: 31,
    name: "维护通知模板",
    content: "您好，{1} 将进行维护",
    var_specs: [{ pos: 1, max_len: 12 }],
    dept: "平台部",
    vendor_template_id: "TPL-31",
    vendor_state: "approved",
    vendor_reject_reason: null,
  },
  {
    id: 32,
    name: "未审核模板",
    content: "不可发送 {1}",
    var_specs: [{ pos: 1, max_len: 8 }],
    dept: "平台部",
    vendor_template_id: null,
    vendor_state: "pending",
    vendor_reject_reason: null,
  },
]

const signs = [
  {
    id: 41,
    name: "平台",
    vendor_sign_id: "SIGN-41",
    vendor_state: "approved",
    vendor_reject_reason: null,
  },
  {
    id: 42,
    name: "未审核签名",
    vendor_sign_id: null,
    vendor_state: "pending",
    vendor_reject_reason: null,
  },
]

const statusCases: Array<
  [VendorTestStatus["mode"], VendorTestStatus["pause_kind"], string]
> = [
  ["setup_required", null, "待完成设置"],
  ["inactive", null, "待激活"],
  ["controlled", null, "受控联调中"],
  ["blocked", "critical", "安全阻断"],
  ["blocked", "daily", "日预算已封顶"],
]

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => (name.toLowerCase() === "cache-control" ? "no-store" : null),
    },
    json: async () => body,
  }
}

function consoleFetch(
  current = baseStatus,
  override?: (url: string, init: RequestInit) => ReturnType<typeof response> | undefined,
) {
  return vi.fn().mockImplementation((url: string, init: RequestInit = {}) => {
    const overridden = override?.(url, init)
    if (overridden) return Promise.resolve(overridden)
    if (url.endsWith("/vendor-test/status")) return Promise.resolve(response(current))
    if (url.endsWith("/vendor-test/recipients")) return Promise.resolve(response(recipients))
    if (url.endsWith("/admin/apps")) return Promise.resolve(response(apps))
    if (url.endsWith("/templates")) return Promise.resolve(response(templates))
    if (url.endsWith("/signs")) return Promise.resolve(response(signs))
    return Promise.resolve(response({}))
  })
}

function mountConsole() {
  return mount(VendorTestConsole, {
    attachTo: document.body,
    global: { plugins: [createPinia(), ElementPlus] },
  })
}

afterEach(() => {
  document.body.innerHTML = ""
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  localStorage.clear()
  sessionStorage.clear()
})

describe("系统配置页真实联调控制台", () => {
  it.each(statusCases)("渲染 %s / %s 安全状态", async (mode, pauseKind, expected) => {
    vi.stubGlobal("fetch", consoleFetch({ ...baseStatus, mode, pause_kind: pauseKind }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(wrapper.text()).toContain(expected)
    expect(wrapper.text()).toContain("100 条/日")
    expect(wrapper.text()).not.toContain("secretName")
    wrapper.unmount()
  })

  it("凭据弹窗只显示布尔就绪态，关闭后移除并清空局部密码字段", async () => {
    vi.stubGlobal("fetch", consoleFetch())
    const wrapper = mountConsole()
    await flushPromises()

    expect(wrapper.text()).toContain("正式凭据已安装")
    await wrapper.get("[data-testid='vendor-credentials']").trigger("click")
    await flushPromises()

    const nameInput = document.querySelector("[data-testid='vendor-secret-name']") as HTMLInputElement
    const keyInput = document.querySelector("[data-testid='vendor-secret-key']") as HTMLInputElement
    expect(nameInput.autocomplete).toBe("new-password")
    expect(keyInput.autocomplete).toBe("new-password")
    expect(nameInput.getAttribute("spellcheck")).toBe("false")
    expect(keyInput.getAttribute("spellcheck")).toBe("false")
    nameInput.value = "sentinel-name"
    nameInput.dispatchEvent(new Event("input", { bubbles: true }))
    keyInput.value = "sentinel-key"
    keyInput.dispatchEvent(new Event("input", { bubbles: true }))

    const keep = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("保留现状"),
    ) as HTMLButtonElement
    keep.click()
    await flushPromises()

    expect(document.querySelector("[data-testid='vendor-secret-name']")).toBeNull()
    expect(document.querySelector("[data-testid='vendor-secret-key']")).toBeNull()
    expect(JSON.stringify(localStorage)).not.toContain("sentinel")
    expect(JSON.stringify(sessionStorage)).not.toContain("sentinel")
    wrapper.unmount()
  })

  it("只展示登记号码掩码，并在受控态开放单收件人 UAT 与后端计费预览", async () => {
    vi.useFakeTimers()
    let uatPolls = 0
    const uatOperation = {
      operation_id: "00000000-0000-4000-8000-000000000099",
      operation_type: "uat_send",
      status: "running",
      safe_code: null,
      vendor_code: null,
      batch_no: "UAT-001",
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(
      { ...baseStatus, mode: "controlled" },
      (url, init) => {
        if (url.endsWith("/vendor-test/messages/preview")) {
          return response({
            final_length: 18,
            est_segments: 1,
            quota_cost: 1,
            segment_parts: [{ used: 18, capacity: 70, partial: false }],
            next_segment_at: 71,
            approval_required: false,
            unsubscribe_appended: false,
            deferred_reason: null,
          })
        }
        if (url.endsWith("/vendor-test/messages") && init.method === "POST") {
          return response(uatOperation)
        }
        if (url.endsWith(`/vendor-test/messages/${uatOperation.operation_id}`)) {
          uatPolls += 1
          return response({
            ...uatOperation,
            status: "succeeded",
            completed_at: "2026-07-17T09:31:01+08:00",
          })
        }
        return undefined
      },
    )
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mountConsole()
    await flushPromises()

    expect(wrapper.text()).toContain("139****0001")
    expect(wrapper.text()).not.toContain("13900000001")
    expect(wrapper.get("[data-testid='vendor-credentials']").attributes("disabled")).toBeUndefined()
    await wrapper.getComponent("[data-testid='uat-recipient']").setValue(9)
    await wrapper.getComponent("[data-testid='uat-app']").setValue(7)
    await wrapper.get("[data-testid='uat-content']").setValue("维护通知")
    await wrapper.get("[data-testid='uat-preview']").trigger("click")
    await flushPromises()

    expect(wrapper.text()).toContain("预计 1 条")
    await wrapper.get("[data-testid='uat-send']").trigger("click")
    await flushPromises()
    expect(confirm).toHaveBeenCalledWith(
      expect.stringContaining("本次预计消耗 1 条计费额度（1 个计费段）"),
      "确认发送真实 UAT",
      expect.objectContaining({ confirmButtonText: "确认发送（预计 1 条）" }),
    )
    expect(String(confirm.mock.calls[0]?.[0])).toContain("每日总上限为 100 条")
    expect(String(confirm.mock.calls[0]?.[0])).not.toContain("消耗每日 100 条预算")
    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()

    const send = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/messages"))
    expect(JSON.parse(String(send?.[1].body))).toEqual({
      recipient_id: 9,
      app_id: 7,
      category: "notice",
      content: "维护通知",
      consent_confirmed: false,
      remark: "系统配置页真实 UAT",
    })
    expect(JSON.stringify(send)).not.toContain("13900000001")
    expect(wrapper.text()).toContain("UAT-001")
    expect(uatPolls).toBe(1)
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).toBeNull()
    wrapper.unmount()
  })

  it("可在页面重录号码刷新跨版本 HMAC 索引且关闭后清空明文", async () => {
    const fetch = consoleFetch(baseStatus, (url, init) => {
      if (url.endsWith("/vendor-test/recipients/9/refresh-index") && init.method === "POST") {
        return response(recipients[0])
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-refresh-recipient-9']").trigger("click")
    await flushPromises()
    const phone = document.querySelector(
      "[data-testid='vendor-refresh-phone']",
    ) as HTMLInputElement
    expect(phone.autocomplete).toBe("off")
    phone.value = "13900000001"
    phone.dispatchEvent(new Event("input", { bubbles: true }))
    const submit = document.querySelector(
      "[data-testid='vendor-refresh-submit']",
    ) as HTMLButtonElement
    submit.click()
    await flushPromises()

    const call = fetch.mock.calls.find(([url]) =>
      url.endsWith("/vendor-test/recipients/9/refresh-index"),
    )
    expect(JSON.parse(String(call?.[1].body))).toEqual({ phone: "13900000001" })
    expect(document.querySelector("[data-testid='vendor-refresh-phone']")).toBeNull()
    expect(JSON.stringify(localStorage)).not.toContain("13900000001")
    expect(JSON.stringify(sessionStorage)).not.toContain("13900000001")
    wrapper.unmount()
  })

  it("可在页面选择已审核模板、参数和签名完成真实 UAT", async () => {
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000098",
      operation_type: "uat_send",
      status: "succeeded",
      safe_code: null,
      vendor_code: null,
      batch_no: "UAT-TEMPLATE-001",
      checkpoint_id: null,
      requested_at: "2026-07-17T09:32:00+08:00",
      completed_at: "2026-07-17T09:32:01+08:00",
    }
    const fetch = consoleFetch(
      { ...baseStatus, mode: "controlled" },
      (url, init) => {
        if (url.endsWith("/vendor-test/messages/preview")) {
          return response({
            final_length: 25,
            est_segments: 1,
            quota_cost: 1,
            segment_parts: [{ used: 25, capacity: 70, partial: false }],
            next_segment_at: 71,
            approval_required: false,
            unsubscribe_appended: false,
            deferred_reason: null,
          })
        }
        if (url.endsWith("/vendor-test/messages") && init.method === "POST") {
          return response(operation)
        }
        return undefined
      },
    )
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.getComponent("[data-testid='uat-recipient']").setValue(9)
    await wrapper.getComponent("[data-testid='uat-app']").setValue(7)
    await wrapper.getComponent("[data-testid='uat-content-mode']").setValue("template")
    await wrapper.getComponent("[data-testid='uat-template']").setValue(31)
    await flushPromises()
    await wrapper.get("[data-testid='uat-template-param-1']").setValue("今日 22:00")
    await wrapper.getComponent("[data-testid='uat-sign']").setValue("平台")
    await wrapper.get("[data-testid='uat-preview']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='uat-send']").trigger("click")
    await flushPromises()

    const previewCall = fetch.mock.calls.find(([url]) =>
      url.endsWith("/vendor-test/messages/preview"),
    )
    expect(JSON.parse(String(previewCall?.[1].body))).toEqual({
      app_id: 7,
      category: "notice",
      template_id: 31,
      template_params: ["今日 22:00"],
      sign_name: "平台",
      consent_confirmed: false,
    })
    const sendCall = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/messages"))
    expect(JSON.parse(String(sendCall?.[1].body))).toEqual({
      recipient_id: 9,
      app_id: 7,
      category: "notice",
      template_id: 31,
      template_params: ["今日 22:00"],
      sign_name: "平台",
      consent_confirmed: false,
      remark: "系统配置页真实 UAT",
    })
    expect(JSON.stringify(sendCall)).not.toContain("13900000001")
    expect(wrapper.text()).toContain("UAT-TEMPLATE-001")
    expect(wrapper.text()).not.toContain("未审核模板")
    expect(wrapper.text()).not.toContain("未审核签名")
    wrapper.unmount()
  })

  it("二次认证激活并仅按 operation_id 轮询到终态", async () => {
    vi.useFakeTimers()
    let polls = 0
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000088",
      operation_type: "activate",
      status: "requested",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) return response({ token: "single-use", expires_in: 300 })
      if (url.endsWith("/vendor-test/activate")) return response(operation)
      if (url.includes("/vendor-test/operations/")) {
        polls += 1
        return response({
          ...operation,
          status: polls > 1 ? "succeeded" : "running",
          completed_at: polls > 1 ? "2026-07-17T09:31:02+08:00" : null,
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-activate']").trigger("click")
    await flushPromises()
    const password = document.querySelector("[data-testid='vendor-step-up-password']") as HTMLInputElement
    password.value = "current-password"
    password.dispatchEvent(new Event("input", { bubbles: true }))
    const activate = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("验证并激活"),
    ) as HTMLButtonElement
    activate.click()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2_000)
    await flushPromises()

    expect(polls).toBe(2)
    expect(fetch.mock.calls.filter(([url]) => url.includes("/vendor-test/operations/")).every(
      ([url]) => url.endsWith(operation.operation_id),
    )).toBe(true)
    expect(document.querySelector("[data-testid='vendor-step-up-password']")).toBeNull()
    expect(sessionStorage.getItem("current-password")).toBeNull()
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).toBeNull()
    wrapper.unmount()
  })

  it("刷新后只按已保存的安全 operation_id 恢复受控操作", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000077",
      operation_type: "activate",
      status: "requested",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) return response({ token: "single-use", expires_in: 300 })
      if (url.endsWith("/vendor-test/activate")) return response(operation)
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        return response({ ...operation, status: "running" })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)

    const beforeRefresh = mountConsole()
    await flushPromises()
    await beforeRefresh.get("[data-testid='vendor-activate']").trigger("click")
    await flushPromises()
    const password = document.querySelector("[data-testid='vendor-step-up-password']") as HTMLInputElement
    password.value = "current-password"
    password.dispatchEvent(new Event("input", { bubbles: true }))
    const activate = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("验证并激活"),
    ) as HTMLButtonElement
    activate.click()
    await flushPromises()

    beforeRefresh.unmount()
    document.body.innerHTML = ""
    const afterRefresh = mountConsole()
    await flushPromises()

    expect(afterRefresh.text()).toContain(operation.operation_id)
    expect(afterRefresh.text()).toContain("running")
    expect(fetch.mock.calls.some(([url]) =>
      url.endsWith(`/vendor-test/operations/${operation.operation_id}`),
    )).toBe(true)
    const stored = JSON.stringify(sessionStorage)
    expect(stored).toContain(operation.operation_id)
    expect(stored).not.toContain("current-password")
    expect(stored).not.toContain("single-use")
    expect(localStorage.length).toBe(0)
    afterRefresh.unmount()
  })
})
