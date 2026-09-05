import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { vi } from "vitest"

import ReplyView from "../src/views/ReplyView.vue"
import { useSessionStore } from "../src/stores/session"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: {
      get: (name: string) => (name === "content-length" && status === 200 && body === undefined ? "0" : null),
    },
    json: async () => body,
  }
}

function reply(overrides: Record<string, unknown> = {}) {
  return {
    id: 5,
    phone: "138****8000",
    content: "TD",
    batch_no: "BATCH-1",
    reply_time: "2026-07-12T08:00:00+08:00",
    blacklisted: false,
    ...overrides,
  }
}

describe("回复查询", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("展示掩码回复并允许操作员确认后将回复号码加入退订黑名单", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const toast = vi.spyOn(ElMessage, "success")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ total: 1, items: [reply()] }))
      .mockResolvedValueOnce(response(undefined))
      .mockResolvedValueOnce(response({ total: 1, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    expect(wrapper.get("form.reply-filter-bar").classes()).toContain("reply-filter-bar")
    expect(wrapper.get("form.reply-filter-bar").classes()).not.toContain("filter-grid")
    expect(wrapper.findComponent({ name: "ElSegmented" }).exists()).toBe(false)
    expect(wrapper.text()).toContain("上行回复")
    expect(wrapper.text()).toContain("138****8000")
    expect(fetch.mock.calls[0][0]).toBe("/api/v1/web/replies")
    expect(fetch.mock.calls[0][1]).toEqual(expect.objectContaining({ method: "POST" }))
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toEqual({ page: 1, disposition: "all" })
    expect(String(fetch.mock.calls[0][0])).not.toContain("phone=")
    expect(wrapper.text()).toContain("TD")
    expect(wrapper.get(".reply-content").classes()).toContain("is-optout")
    const optout = wrapper.findAll("button").find((button) => button.text().includes("退订加黑"))
    expect(optout).toBeTruthy()
    await optout!.trigger("click")
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(confirm.mock.calls[0][0]).toContain("138****8000")
    expect(confirm.mock.calls[0][0]).toContain("加入后发送将自动剔除该号码；加黑行为与操作人将写入审计日志")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/replies/5/blacklist")
    expect(fetch.mock.calls[1][1].method).toBe("POST")
    expect(toast).toHaveBeenCalledWith("已加入退订黑名单 · 本次操作已记入审计")
  })

  it("取消退订确认时不发起加黑请求", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel")
    const fetch = vi.fn().mockResolvedValue(response({ total: 1, items: [reply()] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const optout = wrapper.findAll("button").find((button) => button.text().includes("退订加黑"))
    await optout!.trigger("click")
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("已加黑的回复在状态列显示已加黑且不再提供加黑入口", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ total: 1, items: [reply({ blacklisted: true })] })))
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "operator"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()

    const headers = wrapper.findAll(".reply-table th").map((header) => header.text())
    expect(headers).toContain("状态")
    expect(headers).toContain("处置")
    expect(wrapper.text()).toContain("已加黑")
    expect(wrapper.findAll("button").find((button) => button.text().includes("退订加黑"))).toBeUndefined()
  })

  it("手机号格式不合法时内联提示、提交前拦截且不发起查询请求", async () => {
    const warning = vi.spyOn(ElMessage, "warning")
    const fetch = vi.fn().mockResolvedValue(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    expect(fetch).toHaveBeenCalledTimes(1)

    await wrapper.get("[data-testid='reply-filter-phone']").setValue("12345")
    // el-form-item 的 error 展示经 100ms refDebounced，等待其生效
    await new Promise((resolve) => setTimeout(resolve, 120))
    await flushPromises()
    expect(wrapper.text()).toContain("手机号须为 11 位以 1 开头的数字")
    await wrapper.get("form.reply-filter-bar").trigger("submit")
    await flushPromises()

    expect(warning).toHaveBeenCalledWith("手机号须为 11 位以 1 开头的数字")
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("按条件查询无结果时切换为筛选空态，重置后恢复默认空态", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"

    const wrapper = mount(ReplyView, {
      global: { plugins: [pinia, ElementPlus] },
    })
    await flushPromises()
    expect(wrapper.text()).toContain("尚未采集到上行回复")

    await wrapper.get("[data-testid='reply-filter-phone']").setValue("13800138000")
    await wrapper.get("form.reply-filter-bar").trigger("submit")
    await flushPromises()
    expect(wrapper.text()).toContain("没有符合筛选条件的回复")
    expect(wrapper.text()).not.toContain("尚未采集到上行回复")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/replies")
    expect(JSON.parse(String(fetch.mock.calls[1][1].body))).toEqual({
      page: 1,
      phone: "13800138000",
      disposition: "all",
    })
    expect(String(fetch.mock.calls[1][0])).not.toContain("phone=")

    await wrapper.get("[data-testid='reply-reset']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("尚未采集到上行回复")
    expect(JSON.parse(String(fetch.mock.calls[2][1].body))).toEqual({ page: 1, disposition: "all" })
  })

  it("普通回复文案不高亮为退订语", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ total: 1, items: [reply({ content: "请勿再发" })] })))
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"
    const wrapper = mount(ReplyView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain("请勿再发")
    expect(wrapper.get(".reply-content").classes()).not.toContain("is-optout")
    wrapper.unmount()
  })

  it("viewer 角色可见状态列但隐藏处置列与加黑入口", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ total: 1, items: [reply({ blacklisted: true })] })))
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"
    const wrapper = mount(ReplyView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()

    const headers = wrapper.findAll(".reply-table th").map((header) => header.text())
    expect(headers).toContain("状态")
    expect(headers).not.toContain("处置")
    expect(wrapper.text()).toContain("已加黑")
    expect(wrapper.find("[data-testid='reply-optout-5']").exists()).toBe(false)
    expect(wrapper.text()).not.toContain("退订加黑")
  })

  it("处置 seg 切换按 all/pending_optout/blacklisted 传参并回到第一页", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ total: 0, items: [] }))
    vi.stubGlobal("fetch", fetch)
    const pinia = createPinia()
    setActivePinia(pinia)
    useSessionStore().role = "viewer"

    const wrapper = mount(ReplyView, { global: { plugins: [pinia, ElementPlus] } })
    await flushPromises()
    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toEqual({ page: 1, disposition: "all" })

    const seg = wrapper.get("[data-testid='reply-disposition-seg']")
    expect(seg.text()).toContain("全部")
    expect(seg.text()).toContain("待加黑退订")
    expect(seg.text()).toContain("已加黑")

    await wrapper.get("[data-testid='reply-disposition-pending_optout']").trigger("click")
    await flushPromises()
    expect(JSON.parse(String(fetch.mock.calls[1][1].body))).toEqual({
      page: 1,
      disposition: "pending_optout",
    })

    await wrapper.get("[data-testid='reply-disposition-blacklisted']").trigger("click")
    await flushPromises()
    expect(JSON.parse(String(fetch.mock.calls[2][1].body))).toEqual({
      page: 1,
      disposition: "blacklisted",
    })

    await wrapper.get("[data-testid='reply-disposition-all']").trigger("click")
    await flushPromises()
    expect(JSON.parse(String(fetch.mock.calls[3][1].body))).toEqual({ page: 1, disposition: "all" })
  })
})
