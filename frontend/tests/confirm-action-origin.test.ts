import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, expect, it, vi } from "vitest"
import { useSessionStore } from "../src/stores/session"
import type { BatchItem } from "../src/api/queries"
const api = vi.hoisted(() => ({
  cancelBatch: vi.fn(),
  resendFailedBatch: vi.fn().mockResolvedValue({ batch_no: "new" }),
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
