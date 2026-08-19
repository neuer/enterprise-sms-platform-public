import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox } from "element-plus"
import { createPinia } from "pinia"
import { vi } from "vitest"

const chart = vi.hoisted(() => ({ setOption: vi.fn(), resize: vi.fn(), dispose: vi.fn() }))
vi.mock("echarts/core", () => ({ init: vi.fn(() => chart), use: vi.fn() }))
vi.mock("echarts/charts", () => ({ BarChart: {} }))
vi.mock("echarts/components", () => ({ GridComponent: {}, LegendComponent: {}, TooltipComponent: {} }))
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }))

import ReportTrendChart from "../src/components/ReportTrendChart.vue"
import ReportView from "../src/views/ReportView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
    blob: async () => new Blob(["csv"]),
  }
}

const report = {
  granularity: "day",
  group_by: "app",
  category: "all",
  start: "2026-07-01",
  end: "2026-07-12",
  can_export_decrypted: false,
  summary: { total: 15, total_segments: 19, delivered: 11, failed: 3, unknown: 1, success_rate: 11 / 14 },
  dim_summary: [
    { dim_value: "7", dim_label: "OA应用", total: 15, total_segments: 19, delivered: 11, failed: 3, unknown: 1, success_rate: 11 / 14 },
  ],
  items: [
    { period_start: "2026-07-11", dim_value: "7", dim_label: "OA应用", total: 5, total_segments: 6, delivered: 4, failed: 1, unknown: 0, success_rate: 0.8 },
    { period_start: "2026-07-12", dim_value: "7", dim_label: "OA应用", total: 10, total_segments: 13, delivered: 7, failed: 2, unknown: 1, success_rate: 7 / 9 },
  ],
}
const publicId = "c0a80101-0000-4000-8000-000000000134"

describe("统计报表页", () => {
  it("展示服务端摘要、维度排行和异步明细导出", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(report))
      .mockResolvedValueOnce(response({ id: publicId, status: "pending", decrypted: false, row_count: null, download_url: null, expires_at: null, created_at: "2026-07-12T08:00:00+08:00" }, 202))
      .mockResolvedValueOnce(response({ id: publicId, status: "done", decrypted: false, row_count: 15, download_url: `/api/v1/web/reports/export/${publicId}/download`, expires_at: "2026-07-19T08:00:00+08:00", created_at: "2026-07-12T08:00:00+08:00" }))
      .mockResolvedValueOnce(response(null))
    vi.stubGlobal("fetch", fetch)
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:test"),
        revokeObjectURL: vi.fn(),
      }),
    )
    const click = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)

    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    // 周期 / 维度 / 趋势指标切换共三组分段控件
    expect(wrapper.findAllComponents({ name: "ElSegmented" })).toHaveLength(3)
    expect(wrapper.text()).toContain("统计报表")
    expect(wrapper.text()).toContain("当前口径")
    expect(wrapper.text()).toContain("15")
    expect(wrapper.text()).toContain("19")
    expect(wrapper.text()).toContain("78.6%")
    expect(wrapper.text()).toContain("结果构成")
    expect(wrapper.text()).toContain("维度排行")
    expect(wrapper.text()).toContain("OA应用")
    expect(wrapper.text()).not.toContain("含明文手机号")

    const exportButton = wrapper.findAll("button").find((item) => item.text().includes("导出明细 CSV"))
    await exportButton!.trigger("click")
    await flushPromises()
    const strip = wrapper.find('[data-testid="export-strip"]')
    expect(strip.exists()).toBe(true)
    expect(strip.text()).toContain("15 行")
    expect(strip.text()).toContain("掩码导出")
    const downloadButton = wrapper.findAll("button").find((item) => item.text().includes("下载 CSV"))
    await downloadButton!.trigger("click")
    await flushPromises()
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/reports/export")
    expect(fetch.mock.calls[3][0]).toBe(`/api/v1/web/reports/export/${publicId}/download`)
    expect(click).toHaveBeenCalled()

    wrapper.unmount()
    click.mockRestore()
    vi.unstubAllGlobals()
  })

  it("明文下载重新认证并把单次令牌只放在下载请求头", async () => {
    const decryptedReport = { ...report, can_export_decrypted: true }
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(decryptedReport))
      .mockResolvedValueOnce(response({ id: publicId, status: "pending", decrypted: true, row_count: null, download_url: null, expires_at: null, created_at: "2026-07-12T08:00:00+08:00" }, 202))
      .mockResolvedValueOnce(response({ id: publicId, status: "done", decrypted: true, row_count: 15, download_url: `/api/v1/web/reports/export/${publicId}/download`, expires_at: "2026-07-19T08:00:00+08:00", created_at: "2026-07-12T08:00:00+08:00" }))
      .mockResolvedValueOnce(response({ token: "single-use-token", expires_in: 300 }))
      .mockResolvedValueOnce(response(null))
    vi.stubGlobal("fetch", fetch)
    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:test"),
        revokeObjectURL: vi.fn(),
      }),
    )
    vi.spyOn(ElMessageBox, "prompt").mockResolvedValue({ value: "current-password", action: "confirm" } as never)
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined)

    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    await wrapper.get(".el-checkbox input").setValue(true)
    await wrapper.findAll("button").find((item) => item.text().includes("导出明细 CSV"))!.trigger("click")
    await flushPromises()
    await wrapper.findAll("button").find((item) => item.text().includes("下载 CSV"))!.trigger("click")
    await flushPromises()

    expect(fetch.mock.calls[3][0]).toBe(`/api/v1/web/reports/export/${publicId}/step-up`)
    expect(JSON.parse(fetch.mock.calls[3][1].body)).toEqual({ password: "current-password" })
    expect(fetch.mock.calls[4][0]).toBe(`/api/v1/web/reports/export/${publicId}/download`)
    expect(fetch.mock.calls[4][1].headers).toMatchObject({ "X-Export-Step-Up": "single-use-token" })
    expect(String(fetch.mock.calls[4][1].headers)).not.toContain("current-password")

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("重复点击导出只保留一条轮询链", async () => {
    vi.useFakeTimers()
    const pending = { id: publicId, status: "pending", decrypted: false, row_count: null, download_url: null, expires_at: null, created_at: "2026-07-12T08:00:00+08:00" }
    const fetch = vi.fn((url: string, init?: RequestInit) => {
      if (String(url) === "/api/v1/web/reports/export" && init?.method === "POST") {
        return Promise.resolve(response(pending, 202))
      }
      if (String(url) === `/api/v1/web/reports/export/${publicId}`) {
        return Promise.resolve(response(pending))
      }
      return Promise.resolve(response(report))
    })
    vi.stubGlobal("fetch", fetch)

    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()

    const statusCalls = () =>
      fetch.mock.calls.filter(([url]) => String(url) === `/api/v1/web/reports/export/${publicId}`).length
    const exportButton = wrapper.findAll("button").find((item) => item.text().includes("导出明细 CSV"))
    await exportButton!.trigger("click")
    await flushPromises()
    await exportButton!.trigger("click")
    await flushPromises()
    expect(statusCalls()).toBe(2)

    // 若旧定时器未清除，这里会出现两条并行轮询链，每 2s 各查一次。
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(statusCalls()).toBe(3)
    await vi.advanceTimersByTimeAsync(2000)
    await flushPromises()
    expect(statusCalls()).toBe(4)

    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it("修改筛选条件后提示已变更且不自动重查", async () => {
    const fetch = vi.fn().mockResolvedValue(response(report))
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).not.toContain("筛选条件已变更")

    const segmented = wrapper.findAllComponents({ name: "ElSegmented" })
    await segmented[1].vm.$emit("update:modelValue", "dept")
    await flushPromises()
    expect(wrapper.text()).toContain("筛选条件已变更")
    // 只有首次加载的一次请求，改条件不触发新查询
    expect(fetch.mock.calls.filter(([url]) => String(url).includes("/reports/stats"))).toHaveLength(1)
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("请求失败时显示可重试错误", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "报表暂不可用" }, 500)))
    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain("报表暂不可用")
    expect(wrapper.text()).toContain("重新查询")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})

