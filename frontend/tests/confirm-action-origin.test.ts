import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { nextTick } from "vue"
import { afterEach, expect, it, vi } from "vitest"
import { useSessionStore } from "../src/stores/session"
import type { BatchItem } from "../src/api/queries"
const api = vi.hoisted(() => ({
  cancelBatch: vi.fn(),
  resendFailedBatch: vi.fn().mockResolvedValue({
    batch_no: "new",
    resend_of: "original",
    accepted: 1,
    status: "queued",
    idempotent: false,
  }),
  revokeUserSessions: vi.fn(),
  getApp: vi.fn(),
  updateApp: vi.fn(),
}))
vi.mock("../src/api/queries", async (original) => ({
  ...(await original<object>()),
  ...api,
  listBatches: vi.fn().mockResolvedValue({ total: 0, items: [] }),
}))
vi.mock("../src/api/apps", async (original) => ({
  ...(await original<object>()),
  listApps: vi.fn().mockResolvedValue([]),
  getApp: api.getApp,
  updateApp: api.updateApp,
}))
vi.mock("../src/api/users", async (original) => ({
  ...(await original<object>()),
  listUsers: vi.fn().mockResolvedValue({ total: 0, items: [] }),
  revokeUserSessions: api.revokeUserSessions,
}))
vi.mock("../src/api/auth", async (original) => ({
  ...(await original<object>()),
  passwordPolicyRequest: vi.fn().mockResolvedValue({}),
}))
vi.mock("../src/api/admin", () => ({ listConfigs: vi.fn().mockResolvedValue([]) }))
vi.mock("../src/api/reports", () => ({ getReport: vi.fn().mockResolvedValue({ rows: [] }) }))
vi.mock("../src/api/signs", () => ({ listSigns: vi.fn().mockResolvedValue([]) }))
vi.mock("vue-router", () => ({ useRoute: () => ({ query: {} }) }))
import UserView from "../src/views/UserView.vue"
import AppManagementView from "../src/views/AppManagementView.vue"
import type { ManagedUser } from "../src/api/users"
import type { ManagedApp } from "../src/api/apps"
import BatchView from "../src/views/BatchView.vue"

afterEach(() => {
  vi.restoreAllMocks()
  vi.clearAllMocks()
})
it.each(
  (["cancelSelected", "resendFailed"] as const).flatMap((action) =>
    ["target", "session", "dispose", "unchanged"].map((change) => ({ action, change })),
  ),
)("批次操作 $action 在 $change 后保持目标", async ({ action, change }) => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "admin"
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as {
    selected: BatchItem
    cancelSelected: () => Promise<void>
    resendFailed: () => Promise<void>
  }
  vm.selected = { batch_no: "original", status: "scheduled", channel: "web", failed: 1 } as BatchItem
  let resolve!: (value: never) => void
  vi.spyOn(ElMessageBox, "confirm").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }) as never,
  )
  const pending = vm[action]()
  if (change === "target")
    vm.selected = { batch_no: "replacement", status: "scheduled", channel: "web", failed: 1 } as BatchItem
  if (change === "session") {
    useSessionStore().clear()
    useSessionStore().role = "admin"
  }
  if (change === "dispose") wrapper.unmount()
  resolve("confirm" as never)
  await pending
  if (change === "unchanged")
    expect(action === "cancelSelected" ? api.cancelBatch : api.resendFailedBatch).toHaveBeenCalledWith("original")
  else {
    expect(api.cancelBatch).not.toHaveBeenCalled()
    expect(api.resendFailedBatch).not.toHaveBeenCalled()
  }
  if (change !== "dispose") wrapper.unmount()
})

