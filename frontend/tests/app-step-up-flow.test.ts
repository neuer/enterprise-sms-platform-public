import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { afterEach, describe, expect, it, vi } from "vitest"
import AppManagementView from "../src/views/AppManagementView.vue"
import AppEditorDrawer from "../src/components/AppEditorDrawer.vue"
import type { AppPayload } from "../src/api/apps"
import AdminStepUpDialog from "../src/components/AdminStepUpDialog.vue"
import { useSessionStore } from "../src/stores/session"

const app = {
  id: 1,
  name: "app-iam",
  dept: "平台技术部",
  allowed_categories: ["verify"],
  default_sign: "青鸾平台",
  daily_quota: 10000,
  rate_limit_per_min: 60,
  recipient_limit_per_min: 10000,
  segment_limit_per_min: 10000,
  max_in_flight_chunks: 200,
  allow_market_api_bulk: false,
  blacklist_check: true,
  freq_override: null,
  allowed_ips: [],
  ip_allowlist_exempt_until: null,
  unlimited_quota_exempt_until: null,
  admission_exempt_note: null,
  callback_url: null,
  callback_report_enabled: false,
  status: 1,
  api_key_prefix: "sk-7f3a9c2e",
  old_key_prefix: null,
  old_key_expires_at: null,
  callback_secret_configured: true,
  created_at: "2026-04-02T10:18:44+08:00",
  updated_at: "2026-04-02T10:18:44+08:00",
}

function response(body: unknown) {
  return { ok: true, status: 200, headers: new Headers(), json: async () => body }
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  document.body.innerHTML = ""
})

