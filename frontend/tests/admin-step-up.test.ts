import { createPinia } from "pinia"
import { useSessionStore } from "../src/stores/session"
import { defineComponent, h } from "vue"
import ElementPlus from "element-plus"
import AdminStepUpDialog from "../src/components/AdminStepUpDialog.vue"
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

it.each(
  ["cancel", "session", "unmount"].flatMap((boundary) =>
    ["success", "failure"].map((outcome) => ({ boundary, outcome })),
  ),
)("已发出业务写入的 $outcome 在 $boundary 后不向旧调用方交付", async ({ boundary, outcome }) => {
  api.issueAdminStepUp.mockResolvedValueOnce("synthetic-grant")
  const { wrapper, controller } = setup()
  let resolve!: (value: { saved: boolean }) => void
  let reject!: (error: Error) => void
  const action = vi.fn(
    () =>
      new Promise<{ saved: boolean }>((yes, no) => {
        resolve = yes
        reject = no
      }),
  )
  const work = controller.run(intent, "修改角色", action)
  // 立即观察拒绝分支，避免测试延后断言引起未处理 Promise。
  const settled = work.then(
    (value) => ({ value, error: null }),
    (error: unknown) => ({ value: undefined, error }),
  )
  controller.updatePassword("synthetic-password")
  await controller.submit()
  await flushPromises()
  expect(action).toHaveBeenCalledTimes(1)
  if (boundary === "cancel") controller.cancel()
  else if (boundary === "session") useSessionStore(createPinia()).clear()
  else wrapper.unmount()
  if (outcome === "success") resolve({ saved: true })
  else reject(new Error("synthetic transport loss"))
  expect(await settled).toEqual({ value: undefined, error: null })
  expect(action).toHaveBeenCalledTimes(1)
  wrapper.unmount()
})

it("当前授权的业务写入失败仍向调用方传播", async () => {
  api.issueAdminStepUp.mockResolvedValueOnce("synthetic-grant")
  const { wrapper, controller } = setup()
  const error = new Error("synthetic current failure")
  const work = controller.run(intent, "修改角色", async () => {
    throw error
  })
  const settled = work.catch((value: unknown) => value)
  controller.updatePassword("synthetic-password")
  await controller.submit()
  expect(await settled).toBe(error)
  wrapper.unmount()
})

it.each(["success", "failure"])("新意图等待授权时，旧意图的 %s 不影响其提示、loading 或执行", async (outcome) => {
  api.issueAdminStepUp.mockReset()
  let resolveOld!: (token: string) => void
  let rejectOld!: (error: Error) => void
  let resolveCurrent!: (token: string) => void
  api.issueAdminStepUp.mockImplementationOnce(
    () =>
      new Promise<string>((resolve, reject) => {
        resolveOld = resolve
        rejectOld = reject
      }),
  )
  api.issueAdminStepUp.mockImplementationOnce(
    () =>
      new Promise<string>((resolve) => {
        resolveCurrent = resolve
      }),
  )
  const { wrapper, controller } = setup()
  const oldAction = vi.fn()
  const currentAction = vi.fn().mockResolvedValue("current-result")
  try {
    expect(controller.phase.value).toBe("closed")
    const oldWork = controller.run(intent, "原意图", oldAction)
    expect(controller.phase.value).toBe("prompting")
    controller.updatePassword("synthetic-old-password")
    const oldSubmit = controller.submit()
    const currentIntent = { ...intent, target_id: "8" }
    const currentWork = controller.run(currentIntent, "新意图", currentAction)
    expect(await oldWork).toBeUndefined()
    controller.updatePassword("synthetic-current-password")
    const currentSubmit = controller.submit()
    if (outcome === "success") resolveOld("synthetic-old-token")
    else rejectOld(new Error("synthetic-old-error"))
    await oldSubmit
    expect(controller.phase.value).toBe("authorizing")
    expect(controller.state.label).toBe("新意图")
    expect(controller.state.error).toBe("")
    expect(oldAction).not.toHaveBeenCalled()
    expect(currentAction).not.toHaveBeenCalled()
    resolveCurrent("synthetic-current-token")
    await currentSubmit
    expect(await currentWork).toBe("current-result")
    expect(currentAction).toHaveBeenCalledExactlyOnceWith("synthetic-current-token")
    expect(controller.phase.value).toBe("closed")
  } finally {
    wrapper.unmount()
  }
})