describe("报表趋势图", () => {
  it("按维度堆叠并随指标切换改数", async () => {
    const wrapper = mount(ReportTrendChart, { props: { items: report.items, metric: "total" } })
    await flushPromises()
    let option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.xAxis.data).toEqual(["2026-07-11", "2026-07-12"])
    expect(option.series).toHaveLength(1)
    expect(option.series[0].name).toBe("OA应用")
    expect(option.series[0].data).toEqual([5, 10])
    expect(option.series[0].stack).toBe("total")

    await wrapper.setProps({ metric: "total_segments" })
    await flushPromises()
    option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.series[0].data).toEqual([6, 13])
    wrapper.unmount()
  })

  it("按日粒度把选定范围内的空日补零", async () => {
    chart.setOption.mockClear()
    const wrapper = mount(ReportTrendChart, {
      props: {
        items: [report.items[1]],
        metric: "total",
        start: "2026-07-10",
        end: "2026-07-12",
        granularity: "day",
      },
    })
    await flushPromises()
    const option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.xAxis.data).toEqual(["2026-07-10", "2026-07-11", "2026-07-12"])
    expect(option.series[0].data).toEqual([0, 0, 10])
    wrapper.unmount()
  })

  it("维度超过六个时 Top 5 之外归并为其他", async () => {
    chart.setOption.mockClear()
    const items = [70, 60, 50, 40, 30, 20, 10].map((total, index) => ({
      period_start: "2026-07-12",
      dim_value: String(index + 1),
      dim_label: `应用${index + 1}`,
      total,
      total_segments: total,
      delivered: total,
      failed: 0,
      unknown: 0,
      success_rate: 1,
    }))
    const wrapper = mount(ReportTrendChart, { props: { items, metric: "total" } })
    await flushPromises()
    const option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.series).toHaveLength(6)
    expect(option.series.map((serie: { name: string }) => serie.name)).toEqual([
      "应用1", "应用2", "应用3", "应用4", "应用5", "其他",
    ])
    expect(option.series[5].data).toEqual([30])
    wrapper.unmount()
  })
})