async function setup(options: { delayedGrant?: boolean; delayedWrite?: boolean } = {}) {
  let resolveGrant!: (value: ReturnType<typeof response>) => void
  let resolveWrite!: (value: ReturnType<typeof response>) => void
  const grant = new Promise<ReturnType<typeof response>>((done) => {
    resolveGrant = done
  })
  const write = new Promise<ReturnType<typeof response>>((done) => {
    resolveWrite = done
  })
  const requests: { url: string; init?: RequestInit }[] = []
  const fetch = vi.fn(async (url: string, init?: RequestInit) => {
    requests.push({ url: String(url), init })
    if (String(url).endsWith("/admin/step-up"))
      return options.delayedGrant ? grant : response({ token: "synthetic-grant", expires_in: 300 })
    if (String(url).endsWith("/rotate-key"))
      return options.delayedWrite
        ? write
        : response({ api_key: "synthetic-once-key", old_key_expires_at: "2026-04-05T00:00:00Z" })
    if (String(url).endsWith("/admin/configs")) return response([{ key: "key_grace_hours", value: "72" }])
    if (String(url).includes("/reports/stats")) return response({ total: 0, items: [], summary: {}, dim_summary: [] })
    return response([app])
  })
  vi.stubGlobal("fetch", fetch)
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const pinia = createPinia()
  const wrapper = mount(AppManagementView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  await wrapper.get("[data-testid='app-detail-1']").trigger("click")
  await flushPromises()
  await wrapper.get("[data-testid='rotate-key-1']").trigger("click")
  await flushPromises()
  const controller = wrapper.findComponent(AdminStepUpDialog).props("controller")
  return { wrapper, controller, requests, resolveGrant, resolveWrite, pinia }
}

describe("应用二次认证 HTTP 与生命周期", () => {
  it("确认后仍须验证密码，签发和写入绑定同一目标及版本", async () => {
    const { wrapper, controller, requests, pinia } = await setup()
    try {
      expect(controller.state.open).toBe(true)
      expect(requests.filter((r) => r.url.endsWith("/rotate-key"))).toHaveLength(0)
      controller.updatePassword("synthetic-password")
      await controller.submit()
      await flushPromises()
      const issue = requests.find((r) => r.url.endsWith("/admin/step-up"))!
      expect(JSON.parse(String(issue.init?.body))).toEqual({
        operation: "app_rotate_key",
        target_id: "1",
        parameters: { expected_updated_at: app.updated_at },
        password: "synthetic-password",
      })
      const writes = requests.filter((r) => r.url.endsWith("/rotate-key"))
      expect(writes).toHaveLength(1)
      const headers = new Headers(writes[0].init?.headers)
      expect(headers.get("X-Admin-Step-Up")).toBe("synthetic-grant")
      expect(headers.get("X-App-Revision")).toBe(app.updated_at)
      expect(controller.state.password).toBe("")
      expect(wrapper.text()).toContain("synthetic-once-key")
      useSessionStore(pinia).clear()
      await flushPromises()
      expect(wrapper.text()).not.toContain("synthetic-once-key")
    } finally {
      wrapper.unmount()
    }
  })

  it("编辑提交冻结配置和原版本，不读取认证期间变化的表单对象", async () => {
    const { wrapper, controller, requests } = await setup()
    try {
      controller.cancel()
      await flushPromises()
      await wrapper.get("[data-testid='edit-app-1']").trigger("click")
      await flushPromises()
      const metadata = new Set([
        "id",
        "name",
        "api_key_prefix",
        "old_key_prefix",
        "old_key_expires_at",
        "callback_secret_configured",
        "created_at",
        "updated_at",
      ])
      const payload = JSON.parse(
        JSON.stringify(Object.fromEntries(Object.entries(app).filter(([key]) => !metadata.has(key)))),
      ) as AppPayload
      const original = JSON.parse(JSON.stringify(payload))
      wrapper.findComponent(AppEditorDrawer).vm.$emit("save", payload, app.name, app.id, app.updated_at)
      await flushPromises()
      payload.dept = "changed-after-confirmation"
      payload.allowed_categories.push("market")
      controller.updatePassword("synthetic-password")
      await controller.submit()
      await flushPromises()
      const issue = requests.find((r) => r.url.endsWith("/admin/step-up"))!
      const binding = JSON.parse(String(issue.init?.body))
      expect(binding.operation).toBe("app_update")
      expect(binding.parameters).toEqual({ config: original, expected_updated_at: app.updated_at })
      const writes = requests.filter((r) => r.init?.method === "PUT")
      expect(writes).toHaveLength(1)
      expect(JSON.parse(String(writes[0].init?.body))).toEqual(original)
      expect(new Headers(writes[0].init?.headers).get("X-App-Revision")).toBe(app.updated_at)
    } finally {
      wrapper.unmount()
    }
  })

  it.each(["cancel", "session", "unmount"])("%s 后迟到授权不能轮换密钥", async (kind) => {
    const { wrapper, controller, requests, resolveGrant, pinia } = await setup({ delayedGrant: true })
    try {
      controller.updatePassword("synthetic-password")
      const pending = controller.submit()
      await flushPromises()
      if (kind === "cancel") controller.cancel()
      else if (kind === "session") useSessionStore(pinia).clear()
      else wrapper.unmount()
      resolveGrant(response({ token: "late-grant", expires_in: 300 }))
      await pending
      await flushPromises()
      expect(requests.filter((r) => r.url.endsWith("/rotate-key"))).toHaveLength(0)
      expect(controller.state.password).toBe("")
    } finally {
      wrapper.unmount()
    }
  })

  it("会话切换后丢弃已经提交操作的迟到明文凭据", async () => {
    const { wrapper, controller, requests, resolveWrite, pinia } = await setup({ delayedWrite: true })
    try {
      controller.updatePassword("synthetic-password")
      await controller.submit()
      await flushPromises()
      expect(requests.filter((r) => r.url.endsWith("/rotate-key"))).toHaveLength(1)
      useSessionStore(pinia).clear()
      resolveWrite(response({ api_key: "late-secret-must-not-display", old_key_expires_at: "2026-04-05T00:00:00Z" }))
      await flushPromises()
      expect(document.body.textContent).not.toContain("late-secret-must-not-display")
    } finally {
      wrapper.unmount()
    }
  })
})