it.each(["success", "failure"])("重认证弹窗自动关闭不吞掉在途业务写入的 %s", async (outcome) => {
  api.issueAdminStepUp.mockResolvedValueOnce("synthetic-grant")
  let controller!: AdminStepUpController
  const wrapper = mount(
    defineComponent({
      setup() {
        controller = useAdminStepUp()
        return () => h(AdminStepUpDialog, { controller })
      },
    }),
    { attachTo: document.body, global: { plugins: [ElementPlus] } },
  )
  let resolve!: (value: string) => void
  let reject!: (value: Error) => void
  const action = vi.fn(
    () =>
      new Promise<string>((yes, no) => {
        resolve = yes
        reject = no
      }),
  )
  const work = controller.run(intent, "修改角色", action)
  const observed = work.then(
    (value) => ({ value }),
    (error: Error) => ({ error: error.message }),
  )
  try {
    await flushPromises()
    controller.updatePassword("synthetic-password")
    await controller.submit()
    await flushPromises()
    // Element Plus 在程序关闭的过渡结束也会回写 false；这不是用户取消。
    const dialog = wrapper.findComponent({ name: "ElDialog" })
    dialog.vm.$emit("close")
    dialog.vm.$emit("update:modelValue", false)
    await flushPromises()
    if (outcome === "success") resolve("saved")
    else reject(new Error("write failed"))
    expect(await observed).toEqual(outcome === "success" ? { value: "saved" } : { error: "write failed" })
    expect(action).toHaveBeenCalledTimes(1)
  } finally {
    wrapper.unmount()
  }
})

it("用户关闭重认证立即清空口令，迟到令牌不能执行写入", async () => {
  let grant!: (value: string) => void
  api.issueAdminStepUp.mockImplementationOnce(
    () =>
      new Promise<string>((done) => {
        grant = done
      }),
  )
  let controller!: AdminStepUpController
  const wrapper = mount(
    defineComponent({
      setup() {
        controller = useAdminStepUp()
        return () => h(AdminStepUpDialog, { controller })
      },
    }),
    { attachTo: document.body, global: { plugins: [ElementPlus] } },
  )
  const action = vi.fn()
  const work = controller.run(intent, "取消测试", action)
  try {
    await flushPromises()
    controller.updatePassword("synthetic-password")
    const submit = controller.submit()
    await flushPromises()
    await wrapper.findComponent({ name: "ElDialog" }).get("button.el-dialog__headerbtn").trigger("click")
    expect(controller.state.password).toBe("")
    expect(controller.phase.value).toBe("closed")
    grant("late-grant")
    await submit
    expect(await work).toBeUndefined()
    expect(action).not.toHaveBeenCalled()
  } finally {
    wrapper.unmount()
  }
})

it("旧关闭动画的回写不能取消新打开的授权意图", async () => {
  api.issueAdminStepUp.mockResolvedValue("synthetic-grant")
  let controller!: AdminStepUpController
  const wrapper = mount(
    defineComponent({
      setup() {
        controller = useAdminStepUp()
        return () => h(AdminStepUpDialog, { controller })
      },
    }),
    { attachTo: document.body, global: { plugins: [ElementPlus] } },
  )
  try {
    const first = controller.run(intent, "原意图", async () => "first")
    await flushPromises()
    controller.updatePassword("synthetic-password")
    await controller.submit()
    expect(await first).toBe("first")
    await flushPromises()
    const second = controller.run({ ...intent, target_id: "9" }, "新意图", async () => "second")
    await flushPromises()
    wrapper.findComponent({ name: "ElDialog" }).vm.$emit("update:modelValue", false)
    expect(controller.phase.value).toBe("prompting")
    controller.updatePassword("synthetic-password")
    await controller.submit()
    expect(await second).toBe("second")
  } finally {
    wrapper.unmount()
  }
})