it("首次重发提示已创建，完成后重复操作提示既有子批次状态", async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "admin"
  const toast = vi.spyOn(ElMessage, "success")
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  api.resendFailedBatch
    .mockResolvedValueOnce({
      batch_no: "child-1",
      resend_of: "original",
      accepted: 1,
      status: "queued",
      idempotent: false,
    })
    .mockResolvedValueOnce({
      batch_no: "child-1",
      resend_of: "original",
      accepted: 1,
      status: "completed",
      idempotent: true,
    })
  const wrapper = mount(BatchView, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as {
    selected: BatchItem
    drawer: boolean
    resendFailed: () => Promise<void>
  }
  vm.drawer = true
  vm.selected = {
    batch_no: "original",
    status: "completed",
    channel: "web",
    failed: 1,
    dept: "平台部",
    created_at: "2026-10-27T01:00:00+08:00",
    category: "notice",
    app_name: null,
    creator: "管理员",
    content: "通知",
    deferred_reason: null,
    resend_of: null,
    is_test: false,
    segments: 1,
    quota_cost: 1,
    total: 1,
    removed_freq_limit: 0,
    delivered: 0,
    unknown: 0,
    pending: 0,
    sent: 0,
    other: 0,
    scheduled_at: null,
  } as BatchItem
  await vm.resendFailed()
  await flushPromises()
  expect(toast).toHaveBeenCalledWith("重发批次 child-1 已创建，当前状态：排队中 · 本次操作已记入审计")
  expect(wrapper.get("[data-testid='view-resend-batch']").text()).toContain("child-1")
  expect(wrapper.get("[data-testid='view-resend-batch']").text()).toContain("排队中")

  await vm.resendFailed()
  await flushPromises()
  expect(toast).toHaveBeenCalledWith("重发批次 child-1 已存在，当前状态：已完成")
  expect(wrapper.get("[data-testid='view-resend-batch']").text()).toContain("已完成")
  expect(api.resendFailedBatch).toHaveBeenCalledTimes(2)
  wrapper.unmount()
})

it("强制下线确认后不得跨会话提交", async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  const wrapper = mount(UserView, { global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  let resolve!: (value: never) => void
  vi.spyOn(ElMessageBox, "confirm").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }) as never,
  )
  const pending = (wrapper.vm as unknown as { forceLogout: (user: ManagedUser) => Promise<void> }).forceLogout({
    account_id: 99,
    username: "synthetic",
  } as ManagedUser)
  useSessionStore().clear()
  resolve("confirm" as never)
  await pending
  expect(api.revokeUserSessions).not.toHaveBeenCalled()
  wrapper.unmount()
})

it("确认启用后读取配置期间卸载，不发出最终写入", async () => {
  const wrapper = mount(AppManagementView, { global: { plugins: [createPinia(), ElementPlus] } })
  await flushPromises()
  let resolve!: (value: ManagedApp) => void
  api.getApp.mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }),
  )
  vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
  const pending = (wrapper.vm as unknown as { enable: (app: ManagedApp) => Promise<void> }).enable({
    id: 99,
    name: "synthetic",
  } as ManagedApp)
  await flushPromises()
  expect(api.getApp).toHaveBeenCalledWith(99)
  wrapper.unmount()
  resolve({ id: 99 } as ManagedApp)
  await pending
  expect(api.updateApp).not.toHaveBeenCalled()
})

it("真实批次确认在强制下线后立即消失，后续新确认不被全局关闭", async () => {
  const pinia = createPinia()
  setActivePinia(pinia)
  useSessionStore().role = "admin"
  const closeAll = vi.spyOn(ElMessageBox, "close")
  const wrapper = mount(BatchView, { attachTo: document.body, global: { plugins: [pinia, ElementPlus] } })
  await flushPromises()
  const vm = wrapper.vm as unknown as {
    selected: BatchItem
    cancelSelected: () => Promise<void>
  }
  vm.selected = { batch_no: "original", status: "scheduled", channel: "web", failed: 1 } as BatchItem
  const pending = vm.cancelSelected()
  await nextTick()
  expect(document.body.textContent).toContain("取消批次 original")
  useSessionStore().clear()
  await pending
  expect(api.cancelBatch).not.toHaveBeenCalled()
  expect(document.body.textContent).not.toContain("配额将按规则回补")
  expect(closeAll).not.toHaveBeenCalled()

  useSessionStore().role = "admin"
  vm.selected = { batch_no: "fresh", status: "scheduled", channel: "web", failed: 1 } as BatchItem
  const next = vm.cancelSelected()
  await nextTick()
  expect(document.body.textContent).toContain("fresh")
  await nextTick()
  expect(document.body.textContent).toContain("fresh")
  expect(closeAll).not.toHaveBeenCalled()
  useSessionStore().clear()
  await next
  wrapper.unmount()
  ElMessageBox.close()
})
