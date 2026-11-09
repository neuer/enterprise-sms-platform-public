import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage } from "element-plus"
import { afterEach, expect, it, vi } from "vitest"

import * as queries from "../src/api/queries"
import BatchDetailDrawer from "../src/components/BatchDetailDrawer.vue"
import PhoneReveal from "../src/components/PhoneReveal.vue"

const batch: queries.BatchItem = {
  batch_no: "synthetic-batch",
  category: "notice",
  channel: "web",
  app_name: null,
  creator: "synthetic-operator",
  dept: "合成部门",
  content: "合成内容",
  status: "completed",
  deferred_reason: null,
  resend_of: null,
  is_test: true,
  segments: 1,
  quota_cost: 1,
  total: 1,
  removed_freq_limit: 0,
  delivered: 1,
  failed: 0,
  unknown: 0,
  pending: 0,
  sent: 0,
  other: 0,
  cancelled: 0,
  rejected: 0,
  expired: 0,
  needs_review: 0,
  scheduled_at: null,
  created_at: "2026-11-09T08:00:00+08:00",
  can_resend_failed: false,
  resend_covered_failed: 0,
  resend_uncovered_failed: 0,
  excluded_before_send: 0,
}
const row: queries.BatchMessage = {
  id: 1,
  phone: "synthetic-mask",
  status: "delivered",
  batch_status: "completed",
  display_status: "delivered",
  vendor_task_id: null,
  report_desc: null,
  report_time: null,
}
const cleanups: Array<() => void> = []

function deferred() {
  let resolve!: (value: { phone: string }) => void
  const promise = new Promise<{ phone: string }>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

async function setup() {
  const wrapper = mount(BatchDetailDrawer, {
    props: {
      modelValue: true,
      detailStatus: "",
      detailPage: 1,
      selected: batch,
      details: [row],
      detailTotal: 1,
      detailsLoading: false,
      canWrite: false,
      canDecrypt: true,
      canScheduleOps: false,
      canResendFailed: false,
      detailUnconfirmed: false,
      cancelling: false,
      resending: false,
    },
    attachTo: document.body,
    global: { plugins: [ElementPlus] },
  })
  cleanups.push(() => wrapper.unmount())
  await flushPromises()
  return wrapper
}

afterEach(() => {
  cleanups.splice(0).forEach((cleanup) => cleanup())
  vi.restoreAllMocks()
  document.body.innerHTML = ""
})

it("抽屉 close 事件立即清空展示，不等待 model 回写或销毁子组件", async () => {
  vi.spyOn(queries, "decryptMessagePhone").mockResolvedValue({ phone: "synthetic-revealed" })
  vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
  const wrapper = await setup()
  await wrapper.get("[data-testid='batch-phone-decrypt-1']").trigger("click")
  await flushPromises()
  expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-revealed")
  wrapper.getComponent({ name: "ElDrawer" }).vm.$emit("close")
  await flushPromises()
  expect(wrapper.props("modelValue")).toBe(true)
  expect(wrapper.findComponent(PhoneReveal).exists()).toBe(true)
  expect(wrapper.find(".revealed-phone").exists()).toBe(false)
  expect((wrapper.get("[data-testid='batch-phone-decrypt-1']").element as HTMLButtonElement).disabled).toBe(true)
})

it("保留挂载的抽屉关闭再打开，旧响应不影响新请求", async () => {
  const old = deferred()
  const current = deferred()
  const decrypt = vi
    .spyOn(queries, "decryptMessagePhone")
    .mockReturnValueOnce(old.promise)
    .mockReturnValueOnce(current.promise)
  const success = vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
  const wrapper = await setup()
  await wrapper.get("[data-testid='batch-phone-decrypt-1']").trigger("click")
  const drawer = wrapper.getComponent({ name: "ElDrawer" })
  drawer.vm.$emit("close")
  await flushPromises()
  drawer.vm.$emit("open")
  await flushPromises()
  await wrapper.get("[data-testid='batch-phone-decrypt-1']").trigger("click")
  expect(decrypt).toHaveBeenCalledTimes(2)
  old.resolve({ phone: "synthetic-old" })
  await flushPromises()
  expect(success).not.toHaveBeenCalled()
  expect(wrapper.find(".revealed-phone").exists()).toBe(false)
  expect(wrapper.get("[data-testid='batch-phone-decrypt-1']").classes()).toContain("is-loading")
  current.resolve({ phone: "synthetic-current" })
  await flushPromises()
  expect(wrapper.get(".revealed-phone").text()).toBe("synthetic-current")
})

it.each(["permission-loss", "same-mask-new-record"])("%s 后父级撤销旧记录的展示", async (boundary) => {
  const old = deferred()
  vi.spyOn(queries, "decryptMessagePhone").mockReturnValueOnce(old.promise)
  const success = vi.spyOn(ElMessage, "success").mockImplementation(() => ({ close: () => undefined }))
  const wrapper = await setup()
  await wrapper.get("[data-testid='batch-phone-decrypt-1']").trigger("click")
  if (boundary === "permission-loss") await wrapper.setProps({ canDecrypt: false })
  else await wrapper.setProps({ details: [{ ...row, id: 2 }] })
  old.resolve({ phone: "synthetic-old" })
  await flushPromises()
  expect(success).not.toHaveBeenCalled()
  expect(wrapper.find(".revealed-phone").exists()).toBe(false)
  if (boundary === "permission-loss") expect(wrapper.findComponent(PhoneReveal).exists()).toBe(false)
  else expect(wrapper.getComponent(PhoneReveal).props("recordKey")).toBe(2)
})
