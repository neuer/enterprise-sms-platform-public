import { flushPromises, mount } from "@vue/test-utils"
import type { VueWrapper } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { vi } from "vitest"

import SegmentBar from "../src/components/SegmentBar.vue"
import SendView from "../src/views/SendView.vue"

describe("人工发送工作台", () => {
  it("测试发送显示后端运行策略中的号码上限", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => String(url).endsWith("/reports/dashboard")
        ? { ui_policy: { test_send_max: 7 } }
        : [],
    })))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("最多 7 个号码")
    expect(wrapper.text()).not.toContain("号码上限以系统参数为准")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("SegmentBar 只按服务端分段数据渲染", () => {
    const wrapper = mount(SegmentBar, {
      props: {
        parts: [
          { used: 67, capacity: 67, partial: false },
          { used: 13, capacity: 67, partial: true },
        ],
      },
    })

    expect(wrapper.findAll("[data-testid='segment-part']")).toHaveLength(2)
    expect(wrapper.text()).toContain("67 / 67")
    expect(wrapper.text()).toContain("13 / 67")
  })

  it("营销发送未勾选同意时禁止提交", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, headers: { get: () => null }, json: async () => [] }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })

    await wrapper.get("[data-testid='category-market']").trigger("click")

    expect(wrapper.get("[data-testid='market-consent']").text()).toContain("明确同意")
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeDefined()
    expect(wrapper.text()).toContain("同意状态将写入审计")
    vi.unstubAllGlobals()
  })

  it("只提供已审核模板并按变量规格生成渲染预览", async () => {
    const templates = [
      { id: 7, name: "登录验证码", content: "验证码{1}，{2}分钟内有效", var_specs: [{ pos: 1, max_len: 6 }, { pos: 2, max_len: 2 }], dept: "业务一部", vendor_template_id: "T7", vendor_state: "approved", vendor_reject_reason: null },
      { id: 8, name: "未审核模板", content: "草稿{1}", var_specs: [{ pos: 1, max_len: 4 }], dept: "业务一部", vendor_template_id: null, vendor_state: "pending", vendor_reject_reason: null },
    ]
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, headers: { get: () => null }, json: async () => templates }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    ;(wrapper.vm as unknown as { form: { contentMode: string } }).form.contentMode = "template"
    await wrapper.vm.$nextTick()

    const options = wrapper.findAllComponents({ name: "ElOption" }).map((item) => item.props("label"))
    expect(options).toContain("登录验证码")
    expect(options).not.toContain("未审核模板")
    const select = wrapper.findComponent({ name: "ElSelect" }) as VueWrapper
    select.vm.$emit("update:modelValue", "7")
    select.vm.$emit("change", "7")
    await wrapper.vm.$nextTick()
    expect(wrapper.findAll("[data-testid='template-param']")).toHaveLength(2)
    expect(wrapper.text()).toContain("模板渲染预览")
    vi.unstubAllGlobals()
  })

  it("剔除清单使用带 Bearer 的按钮下载而不是裸链接", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    const imported = { import_id: "imp-1", valid: 1, invalid: 1, duplicate: 0, blacklisted: 0, invalid_download_url: "/api/v1/web/messages/import/imp-1/invalid-file", expires_at: "2026-07-13T08:00:00+08:00", status: "ready" as const, error: null }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (url === "/api/v1/web/templates") return { ok: true, status: 200, headers: { get: (_name: string): string | null => null }, json: async () => [] }
      if (url === imported.invalid_download_url) return { ok: true, status: 200, headers: { get: (_name: string): string | null => null }, blob: async () => new Blob(["masked"]) }
      expect(init?.method).toBe("POST")
      return { ok: true, status: 200, headers: { get: (_name: string): string | null => null }, json: async () => imported }
    })
    vi.stubGlobal("fetch", fetch)
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:masked"),
        revokeObjectURL: vi.fn(),
      }),
    )
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    ;(wrapper.vm as unknown as { form: { source: string } }).form.source = "import"
    await wrapper.vm.$nextTick()
    const upload = wrapper.findComponent({ name: "ElUpload" })
    await (upload.props("httpRequest") as (options: object) => Promise<void>)({ file: new File(["phone"], "phones.csv"), onSuccess: vi.fn() })
    await flushPromises()

    const download = wrapper.get("[data-testid='download-invalid']")
    expect(download.element.tagName).toBe("BUTTON")
    await download.trigger("click")
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      imported.invalid_download_url,
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.any(String) }) }),
    )
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
})
