import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

import OpsView from "../src/views/OpsView.vue"

const publicId = "c0a80101-0000-4000-8000-000000000134"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => name === "content-length" && body === undefined ? "0" : null },
    json: async () => body,
    blob: async () => new Blob(["masked-export"]),
  }
}

function result(url: string, method: string): unknown {
  if (method === "POST") {
    if (url.includes("/replay")) return { processed_items: 3 }
    if (url.includes("/unmatched-reports/export")) {
      return { id: publicId, status: "pending", decrypted: false, row_count: null, download_url: null, expires_at: null, created_at: "2026-07-12T08:00:00+08:00" }
    }
    if (url.includes("/queue/resume")) return { resumed_batches: 2, paused_codes: ["999"] }
    return undefined
  }
  if (url.includes(`/reports/export/${publicId}`) && !url.endsWith("/download")) return { id: publicId, status: "done", decrypted: false, row_count: 45, download_url: `/api/v1/web/reports/export/${publicId}/download`, expires_at: "2026-07-19T08:00:00+08:00", created_at: "2026-07-12T08:00:00+08:00" }
  if (url.includes("/alerts")) return { items: [{ id: 1, alert_type: "job_failed", level: "crit", title: "任务连续失败", detail: { job_name: "poll_report" }, channels: "log-sink", created_at: "2026-07-12T08:00:00+08:00" }], total: 45, page: 1, page_size: 20 }
  if (url.includes("/raw-logs")) return { items: [{ id: 2, source: "report", item_count: 3, custom_id_count: 2, processed: false, error: "ValueError", fetched_at: "2026-07-12T08:00:00+08:00" }], total: 45, page: 1, page_size: 20 }
  if (url.includes("/chunks/uncertain")) return { items: [{ chunk_id: 3, batch_no: "BATCH-1", custom_id: "CUSTOM-1", phone_count: 50, vendor_code: null, uncertain_since: "2026-07-12T08:00:00+08:00", age_seconds: 90000 }], total: 45, page: 1, page_size: 20 }
  if (url.includes("/unmatched-reports")) return { items: [{ id: 4, vendor_task_id: "vendor-1", custom_id: "legacy-1", phone_mask: "138****8000", report_status: 1, report_desc: "DELIVRD", report_time: "2026-07-12T08:00:00+08:00", created_at: "2026-07-12T08:00:00+08:00" }], total: 45, page: 1, page_size: 20 }
  if (url.includes("/jobs")) return [{ job_name: "poll_report", last_run_at: "2026-07-12T08:00:00+08:00", last_status: "failed", last_duration_ms: 120, last_items: 0, success_rate_24h: 0.75, stalled: true }]
  if (url.includes("/queue/status")) return { realtime_code: "999", bulk_code: "999", balance: 20000, threshold: 10000 }
  return { total: 0, items: [] }
}

function tab(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll(".el-tabs__item").find((item) => item.text().includes(label))!
}

describe("统一运维中心", () => {
  it("按 Tab 懒加载并执行确认式 raw 重放和任务触发", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(OpsView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("任务连续失败")
    expect(wrapper.find("[data-testid='ops-alert-pagination']").exists()).toBe(true)
    expect(fetch.mock.calls.filter(([url]) => String(url).includes("/alerts"))).toHaveLength(1)
    expect(fetch.mock.calls.some(([url]) => String(url).includes("/raw-logs"))).toBe(false)

    await tab(wrapper, "原始报文").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("ValueError")
    expect(wrapper.text()).not.toContain("payload_enc")
    await wrapper.findAll("button").find((item) => item.text().includes("重放"))!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.some(([url, init]) => String(url).endsWith("/raw-logs/2/replay") && init?.method === "POST")).toBe(true)

    await tab(wrapper, "任务健康").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("poll_report")
    expect(wrapper.text()).toContain("轮询厂商状态报告，保存原始报文后解析并更新发送结果")
    await wrapper.findAll("button").find((item) => item.text().includes("手动触发"))!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.some(([url, init]) => String(url).includes("/jobs/poll_report/trigger") && init?.method === "POST")).toBe(true)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("展示 unmatched 掩码并支持密文导出与 break-glass 恢复", async () => {
    sessionStorage.setItem("sms_token", "jwt-ops")
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = mount(OpsView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    await tab(wrapper, "unmatched").trigger("click")
    await flushPromises()
    expect(wrapper.get(".ops-filter-title > div:last-child").classes()).toContain("filter-toolbar")
    expect(wrapper.text()).toContain("138****8000")
    await wrapper.findAll("button").find((item) => item.text().includes("导出对账"))!.trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain(`导出任务 #${publicId}`)
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith(`/reports/export/${publicId}`))).toBe(true)
    expect(wrapper.text()).toContain("45 行")
    expect(wrapper.get("[data-testid='download-unmatched-export']").text()).toContain("下载 CSV")

    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:masked-export"), revokeObjectURL: vi.fn() })
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)
    await wrapper.get("[data-testid='download-unmatched-export']").trigger("click")
    await flushPromises()
    expect(fetch).toHaveBeenCalledWith(
      `/api/v1/web/reports/export/${publicId}/download`,
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: expect.any(String) }) }),
    )

    await tab(wrapper, "队列恢复").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("余额 20,000")
    const force = wrapper.get("[data-testid='force-resume'] input")
    await force.setValue(true)
    await wrapper.findAll("button").find((item) => item.text().includes("恢复队列"))!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls.some(([url, init]) => String(url).includes("/queue/resume?force=true") && init?.method === "POST")).toBe(true)

    wrapper.unmount()
    sessionStorage.removeItem("sms_token")
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
})
