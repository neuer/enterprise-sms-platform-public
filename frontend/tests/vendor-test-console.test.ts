import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElDialog, ElMessage, ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { afterEach, beforeEach, vi } from "vitest"

import type { VendorTestStatus } from "../src/api/admin"
import VendorTestConsole from "../src/components/VendorTestConsole.vue"
import vendorTestConsoleSource from "../src/components/VendorTestConsole.vue?raw"

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
    clone: () => response(body, status),
  }
}

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

function consoleFetch(
  current = baseStatus,
  override?: (
    url: string,
    init: RequestInit,
  ) => ReturnType<typeof response> | Promise<ReturnType<typeof response>> | undefined,
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

beforeEach(() => {
  vi.stubGlobal("isSecureContext", true)
})

function fillResetDialog(passwordValue: string, confirmationValue: string): void {
  const password = document.querySelector(
    "[data-testid='vendor-reset-password']",
  ) as HTMLInputElement
  const confirmation = document.querySelector(
    "[data-testid='vendor-reset-confirmation']",
  ) as HTMLInputElement
  password.value = passwordValue
  password.dispatchEvent(new Event("input", { bubbles: true }))
  confirmation.value = confirmationValue
  confirmation.dispatchEvent(new Event("input", { bubbles: true }))
}

function clickResetSubmit(): void {
  const submit = document.querySelector("[data-testid='vendor-reset-submit']") as HTMLButtonElement
  submit.click()
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

  it("HTTP 非安全上下文隐藏正式凭据字段且不创建任何凭据请求", async () => {
    vi.stubGlobal("isSecureContext", false)
    const fetch = consoleFetch()
    vi.stubGlobal("fetch", fetch)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-credentials']").trigger("click")
    await flushPromises()

    expect(document.querySelector("[data-testid='vendor-credential-password']")).toBeNull()
    expect(document.querySelector("[data-testid='vendor-secret-name']")).toBeNull()
    expect(document.querySelector("[data-testid='vendor-secret-key']")).toBeNull()
    expect(document.body.textContent).toContain("当前入口不支持正式凭据安全加密。")
    expect(document.body.textContent).toContain(
      "请在 ChatGPT 中发送“打开正式凭据安全入口”",
    )
    expect(document.body.textContent).toContain("然后通过临时 HTTPS 地址重新登录。")

    const submit = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("密封并"),
    ) as HTMLButtonElement
    expect(submit.disabled).toBe(true)
    submit.click()
    await flushPromises()

    const sensitiveRequests = fetch.mock.calls.filter(([url]) =>
      ["/step-up", "/seal-sessions", "/credentials"].some((suffix) =>
        String(url).endsWith(suffix),
      ),
    )
    expect(sensitiveRequests).toEqual([])
    wrapper.unmount()
  })

  it("凭据弹窗同步双击只发起一次二次认证", async () => {
    const stepUp = deferred<ReturnType<typeof response>>()
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) return stepUp.promise
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-credentials']").trigger("click")
    await flushPromises()
    for (const [selector, value] of [
      ["[data-testid='vendor-credential-password']", "current-password"],
      ["[data-testid='vendor-secret-name']", "formal-name"],
      ["[data-testid='vendor-secret-key']", "formal-key"],
    ]) {
      const input = document.querySelector(selector) as HTMLInputElement
      input.value = value
      input.dispatchEvent(new Event("input", { bubbles: true }))
    }
    const submit = [...document.querySelectorAll("button")].find((button) =>
      button.textContent?.includes("密封并轮换"),
    ) as HTMLButtonElement

    submit.click()
    submit.click()

    expect(fetch.mock.calls.filter(([url]) => url.endsWith("/vendor-test/step-up"))).toHaveLength(1)

    stepUp.resolve(response({ message: "二次认证失败" }, 401))
    await flushPromises()
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

  it("刷新恢复 pending reset 时在 operation GET 返回前同步禁用危险动作", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000076",
      operation_type: "reset_configuration",
      status: "requested",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const initialLookup = deferred<ReturnType<typeof response>>()
    let operationLookups = 0
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (!url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) return undefined
      operationLookups += 1
      if (operationLookups === 1) return initialLookup.promise
      return response({
        ...operation,
        status: "failed",
        safe_code: "RESET_INCOMPLETE",
        completed_at: "2026-07-17T09:31:01+08:00",
      })
    }))
    const wrapper = mountConsole()
    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    await flushPromises()

    expect(wrapper.text()).toContain("待激活")
    const restoringReset = wrapper.get("[data-testid='vendor-reset']")
    expect(restoringReset.attributes("disabled")).toBeDefined()
    expect(wrapper.get("[data-testid='vendor-credentials']").attributes("disabled")).toBeDefined()
    expect(wrapper.get("[data-testid='vendor-activate']").attributes("disabled")).toBeDefined()
    expect(wrapper.get("[data-testid='vendor-add-recipient']").attributes("disabled")).toBeDefined()
    expect(
      wrapper.get("[data-testid='vendor-refresh-recipient-9']").attributes("disabled"),
    ).toBeDefined()
    await restoringReset.trigger("click")
    expect(document.querySelector("[data-testid='vendor-reset-password']")).toBeNull()

    initialLookup.resolve(response(operation))
    await flushPromises()
    expect(wrapper.text()).toContain(operation.operation_id)
    expect(wrapper.get("[data-testid='vendor-reset']").attributes("disabled")).toBeDefined()

    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()
    expect(wrapper.get("[data-testid='vendor-reset']").attributes("disabled")).toBeUndefined()
    expect(wrapper.text()).toContain("部分设置可能已清理")
    wrapper.unmount()
  })

  it("刷新恢复 operation 短暂失败时保留引用并持续禁用直到自动重试成功", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000078",
      operation_type: "reset_configuration",
      status: "running",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    let operationLookups = 0
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (!url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) return undefined
      operationLookups += 1
      if (operationLookups === 1) return Promise.reject(new Error("temporary network failure"))
      return response(operation)
    }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(operationLookups).toBe(1)
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).not.toBeNull()
    expect(wrapper.get("[data-testid='vendor-reset']").attributes("disabled")).toBeDefined()

    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()

    expect(operationLookups).toBe(2)
    expect(wrapper.text()).toContain(operation.operation_id)
    expect(wrapper.text()).not.toContain("temporary network failure")
    expect(wrapper.get("[data-testid='vendor-reset']").attributes("disabled")).toBeDefined()
    wrapper.unmount()
  })

  it("刷新恢复 succeeded reset 后在完成态刷新返回前继续禁用清空动作", async () => {
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000077",
      operation_type: "reset_configuration",
      status: "succeeded",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: "2026-07-17T09:31:01+08:00",
    }
    const completionStatus = deferred<ReturnType<typeof response>>()
    let statusLookups = 0
    let recipientLookups = 0
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        return response(operation)
      }
      if (url.endsWith("/vendor-test/status")) {
        statusLookups += 1
        return statusLookups === 1 ? response(baseStatus) : completionStatus.promise
      }
      if (url.endsWith("/vendor-test/recipients")) {
        recipientLookups += 1
        return response(recipientLookups === 1 ? recipients : [])
      }
      return undefined
    }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(statusLookups).toBe(2)
    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='vendor-credentials']").attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='vendor-credentials']").trigger("click")
    expect(document.querySelector("[data-testid='vendor-reset-password']")).toBeNull()

    completionStatus.resolve(response({
      ...baseStatus,
      mode: "setup_required",
      credential_configured: false,
      active_recipient_count: 0,
    }))
    await flushPromises()

    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.text()).toContain("待完成设置")
    wrapper.unmount()
  })

  it("终态刷新完成后忽略更早 load 的迟到旧投影", async () => {
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000079",
      operation_type: "reset_configuration",
      status: "succeeded",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: "2026-07-17T09:31:01+08:00",
    }
    const oldStatus = deferred<ReturnType<typeof response>>()
    let statusLookups = 0
    let recipientLookups = 0
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        return response(operation)
      }
      if (url.endsWith("/vendor-test/status")) {
        statusLookups += 1
        if (statusLookups === 1) return oldStatus.promise
        return response({
          ...baseStatus,
          mode: "setup_required",
          credential_configured: false,
          active_recipient_count: 0,
        })
      }
      if (url.endsWith("/vendor-test/recipients")) {
        recipientLookups += 1
        return response(recipientLookups === 1 ? recipients : [])
      }
      return undefined
    }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.text()).toContain("待完成设置")

    oldStatus.resolve(response(baseStatus))
    await flushPromises()

    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.text()).toContain("待完成设置")
    wrapper.unmount()
  })

  it("终态投影刷新失败时持续禁用并自动重试到完整刷新成功", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000080",
      operation_type: "reset_configuration",
      status: "succeeded",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: "2026-07-17T09:31:01+08:00",
    }
    let statusLookups = 0
    let recipientLookups = 0
    let appLookups = 0
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        return response(operation)
      }
      if (url.endsWith("/vendor-test/status")) {
        statusLookups += 1
        return response(statusLookups === 1
          ? baseStatus
          : {
              ...baseStatus,
              mode: "setup_required",
              credential_configured: false,
              active_recipient_count: 0,
            })
      }
      if (url.endsWith("/vendor-test/recipients")) {
        recipientLookups += 1
        return response(recipientLookups === 1 ? recipients : [])
      }
      if (url.endsWith("/admin/apps")) {
        appLookups += 1
        return appLookups === 2
          ? response({ code: "TEMPORARY", message: "temporary app failure" }, 503)
          : response(apps)
      }
      return undefined
    }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(appLookups).toBe(2)
    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='vendor-credentials']").attributes("disabled")).toBeDefined()
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).not.toBeNull()

    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()

    expect(appLookups).toBe(3)
    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(false)
    expect(wrapper.text()).toContain("待完成设置")
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).toBeNull()
    wrapper.unmount()
  })

  it.each([
    ["inactive 已配置", { ...baseStatus, mode: "inactive" as const }, recipients, true],
    [
      "setup_required 未配置且有旧号码投影时等待原操作恢复",
      {
        ...baseStatus,
        mode: "setup_required" as const,
        credential_configured: false,
        active_recipient_count: 0,
      },
      [{ ...recipients[0], status: "disabled" }],
      false,
    ],
    ["controlled", { ...baseStatus, mode: "controlled" as const }, recipients, false],
    [
      "inactive 但仍有暂停投影",
      { ...baseStatus, mode: "inactive" as const, pause_kind: "manual" as const },
      recipients,
      false,
    ],
    [
      "blocked",
      { ...baseStatus, mode: "blocked" as const, pause_kind: "critical" as const },
      recipients,
      false,
    ],
    [
      "setup_required 全新环境",
      {
        ...baseStatus,
        mode: "setup_required" as const,
        credential_configured: false,
        active_recipient_count: 0,
      },
      [],
      false,
    ],
    [
      "setup_required 恢复态但仍有暂停投影",
      {
        ...baseStatus,
        mode: "setup_required" as const,
        credential_configured: false,
        active_recipient_count: 0,
        pause_kind: "critical" as const,
      },
      [{ ...recipients[0], status: "disabled" }],
      false,
    ],
  ])("仅在安全可清空状态显示动作：%s", async (_scenario, currentStatus, projectedRecipients, visible) => {
    vi.stubGlobal("fetch", consoleFetch(currentStatus, (url) => {
      if (url.endsWith("/vendor-test/recipients")) return response(projectedRecipients)
      return undefined
    }))

    const wrapper = mountConsole()
    await flushPromises()

    expect(wrapper.find("[data-testid='vendor-reset']").exists()).toBe(visible)
    wrapper.unmount()
  })

  it("存在运行中操作时清空动作禁用且不能打开确认流程", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000055",
      operation_type: "activate",
      status: "running",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    sessionStorage.setItem("sms-platform:vendor-test:operation:v1", JSON.stringify({
      operation_id: operation.operation_id,
      operation_type: operation.operation_type,
    }))
    vi.stubGlobal("fetch", consoleFetch(baseStatus, (url) => {
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) return response(operation)
      return undefined
    }))

    const wrapper = mountConsole()
    await flushPromises()
    const reset = wrapper.get("[data-testid='vendor-reset']")

    expect(reset.attributes("disabled")).toBeDefined()
    await reset.trigger("click")
    await flushPromises()
    expect(document.querySelector("[data-testid='vendor-reset-password']")).toBeNull()
    wrapper.unmount()
  })

  it("精确确认后二次认证清空并轮询终态，成功后刷新为待完成设置且清除旧号码", async () => {
    vi.useFakeTimers()
    sessionStorage.setItem("sms_token", "admin.jwt")
    let resetCompleted = false
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000066",
      operation_type: "reset_configuration",
      status: "requested",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(baseStatus, (url, init) => {
      if (url.endsWith("/vendor-test/status") && resetCompleted) {
        return response({
          ...baseStatus,
          mode: "setup_required",
          credential_configured: false,
          active_recipient_count: 0,
        })
      }
      if (url.endsWith("/vendor-test/recipients") && resetCompleted) return response([])
      if (url.endsWith("/vendor-test/step-up")) {
        return response({ token: "single-use-reset-token", expires_in: 300 })
      }
      if (url.endsWith("/vendor-test/reset") && init.method === "POST") {
        return response(operation, 202)
      }
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        resetCompleted = true
        return response({
          ...operation,
          status: "succeeded",
          completed_at: "2026-07-17T09:31:01+08:00",
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    expect(document.body.textContent).toContain("仅删除正式厂商凭据的全部版本和全部测试号码")
    expect(document.body.textContent).toContain("保留管理员、短信业务数据、审计记录")
    expect(document.body.textContent).toContain("当日 UAT 用量、uncertain 占额")
    expect(document.body.textContent).toContain("数据库、Docker volume 和运行态目录")
    expect(document.body.textContent).toContain("这不是系统初始化")

    fillResetDialog("current-password", "错误短语")
    clickResetSubmit()
    await flushPromises()
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/vendor-test/step-up"))).toBe(false)
    expect((document.querySelector(
      "[data-testid='vendor-reset-password']",
    ) as HTMLInputElement).value).toBe("")
    expect((document.querySelector(
      "[data-testid='vendor-reset-confirmation']",
    ) as HTMLInputElement).value).toBe("")

    fillResetDialog("current-password", "清空联调设置")
    clickResetSubmit()
    await flushPromises()

    const stepUpCall = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/step-up"))
    expect(JSON.parse(String(stepUpCall?.[1].body))).toEqual({
      operation: "reset_configuration",
      password: "current-password",
    })
    const resetCall = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/reset"))
    expect(JSON.parse(String(resetCall?.[1].body))).toEqual({
      step_up_token: "single-use-reset-token",
    })
    expect(String(resetCall?.[1].body)).not.toContain("password")
    expect(String(resetCall?.[1].body)).not.toContain("清空联调设置")
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/vendor-test/seal-sessions"))).toBe(false)
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/vendor-test/credentials"))).toBe(false)
    expect(document.querySelector("[data-testid='vendor-reset-password']")).toBeNull()

    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()

    expect(wrapper.text()).toContain("待完成设置")
    expect(wrapper.text()).toContain("未激活")
    expect(wrapper.text()).toContain("尚未登记测试号码")
    expect(wrapper.text()).not.toContain("139****0001")
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms-platform:vendor-test:operation:v1")).toBeNull()
    expect(JSON.stringify(sessionStorage)).not.toContain("single-use-reset-token")
    expect(JSON.stringify(sessionStorage)).not.toContain("current-password")
    expect(localStorage.length).toBe(0)
    wrapper.unmount()
  })

  it("同步重复提交 reset 时只签发一次二次认证请求", async () => {
    const stepUpRequest = deferred<ReturnType<typeof response>>()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000081",
      operation_type: "reset_configuration",
      status: "running",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) return stepUpRequest.promise
      if (url.endsWith("/vendor-test/reset")) return response(operation, 202)
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("current-password", "清空联调设置")
    clickResetSubmit()
    clickResetSubmit()

    expect(fetch.mock.calls.filter(([url]) => url.endsWith("/vendor-test/step-up"))).toHaveLength(1)

    stepUpRequest.resolve(response({ token: "single-use-reset-token", expires_in: 300 }))
    await flushPromises()
    wrapper.unmount()
  })

  it("对话框 close 事件在关闭动画起点清空敏感字段且保留动作文案到 closed", async () => {
    vi.stubGlobal("fetch", consoleFetch())
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("close-password", "清空联调设置")
    const stepUpDialog = wrapper.findAllComponents(ElDialog).find(
      (dialog) => dialog.props("modelValue") === true,
    )
    expect(stepUpDialog).toBeDefined()

    stepUpDialog!.vm.$emit("close")
    await flushPromises()

    expect((document.querySelector(
      "[data-testid='vendor-reset-password']",
    ) as HTMLInputElement).value).toBe("")
    expect((document.querySelector(
      "[data-testid='vendor-reset-confirmation']",
    ) as HTMLInputElement).value).toBe("")
    expect(document.body.textContent).toContain("此操作不可撤销")
    wrapper.unmount()
  })

  it("取消与组件卸载都立即清空 reset 敏感状态", async () => {
    vi.stubGlobal("fetch", consoleFetch())
    const wrapper = mountConsole()
    await flushPromises()
    const setupState = (wrapper.vm.$ as unknown as {
      setupState: {
        stepUpPassword: string
        resetConfirmation: string
      }
    }).setupState

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("cancel-password", "清空联调设置")
    const cancel = document.querySelector("[data-testid='vendor-reset-cancel']") as HTMLButtonElement
    cancel.click()
    expect(setupState.stepUpPassword).toBe("")
    expect(setupState.resetConfirmation).toBe("")

    await flushPromises()
    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("unmount-password", "清空联调设置")
    wrapper.unmount()

    expect(setupState.stepUpPassword).toBe("")
    expect(setupState.resetConfirmation).toBe("")
    expect(JSON.stringify(sessionStorage)).not.toContain("unmount-password")
  })

  it("reset step-up 失效时保留当前登录，显示安全错误并清空敏感输入", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{\"username\":\"admin\"}")
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) {
        return response({
          code: "STEP_UP_EXPIRED",
          message: "二次认证已失效，请重新输入当前账号密码",
        }, 401)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const error = vi.spyOn(ElMessage, "error")
    const unauthorized = vi.fn()
    window.addEventListener("sms:unauthorized", unauthorized)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("expired-password", "清空联调设置")
    clickResetSubmit()
    await flushPromises()

    expect(error).toHaveBeenCalledWith("二次认证已失效，请重新输入当前账号密码")
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/vendor-test/reset"))).toBe(false)
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
    expect((document.querySelector(
      "[data-testid='vendor-reset-password']",
    ) as HTMLInputElement).value).toBe("")
    expect((document.querySelector(
      "[data-testid='vendor-reset-confirmation']",
    ) as HTMLInputElement).value).toBe("")

    const cancel = document.querySelector("[data-testid='vendor-reset-cancel']") as HTMLButtonElement
    cancel.click()
    await flushPromises()
    expect(document.querySelector("[data-testid='vendor-reset-password']")).toBeNull()
    window.removeEventListener("sms:unauthorized", unauthorized)
    wrapper.unmount()
  })

  it("step-up 成功但 reset 端点令牌失效时保留当前登录并清空敏感输入", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    sessionStorage.setItem("sms_user", "{\"username\":\"admin\"}")
    const fetch = consoleFetch(baseStatus, (url) => {
      if (url.endsWith("/vendor-test/step-up")) {
        return response({ token: "expired-after-issue", expires_in: 300 })
      }
      if (url.endsWith("/vendor-test/reset")) {
        return response({
          code: "STEP_UP_EXPIRED",
          message: "二次认证已过期或已使用，请重新认证后重试",
        }, 401)
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const error = vi.spyOn(ElMessage, "error")
    const unauthorized = vi.fn()
    window.addEventListener("sms:unauthorized", unauthorized)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("current-password", "清空联调设置")
    clickResetSubmit()
    await flushPromises()

    expect(error).toHaveBeenCalledWith("二次认证已过期或已使用，请重新认证后重试")
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/vendor-test/reset"))).toBe(true)
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
    expect(unauthorized).not.toHaveBeenCalled()
    expect((document.querySelector(
      "[data-testid='vendor-reset-password']",
    ) as HTMLInputElement).value).toBe("")
    expect((document.querySelector(
      "[data-testid='vendor-reset-confirmation']",
    ) as HTMLInputElement).value).toBe("")
    expect(JSON.stringify(sessionStorage)).not.toContain("expired-after-issue")
    window.removeEventListener("sms:unauthorized", unauthorized)
    wrapper.unmount()
  })

  it("shared step-up dialog 保持 resume_critical 二次认证路径", async () => {
    sessionStorage.setItem("sms_token", "admin.jwt")
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000043",
      operation_type: "resume",
      status: "succeeded",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: "2026-07-17T09:31:01+08:00",
    }
    const fetch = consoleFetch(
      { ...baseStatus, mode: "blocked", pause_kind: "critical" },
      (url) => {
        if (url.endsWith("/vendor-test/step-up")) {
          return response({ token: "resume-critical-token", expires_in: 300 })
        }
        if (url.endsWith("/vendor-test/resume")) return response(operation, 202)
        return undefined
      },
    )
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-resume']").trigger("click")
    await flushPromises()
    const password = document.querySelector(
      "[data-testid='vendor-step-up-password']",
    ) as HTMLInputElement
    password.value = "resume-password"
    password.dispatchEvent(new Event("input", { bubbles: true }))
    const submit = [...document.querySelectorAll("button")].find(
      (button) => button.textContent?.includes("验证并恢复"),
    ) as HTMLButtonElement
    submit.click()
    await flushPromises()

    const stepUpCall = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/step-up"))
    expect(JSON.parse(String(stepUpCall?.[1].body))).toEqual({
      operation: "resume_critical",
      password: "resume-password",
    })
    const resumeCall = fetch.mock.calls.find(([url]) => url.endsWith("/vendor-test/resume"))
    expect(JSON.parse(String(resumeCall?.[1].body))).toEqual({
      step_up_token: "resume-critical-token",
    })
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(JSON.stringify(sessionStorage)).not.toContain("resume-password")
    expect(JSON.stringify(sessionStorage)).not.toContain("resume-critical-token")
    wrapper.unmount()
  })

  it("保留 step-up dialog 移动端宽度与触控 CSS 契约", () => {
    expect(vendorTestConsoleSource).toContain('width="440px"')
    expect(vendorTestConsoleSource).toContain("max-width: calc(100vw - 32px)")
    expect(vendorTestConsoleSource).toContain("min-height: 44px")
    expect(vendorTestConsoleSource).toContain("@media (max-width: 360px)")
    expect(vendorTestConsoleSource).toContain("flex: 1 1 100%")
  })

  it("清空操作 pending 与失败状态提示部分设置可能已清理", async () => {
    vi.useFakeTimers()
    const operation = {
      operation_id: "00000000-0000-4000-8000-000000000044",
      operation_type: "reset_configuration",
      status: "requested",
      safe_code: null,
      vendor_code: null,
      batch_no: null,
      checkpoint_id: null,
      requested_at: "2026-07-17T09:31:00+08:00",
      completed_at: null,
    }
    const fetch = consoleFetch(baseStatus, (url, init) => {
      if (url.endsWith("/vendor-test/step-up")) {
        return response({ token: "single-use-reset-token", expires_in: 300 })
      }
      if (url.endsWith("/vendor-test/reset") && init.method === "POST") return response(operation, 202)
      if (url.endsWith(`/vendor-test/operations/${operation.operation_id}`)) {
        return response({
          ...operation,
          status: "failed",
          safe_code: "RESET_FAILED",
          completed_at: "2026-07-17T09:31:01+08:00",
        })
      }
      return undefined
    })
    vi.stubGlobal("fetch", fetch)
    const error = vi.spyOn(ElMessage, "error")
    const wrapper = mountConsole()
    await flushPromises()

    await wrapper.get("[data-testid='vendor-reset']").trigger("click")
    await flushPromises()
    fillResetDialog("current-password", "清空联调设置")
    clickResetSubmit()
    await flushPromises()
    expect(wrapper.text()).toContain("清空处理中，真实出口保持关闭，请勿重复操作")

    await vi.advanceTimersByTimeAsync(800)
    await flushPromises()
    expect(wrapper.text()).toContain(
      "清空未确认完成，部分设置可能已清理；真实出口保持关闭，请按安全代码修复后重试",
    )
    expect(wrapper.text()).not.toContain("原状")
    expect(wrapper.text()).not.toContain("现状保持不变")
    expect(wrapper.text()).toContain("RESET_FAILED")
    expect(error).toHaveBeenCalledWith(
      "清空联调设置未确认完成：RESET_FAILED；部分设置可能已清理，真实出口保持关闭，请修复后重试",
    )
    wrapper.unmount()
  })
})
