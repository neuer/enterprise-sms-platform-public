import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessageBox, ElPagination } from "element-plus"
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
  total: 2,
  page: 1,
  size: 20,
  metric: "total",
  dimension_total: 1,
  trend: {
    periods: ["2026-07-11", "2026-07-12"],
    series: [{ dim_value: "7", dim_label: "OA应用", is_other: false, total: [5, 10], total_segments: [6, 13] }],
  },
  granularity: "day",
  group_by: "app",
  category: "all",
  start: "2026-07-01",
  end: "2026-07-12",
  can_export_decrypted: false,
  summary: { total: 15, total_segments: 19, delivered: 11, failed: 3, unknown: 1, success_rate: 11 / 14 },
  dim_summary: [
    {
      dim_value: "7",
      dim_label: "OA应用",
      total: 15,
      total_segments: 19,
      delivered: 11,
      failed: 3,
      unknown: 1,
      success_rate: 11 / 14,
    },
  ],
  items: [
    {
      period_start: "2026-07-11",
      dim_value: "7",
      dim_label: "OA应用",
      total: 5,
      total_segments: 6,
      delivered: 4,
      failed: 1,
      unknown: 0,
      success_rate: 0.8,
    },
    {
      period_start: "2026-07-12",
      dim_value: "7",
      dim_label: "OA应用",
      total: 10,
      total_segments: 13,
      delivered: 7,
      failed: 2,
      unknown: 1,
      success_rate: 7 / 9,
    },
  ],
}
const publicId = "c0a80101-0000-4000-8000-000000000134"

