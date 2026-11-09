import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage } from "element-plus"
import { afterEach, expect, it, vi } from "vitest"

import { SESSION_CLEARING_EVENT } from "../src/api/sessionEvents"
import { defaultSessionDocument } from "../src/api/sessionDocument"
import PhoneReveal from "../src/components/PhoneReveal.vue"

const cleanups: Array<() => void> = []

function deferred() {
  let resolve!: (value: string) => void
  let reject!: (error: Error) => void
  const promise = new Promise<string>((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

function createReveal(reveal: () => Promise<string>) {
  const wrapper = mount(PhoneReveal, {
    props: { masked: "synthetic-mask", reveal, recordKey: "record-a", testid: "reveal" },
    global: { plugins: [ElementPlus] },
  })
  let mounted = true
  const unmount = () => {
    if (mounted) wrapper.unmount()
    mounted = false
  }
  cleanups.push(unmount)
  return { wrapper, unmount }
}

function notifications() {
  return {
    success: vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined })),
    error: vi.spyOn(ElMessage, "error").mockImplementation(() => ({ close: () => undefined })),
  }
}

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  vi.restoreAllMocks()
})

it("卸载后迟到成功不能重新填入已清空的组件引用", async () => {
  notifications()
  const pending = deferred()
  const { wrapper, unmount } = createReveal(() => pending.promise)
  const state = (wrapper.vm as unknown as { $: { setupState: Record<string, unknown> } }).$.setupState
  await wrapper.get("[data-testid='reveal']").trigger("click")
  unmount()
  expect(state.revealedPhone).toBe("")
  pending.resolve("synthetic-revealed-value")
  await flushPromises()
  expect(state.revealedPhone).toBe("")
})

it("卸载后迟到成功不能产生全局成功提示", async () => {
  const { success } = notifications()
  const pending = deferred()
  const { wrapper, unmount } = createReveal(() => pending.promise)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  unmount()
  pending.resolve("synthetic-revealed-value")
  await flushPromises()
  expect(success).not.toHaveBeenCalled()
})

it.each(["unmount", "session-clear", "session-generation"])("%s 后迟到失败不再通知", async (boundary) => {
  const { success, error } = notifications()
  const pending = deferred()
  const { wrapper, unmount } = createReveal(() => pending.promise)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  if (boundary === "unmount") unmount()
  else if (boundary === "session-clear") window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
  else defaultSessionDocument.invalidateGeneration()
  pending.reject(new Error("synthetic-late-error"))
  await flushPromises()
  expect(success).not.toHaveBeenCalled()
  expect(error).not.toHaveBeenCalled()
})

it.each(["close-reopen", "same-mask-new-record", "session-clear"])(
  "%s 后旧成功不能写入新对象或提前结束新请求的 loading",
  async (boundary) => {
    const { success, error } = notifications()
    const old = deferred()
    const current = deferred()
    const reveal = vi.fn<() => Promise<string>>().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
    const { wrapper } = createReveal(reveal)
    await wrapper.get("[data-testid='reveal']").trigger("click")
    if (boundary === "close-reopen") {
      await wrapper.setProps({ active: false })
      await wrapper.setProps({ active: true })
    } else if (boundary === "same-mask-new-record") {
      await wrapper.setProps({ recordKey: "record-b" })
    } else {
      window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
      await flushPromises()
    }
    await wrapper.get("[data-testid='reveal']").trigger("click")
    expect(reveal).toHaveBeenCalledTimes(2)
    old.resolve("synthetic-old-result")
    await flushPromises()
    expect(wrapper.find(".revealed-phone").exists()).toBe(false)
    expect(wrapper.get("[data-testid='reveal']").classes()).toContain("is-loading")
    expect(success).not.toHaveBeenCalled()
    expect(error).not.toHaveBeenCalled()
    current.resolve("synthetic-current-result")
    await flushPromises()
    expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-current-result")
    expect(wrapper.emitted("revealed")).toEqual([["synthetic-current-result"]])
    expect(success).toHaveBeenCalledTimes(1)
  },
)

it("旧失败和 finally 不能关闭重新打开后的 loading 或覆盖其错误", async () => {
  const { error } = notifications()
  const old = deferred()
  const current = deferred()
  const reveal = vi.fn<() => Promise<string>>().mockReturnValueOnce(old.promise).mockReturnValueOnce(current.promise)
  const { wrapper } = createReveal(reveal)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await wrapper.setProps({ active: false })
  await wrapper.setProps({ active: true })
  await wrapper.get("[data-testid='reveal']").trigger("click")
  old.reject(new Error("synthetic-old-error"))
  await flushPromises()
  expect(error).not.toHaveBeenCalled()
  expect(wrapper.get("[data-testid='reveal']").classes()).toContain("is-loading")
  current.reject(new Error("synthetic-current-error"))
  await flushPromises()
  expect(error).toHaveBeenCalledExactlyOnceWith("synthetic-current-error")
  expect(wrapper.get("[data-testid='reveal']").classes()).not.toContain("is-loading")
})

it.each(["close", "record-change", "session-clear"])("%s 立即清空已经展示的值", async (boundary) => {
  notifications()
  const reveal = vi.fn<() => Promise<string>>().mockResolvedValue("synthetic-current-result")
  const { wrapper } = createReveal(reveal)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await flushPromises()
  expect(wrapper.find(".revealed-phone").exists()).toBe(true)
  if (boundary === "close") await wrapper.setProps({ active: false })
  else if (boundary === "record-change") await wrapper.setProps({ recordKey: "record-b" })
  else window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
  await flushPromises()
  expect(wrapper.find(".revealed-phone").exists()).toBe(false)
  if (boundary === "close") {
    await wrapper.get("[data-testid='reveal']").trigger("click")
    expect(reveal).toHaveBeenCalledTimes(1)
  }
})

it("正常查看、重复点击、重新隐藏及失败重试保持既有行为", async () => {
  const { success, error } = notifications()
  const pending = deferred()
  const reveal = vi.fn<() => Promise<string>>().mockReturnValueOnce(pending.promise)
  const { wrapper } = createReveal(reveal)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await wrapper.get("[data-testid='reveal']").trigger("click")
  expect(reveal).toHaveBeenCalledTimes(1)
  pending.resolve("synthetic-first-result")
  await flushPromises()
  expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-first-result")
  await wrapper.get("[data-testid='phone-hide']").trigger("click")
  expect(wrapper.find(".revealed-phone").exists()).toBe(false)
  reveal.mockRejectedValueOnce(new Error("synthetic-current-error"))
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await flushPromises()
  expect(error).toHaveBeenCalledExactlyOnceWith("synthetic-current-error")
  reveal.mockResolvedValueOnce("synthetic-retry-result")
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await flushPromises()
  expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-retry-result")
  expect(reveal).toHaveBeenCalledTimes(3)
  expect(success).toHaveBeenCalledTimes(2)
})

it("同一记录的 inline reveal 回调重新创建不撤销正在进行的查看", async () => {
  notifications()
  const pending = deferred()
  const { wrapper } = createReveal(() => pending.promise)
  await wrapper.get("[data-testid='reveal']").trigger("click")
  await wrapper.setProps({ reveal: () => Promise.resolve("synthetic-unused") })
  pending.resolve("synthetic-original")
  await flushPromises()
  expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-original")
})
