import { createPinia } from "pinia"
import { useSessionStore } from "../src/stores/session"
import { defineComponent, h } from "vue"
import { flushPromises, mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"
import { useAdminStepUp, type AdminStepUpController } from "../src/composables/useAdminStepUp"
const api = vi.hoisted(() => ({ issueAdminStepUp: vi.fn() }))
vi.mock("../src/api/adminStepUp", () => api)

function setup() {
  let controller!: AdminStepUpController
  const wrapper = mount(
    defineComponent({
      setup() {
        controller = useAdminStepUp()
        return () => h("div")
      },
    }),
  )
  return { wrapper, controller }
}
const intent = {
  operation: "user_role_change" as const,
  target_id: "7",
  parameters: { role: "admin", role_override: true },
}

describe("管理员二次认证组件生命周期", () => {
  it("只为提交快照执行一次操作，口令和令牌不写浏览器存储", async () => {
    api.issueAdminStepUp.mockResolvedValueOnce("synthetic-token")
    const local = vi.spyOn(Storage.prototype, "setItem")
    const { wrapper, controller } = setup()
    const action = vi.fn().mockResolvedValue({ success: true })
    try {
      const editing = { ...intent, parameters: { ...intent.parameters } }
      const work = controller.run(editing, "修改角色", action)
      editing.parameters.role = "viewer"
      controller.state.password = "synthetic-password"
      await controller.submit()
      expect(await work).toEqual({ success: true })
      expect(api.issueAdminStepUp).toHaveBeenCalledWith(intent, "synthetic-password")
      expect(action).toHaveBeenCalledExactlyOnceWith("synthetic-token")
      expect(controller.state.password).toBe("")
      expect(JSON.stringify(controller.state)).not.toContain("synthetic-token")
      expect(local).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
      local.mockRestore()
    }
  })

  it.each(["cancel", "session", "unmount"])("%s 后迟到令牌不能执行操作", async (kind) => {
    let resolve!: (value: string) => void
    api.issueAdminStepUp.mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done
        }),
    )
    const { wrapper, controller } = setup()
    const action = vi.fn()
    const work = controller.run(intent, "修改角色", action)
    controller.state.password = "synthetic-password"
    const pending = controller.submit()
    if (kind === "cancel") controller.cancel()
    else if (kind === "session") useSessionStore(createPinia()).clear()
    else wrapper.unmount()
    resolve("late-token")
    await pending
    expect(await work).toBeUndefined()
    expect(action).not.toHaveBeenCalled()
    expect(controller.state.password).toBe("")
    wrapper.unmount()
  })

  it("真实会话清理入口清除尚未提交的密码并关闭弹窗", async () => {
    const { wrapper, controller } = setup()
    const action = vi.fn()
    const work = controller.run(intent, "修改角色", action)
    controller.state.password = "synthetic-password"
    useSessionStore(createPinia()).clear()
    expect(await work).toBeUndefined()
    expect(controller.state.open).toBe(false)
    expect(controller.state.password).toBe("")
    expect(action).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it("失败后清空密码且允许重新输入，不产生业务操作", async () => {
    api.issueAdminStepUp.mockRejectedValueOnce(new Error("synthetic invalid password"))
    const { wrapper, controller } = setup()
    const action = vi.fn()
    const work = controller.run(intent, "修改角色", action)
    controller.state.password = "wrong"
    await controller.submit()
    await flushPromises()
    expect(controller.state.open).toBe(true)
    expect(controller.state.busy).toBe(false)
    expect(controller.state.password).toBe("")
    expect(action).not.toHaveBeenCalled()
    controller.cancel()
    await work
    wrapper.unmount()
  })
})