describe("统计报表页", () => {
  it("明细分页与排序请求服务端，全范围摘要和趋势保持完整，改指标重新请求Top维度", async () => {
    const first = { ...report.items[0], dim_label: "第一页应用" }
    const second = { ...report.items[1], dim_label: "第二页应用" }
    const fetch = vi.fn(async (url: string) => {
      const query = new URL(url, "http://localhost").searchParams
      const page = Number(query.get("page"))
      return new Response(
        JSON.stringify({
          ...report,
          total: 41,
          page,
          size: 20,
          dimension_total: 20,
          metric: query.get("metric"),
          items: [page === 1 ? first : second],
        }),
      )
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain("共 41 行")
    expect(wrapper.text()).toContain("共 20 个应用")
    expect(wrapper.text()).toContain("第一页应用")
    expect(wrapper.findComponent(ReportTrendChart).props("trend")).toEqual(report.trend)
    expect(wrapper.findComponent(ReportTrendChart).props()).not.toHaveProperty("items")

    wrapper.findComponent(ElPagination).vm.$emit("current-change", 2)
    await flushPromises()
    expect(new URL(fetch.mock.calls.at(-1)![0], "http://localhost").searchParams.get("page")).toBe("2")
    expect(wrapper.text()).toContain("第二页应用")
    expect(wrapper.text()).not.toContain("第一页应用")
    expect(wrapper.findComponent(ReportTrendChart).props("trend")).toEqual(report.trend)
    expect(wrapper.text()).toContain("78.6%")

    // 编辑但尚未提交的过滤条件不得被翻页或排序偷偷应用。
    await wrapper.get("[data-testid='report-group-dept']").trigger("click")
    wrapper.findComponent({ name: "ElTable" }).vm.$emit("sort-change", { prop: "total", order: "ascending" })
    await flushPromises()
    const sorted = new URL(fetch.mock.calls.at(-1)![0], "http://localhost").searchParams
    expect(sorted.get("sort")).toBe("total")
    expect(sorted.get("order")).toBe("asc")
    expect(sorted.get("page")).toBe("1")
    expect(sorted.get("group_by")).toBe("app")
    await wrapper.get('[aria-label="趋势指标"] button:last-child').trigger("click")
    await flushPromises()
    const metricQuery = new URL(fetch.mock.calls.at(-1)![0], "http://localhost").searchParams
    expect(metricQuery.get("metric")).toBe("total_segments")
    expect(wrapper.findComponent(ReportTrendChart).props("metric")).toBe("total_segments")
    expect(wrapper.get(".rank-num b").text()).toBe("19")
    expect(wrapper.get(".rank-num small").text()).toContain("消息数 15")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
  it("按计费条排行时条宽、主数字和占比使用计费条口径", async () => {
    const fetch = vi.fn(async () =>
      response({
        ...report,
        metric: "total_segments",
        summary: { ...report.summary, total: 100, total_segments: 200 },
        dim_summary: [
          { ...report.dim_summary[0], total: 80, total_segments: 150 },
          { ...report.dim_summary[0], dim_value: "8", dim_label: "另一应用", total: 20, total_segments: 50 },
        ],
      }),
    )
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(ReportView, { global: { plugins: [createPinia(), ElementPlus] } })
    try {
      await flushPromises()
      const ranks = wrapper.findAll(".rank-list li")
      expect(ranks[0].get(".rank-num b").text()).toBe("150")
      expect(ranks[0].get(".rank-num small").text()).toContain("75.0% · 消息数 80")
      expect(ranks[1].get(".rank-num b").text()).toBe("50")
      expect(ranks[1].get(".rank-num small").text()).toContain("25.0% · 消息数 20")
      expect(Number.parseFloat((ranks[1].get(".rank-track i").element as HTMLElement).style.width)).toBeCloseTo(100 / 3)
    } finally {
      wrapper.unmount()
      vi.unstubAllGlobals()
    }
  })
  it("展示服务端摘要、维度排行和异步明细导出", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(report))
      .mockResolvedValueOnce(
        response(
          {
            id: publicId,
            status: "pending",
            decrypted: false,
            row_count: null,
            download_url: null,
            expires_at: null,
            created_at: "2026-07-12T08:00:00+08:00",
          },
          202,
        ),
      )
      .mockResolvedValueOnce(
        response({
          id: publicId,
          status: "done",
          decrypted: false,
          row_count: 15,
          download_url: `/api/v1/web/reports/export/${publicId}/download`,
          expires_at: "2026-07-19T08:00:00+08:00",
          created_at: "2026-07-12T08:00:00+08:00",
        }),
      )
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
    expect(wrapper.findAll(".report-filter-bar .filter-seg")).toHaveLength(2)
    expect(wrapper.findAllComponents({ name: "ElSegmented" })).toHaveLength(0)
    expect(wrapper.text()).toContain("统计报表")
    expect(wrapper.text()).toContain("当前口径")
    expect(wrapper.text()).toContain("周期")
    expect(wrapper.text()).toContain("维度")
    expect(wrapper.text()).toContain("15")
    expect(wrapper.text()).toContain("19")
    expect(wrapper.text()).toContain("78.6%")
    expect(wrapper.text()).toContain("12 天")
    expect(wrapper.text()).toContain("未知不入分母")
    expect(wrapper.text()).toContain("结果构成")
    expect(wrapper.text()).toContain("维度排行")
    expect(wrapper.text()).toContain("OA应用")
    expect(wrapper.text()).toContain("失败")
    expect(wrapper.text()).toContain("未知")
    expect(wrapper.text()).not.toContain("周期起始")
    expect(wrapper.text()).not.toContain("含明文手机号")
    expect(wrapper.text()).not.toContain("app/")
    expect(wrapper.text()).not.toContain("长短信主要来自营销类模板")
    expect(wrapper.find(".trend-legend").text()).toContain("OA应用")

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
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response(decryptedReport))
      .mockResolvedValueOnce(
        response(
          {
            id: publicId,
            status: "pending",
            decrypted: true,
            row_count: null,
            download_url: null,
            expires_at: null,
            created_at: "2026-07-12T08:00:00+08:00",
          },
          202,
        ),
      )
      .mockResolvedValueOnce(
        response({
          id: publicId,
          status: "done",
          decrypted: true,
          row_count: 15,
          download_url: `/api/v1/web/reports/export/${publicId}/download`,
          expires_at: "2026-07-19T08:00:00+08:00",
          created_at: "2026-07-12T08:00:00+08:00",
        }),
      )
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
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("导出明细 CSV"))!
      .trigger("click")
    await flushPromises()
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("下载 CSV"))!
      .trigger("click")
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
    const pending = {
      id: publicId,
      status: "pending",
      decrypted: false,
      row_count: null,
      download_url: null,
      expires_at: null,
      created_at: "2026-07-12T08:00:00+08:00",
    }
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

  it("导出任务超过约 5 分钟未完成时停止轮询并提示超时", async () => {
    vi.useFakeTimers()
    const pending = {
      id: publicId,
      status: "pending",
      decrypted: false,
      row_count: null,
      download_url: null,
      expires_at: null,
      created_at: "2026-07-12T08:00:00+08:00",
    }
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
    expect(statusCalls()).toBe(1)

    // 150 次 × 2s ≈ 5 分钟兜底：到达上限后停止轮询并给出中文超时提示
    await vi.advanceTimersByTimeAsync(298_000)
    expect(statusCalls()).toBe(150)
    expect(wrapper.text()).toContain("导出结果等待超时（已超过 5 分钟），请稍后重新发起导出")

    await vi.advanceTimersByTimeAsync(30_000)
    expect(statusCalls()).toBe(150)
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

    await wrapper.get('[data-testid="report-group-dept"]').trigger("click")
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
    const wrapper = mount(ReportTrendChart, { props: { trend: report.trend, metric: "total" } })
    await flushPromises()
    let option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.xAxis.data).toEqual(["2026-07-11", "2026-07-12"])
    expect(option.series).toHaveLength(1)
    expect(option.series[0].name).toBe("OA应用")
    expect(option.series[0].data).toEqual([5, 10])
    expect(option.series[0].stack).toBe("total")
    expect(option.legend).toBeUndefined()

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
        trend: { periods: ["2026-07-12"], series: [{ ...report.trend.series[0], total: [10], total_segments: [13] }] },
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

  it("直接消费服务端 Top 5 与其他，不在当前页重算或遗漏其他", async () => {
    chart.setOption.mockClear()
    const trend = {
      periods: ["2026-07-12"],
      series: [70, 60, 50, 40, 30, 30].map((total, index) => ({
        dim_value: String(index),
        dim_label: index === 5 ? "其他" : `应用${index + 1}`,
        total: [total],
        total_segments: [total],
        is_other: index === 5,
      })),
    }
    const wrapper = mount(ReportTrendChart, { props: { trend, metric: "total" } })
    await flushPromises()
    const option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.series).toHaveLength(6)
    expect(option.series.map((serie: { name: string }) => serie.name)).toEqual([
      "应用1",
      "应用2",
      "应用3",
      "应用4",
      "应用5",
      "其他",
    ])
    expect(option.series[5].data).toEqual([30])
    wrapper.unmount()
  })
})
