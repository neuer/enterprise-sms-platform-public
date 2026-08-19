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
    // 设计规范：末段斜纹填充，恒显 1 个灰色 ghost 块提示下一段边界
    expect(wrapper.findAll(".segment-fill.partial")).toHaveLength(1)
    expect(wrapper.findAll(".segment-ghost")).toHaveLength(1)
  })

  it("粘贴号码格式无效时即时提示并禁止提交", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200, headers: { get: () => null }, json: async () => [] }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    const vm = wrapper.vm as unknown as { form: { mobilesText: string; content: string } }
    vm.form.content = "维护通知"
    // 10 位号码不满足 ^1\d{10}$，服务端必然 400；客户端必须先行拦截并说明原因。
    vm.form.mobilesText = "13800138000\n1376660000"
    await wrapper.vm.$nextTick()

    const hint = wrapper.get("[data-testid='invalid-mobiles-hint']")
    expect(hint.text()).toContain("1 个号码格式无效")
    expect(hint.text()).toContain("1376660000")
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeDefined()

    vm.form.mobilesText = "13800138000\n13766600001"
    await wrapper.vm.$nextTick()
    expect(wrapper.find("[data-testid='invalid-mobiles-hint']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeUndefined()
    vi.unstubAllGlobals()
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

  it("网络重试复用同一幂等键，修改内容后轮换新键", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    const sentBodies: Array<{ biz_id: string }> = []
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const target = String(url)
      if (target.endsWith("/templates") || target.endsWith("/reports/dashboard")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => null },
          json: async () => (target.endsWith("/reports/dashboard")
            ? { ui_policy: { test_send_max: 5 } }
            : []),
        }
      }
      if (target.endsWith("/messages/send")) {
        sentBodies.push(JSON.parse(String(init?.body)) as { biz_id: string })
        if (sentBodies.length === 1) {
          return {
            ok: false,
            status: 500,
            headers: { get: () => null },
            json: async () => ({ code: "INTERNAL_ERROR", message: "网络丢失", detail: null }),
          }
        }
        return {
          ok: true,
          status: 200,
          headers: { get: () => null },
          json: async () => ({
            batch_no: "b1",
            status: "queued",
            accepted: 1,
            quota_cost: 1,
            idempotent: false,
            deferred_reason: null,
          }),
        }
      }
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) }
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const vm = wrapper.vm as unknown as {
      form: { category: string; mobilesText: string; content: string }
    }
    vm.form.category = "notice"
    vm.form.mobilesText = "13800138000"
    vm.form.content = "维护通知"
    await wrapper.vm.$nextTick()

    await wrapper.get("[data-testid='send-button']").trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='send-button']").trigger("click")
    await flushPromises()

    expect(sentBodies).toHaveLength(2)
    expect(sentBodies[0].biz_id).toBe(sentBodies[1].biz_id)
    // 契约 biz_id 最长 32 字符：UUID 必须去掉连字符，否则服务端 400 INVALID_PARAM。
    expect(sentBodies[0].biz_id).toMatch(/^[0-9a-f]{32}$/)

    vm.form.content = "维护通知（改期）"
    await wrapper.vm.$nextTick()
    await wrapper.get("[data-testid='send-button']").trigger("click")
    await flushPromises()

    expect(sentBodies).toHaveLength(3)
    expect(sentBodies[2].biz_id).not.toBe(sentBodies[0].biz_id)
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it.each([
    {
      name: "普通受理",
      result: {
        batch_no: "b-new",
        status: "queued" as const,
        accepted: 1,
        quota_cost: 1,
        idempotent: false,
        deferred_reason: null,
      },
      expected: "批次 b-new 已受理，状态：排队中",
      unexpected: ["queued", "幂等命中", "窗外转定时"],
    },
    {
      name: "幂等命中",
      result: {
        batch_no: "b-hit",
        status: "queued" as const,
        accepted: 1,
        quota_cost: 1,
        idempotent: true,
        deferred_reason: null,
      },
      expected: "本次为幂等命中，返回历史批次，未重复发送",
      unexpected: ["queued"],
    },
    {
      name: "窗外转定时",
      result: {
        batch_no: "b-defer",
        status: "scheduled" as const,
        accepted: 1,
        quota_cost: 1,
        idempotent: false,
        deferred_reason: "market_window",
      },
      expected: "超出营销发送时间窗，已转为定时发送",
      unexpected: ["scheduled", "market_window"],
    },
  ])("成功提示：$name 使用中文并区分幂等与转定时", async ({ result, expected, unexpected }) => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const target = String(url)
      if (target.endsWith("/templates") || target.endsWith("/reports/dashboard")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => null },
          json: async () => (target.endsWith("/reports/dashboard") ? { ui_policy: { test_send_max: 5 } } : []),
        }
      }
      if (target.endsWith("/messages/send")) {
        return {
          ok: true,
          status: 200,
          headers: { get: () => null },
          json: async () => result,
        }
      }
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) }
    }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { category: string; mobilesText: string; content: string }
    }
    vm.form.category = "notice"
    vm.form.mobilesText = "13800138000"
    vm.form.content = "维护通知"
    await wrapper.vm.$nextTick()
    await wrapper.get("[data-testid='send-button']").trigger("click")
    await flushPromises()

    expect(wrapper.text()).toContain(expected)
    expect(wrapper.text()).toContain(`批次 ${result.batch_no} 已受理`)
    for (const phrase of unexpected) {
      expect(wrapper.text()).not.toContain(phrase)
    }
    wrapper.unmount()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  const basePreview = {
    final_length: 12,
    est_segments: 1,
    quota_cost: 2,
    segment_parts: [{ used: 12, capacity: 70, partial: true }],
    next_segment_at: 59,
    approval_required: false,
    unsubscribe_appended: false,
    final_content: "【平台】维护通知",
    deferred_reason: null,
    quota: { used: 3412, limit: 20000, remaining: 16588 } as { used: number; limit: number; remaining: number | null } | null,
  }

  function stubPreviewFetch(previewBody: unknown) {
    return vi.fn(async (url: string) => {
      const target = String(url)
      if (target.endsWith("/billing/preview")) {
        return { ok: true, status: 200, headers: { get: () => null }, json: async () => previewBody }
      }
      if (target.endsWith("/reports/dashboard")) {
        return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ui_policy: { test_send_max: 5 } }) }
      }
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => [] }
    })
  }

  it("自动预检展示最终内容、计费算式与配额摘要", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.useFakeTimers()
    vi.stubGlobal("fetch", stubPreviewFetch(basePreview))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { mobilesText: string; content: string; signName: string } }
    vm.form.mobilesText = "13800138000\n13800138001"
    vm.form.content = "维护通知"
    vm.form.signName = "平台"
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()

    expect(wrapper.get("[data-testid='final-content']").text()).toBe("【平台】维护通知")
    expect(wrapper.text()).toContain("2 × 1 段 =")
    expect(wrapper.text()).toContain("3,412 / 20,000")
    expect(wrapper.get("[data-testid='send-button']").text()).toContain("立即发送")
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it("配额投影不可用时降级展示而不阻断预检", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.useFakeTimers()
    vi.stubGlobal("fetch", stubPreviewFetch({ ...basePreview, quota: null }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { mobilesText: string; content: string } }
    vm.form.mobilesText = "13800138000"
    vm.form.content = "维护通知"
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()

    expect(wrapper.text()).toContain("配额投影暂不可确认")
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeUndefined()
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it("文件导入展示解析状态并回填四类计数", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.useFakeTimers()
    const readyBody = {
      import_id: "imp-9",
      valid: 3,
      invalid: 1,
      duplicate: 1,
      blacklisted: 0,
      invalid_download_url: null,
      expires_at: "2026-08-20T09:41:00+08:00",
      status: "ready",
      error: null,
    }
    const fetch = vi.fn(async (url: string, init?: RequestInit) => {
      const target = String(url)
      if (target.endsWith("/messages/import") && init?.method === "POST") {
        return {
          ok: true,
          status: 202,
          headers: { get: () => null },
          json: async () => ({ ...readyBody, valid: 0, status: "pending" }),
        }
      }
      if (target.endsWith("/messages/import/imp-9")) {
        return { ok: true, status: 200, headers: { get: () => null }, json: async () => readyBody }
      }
      if (target.endsWith("/reports/dashboard")) {
        return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ui_policy: { test_send_max: 5 } }) }
      }
      return { ok: true, status: 200, headers: { get: () => null }, json: async () => [] }
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    ;(wrapper.vm as unknown as { form: { source: string } }).form.source = "import"
    await wrapper.vm.$nextTick()
    const upload = wrapper.findComponent({ name: "ElUpload" })
    const request = upload.props("httpRequest") as (options: object) => Promise<void>
    const pending = request({ file: new File(["phone"], "phones.csv"), onSuccess: vi.fn() })
    await wrapper.vm.$nextTick()
    expect(wrapper.get("[data-testid='import-parsing']").text()).toContain("phones.csv")
    await vi.advanceTimersByTimeAsync(300)
    await pending
    await flushPromises()

    expect(wrapper.text()).toContain("3 有效")
    expect(wrapper.text()).toContain("1 无效")
    expect(wrapper.text()).toContain("有效期至")
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it("营销窗内显示提示行且达审批阈值时主按钮呈审批语义", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.useFakeTimers()
    vi.stubGlobal("fetch", stubPreviewFetch({ ...basePreview, approval_required: true }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { category: string; mobilesText: string; content: string; consentConfirmed: boolean }
    }
    vm.form.category = "market"
    vm.form.mobilesText = "13800138000\n13800138001"
    vm.form.content = "维护通知"
    vm.form.consentConfirmed = true
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()

    expect(wrapper.text()).toContain("当前处于营销发送时间窗内")
    const button = wrapper.get("[data-testid='send-button']")
    expect(button.classes()).toContain("approval")
    expect(button.text()).toContain("提交审批")
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it("粘贴预检按去重后口径预估计费", async () => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.useFakeTimers()
    const bodies: Array<{ accepted_count: number }> = []
    const base = stubPreviewFetch(basePreview)
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("/billing/preview")) {
        bodies.push(JSON.parse(String(init?.body)) as { accepted_count: number })
      }
      return base(url)
    }))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as { form: { mobilesText: string; content: string } }
    vm.form.mobilesText = "13800138000\n13800138001\n13800138000"
    vm.form.content = "维护通知"
    await wrapper.vm.$nextTick()
    await vi.advanceTimersByTimeAsync(650)
    await flushPromises()

    expect(bodies.at(-1)?.accepted_count).toBe(2)
    expect(wrapper.text()).toContain("重复 1 个，提交时由服务端剔除")
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
    sessionStorage.removeItem("sms_token")
  })

  it("测试发送超出号码上限即时提示并禁止提交", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => ({
      ok: true,
      status: 200,
      headers: { get: () => null },
      json: async () => String(url).endsWith("/reports/dashboard")
        ? { ui_policy: { test_send_max: 1 } }
        : [],
    })))
    const wrapper = mount(SendView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    const vm = wrapper.vm as unknown as {
      form: { mobilesText: string; content: string; isTest: boolean; scheduledAt: string }
    }
    vm.form.mobilesText = "13800138000\n13800138001"
    vm.form.content = "维护通知"
    vm.form.scheduledAt = "2026-08-20T09:00:00+08:00"
    vm.form.isTest = true
    await wrapper.vm.$nextTick()

    expect(wrapper.get("[data-testid='test-limit-hint']").text()).toContain("最多 1 个号码")
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeDefined()
    // 测试发送与定时互斥：勾选即清除已选时间
    expect(vm.form.scheduledAt).toBe("")

    vm.form.isTest = false
    await wrapper.vm.$nextTick()
    expect(wrapper.find("[data-testid='test-limit-hint']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='send-button']").attributes("disabled")).toBeUndefined()
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})
