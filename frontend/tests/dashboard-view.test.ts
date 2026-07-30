import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { vi } from "vitest"

const chart = vi.hoisted(() => ({
  setOption: vi.fn(),
  resize: vi.fn(),
  dispose: vi.fn(),
}))

vi.mock("echarts/core", () => ({ init: vi.fn(() => chart), use: vi.fn() }))
vi.mock("echarts/charts", () => ({ LineChart: {} }))
vi.mock("echarts/components", () => ({ GridComponent: {}, MarkLineComponent: {}, TooltipComponent: {} }))
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }))

import BalanceChart from "../src/components/BalanceChart.vue"
import DashboardView from "../src/views/DashboardView.vue"

function response(body: unknown, ok = true) {
  return {
    ok,
    status: ok ? 200 : 500,
    headers: { get: () => null },
    json: async () => body,
  }
}

const snapshot = {
  refreshed_at: "2026-07-12T08:00:00+08:00",
  categories: [
    { category: "verify", total: 3, total_segments: 3, delivered: 2, failed: 0, unknown: 1, success_rate: 1 },
    { category: "notice", total: 10, total_segments: 12, delivered: 8, failed: 2, unknown: 0, success_rate: 0.8 },
    { category: "market", total: 2, total_segments: 4, delivered: 1, failed: 1, unknown: 0, success_rate: 0.5 },
  ],
  overall_success_rate: 11 / 14,
  pending_approvals: 2,
  ui_policy: { test_send_max: 7 },
  operations: {
    current_balance: 9000,
    channel_monitor: {
      realtime_queue: 4,
      bulk_queue: 9,
      qps_used: 5,
      qps_rate: 8,
      reserved_realtime_qps: 3,
      stale: false,
    },
    balance_alert_threshold: 8800,
    balances: [
      { stat_date: "2026-07-11", balance: 9800 },
      { stat_date: "2026-07-12", balance: 9000 },
    ],
    alerts: [{ level: "warn", title: "余额较低", created_at: "2026-07-12T07:30:00+08:00" }],
    dispositions: { uncertain: 1, unmatched: 3, callback_dead: 4 },
    jobs: [
      { job_name: "poll_report", last_run_at: "2026-07-12T07:59:30+08:00", last_status: "success", stalled: false },
      { job_name: "aggregate_stats", last_run_at: null, last_status: null, stalled: true },
    ],
  },
}

describe("仪表盘", () => {
  it("展示服务端统计、处置计数和任务健康", async () => {
    const fetch = vi.fn().mockResolvedValue(response(snapshot))
    vi.stubGlobal("fetch", fetch)
    sessionStorage.setItem("sms_token", "jwt")

    const wrapper = mount(DashboardView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain("今日消息")
    expect(wrapper.text()).toContain("15")
    expect(wrapper.text()).toContain("19 计费条")
    expect(wrapper.text()).toContain("78.6%")
    expect(wrapper.text()).toContain("9,000")
    expect(wrapper.text()).toContain("告警阈值 8,800")
    expect(wrapper.text()).toContain("4")
    expect(wrapper.text()).toContain("5 / 8")
    expect(wrapper.text()).not.toContain("令牌容量未接入当前 API")
    const dashboardChartOption = chart.setOption.mock.calls.at(-1)?.[0]
    expect(dashboardChartOption.series[0].markLine.data[0].yAxis).toBe(8800)
    expect(wrapper.text()).toContain("余额较低")
    expect(wrapper.text()).toContain("uncertain")
    expect(wrapper.text()).toContain("aggregate_stats")
    expect(wrapper.findAll(".job-dot.danger")).toHaveLength(1)
    expect(fetch).toHaveBeenCalledWith(
      "/api/v1/web/reports/dashboard",
      expect.objectContaining({ method: "GET" }),
    )

    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("请求失败时提供可重试错误态", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({ message: "仪表盘暂不可用" }, false)))
    const wrapper = mount(DashboardView, { global: { plugins: [ElementPlus] } })
    await flushPromises()
    expect(wrapper.text()).toContain("仪表盘暂不可用")
    expect(wrapper.text()).toContain("重新加载")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("成功后轮询失败时保留最后值并把信道标记为陈旧", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(snapshot))
      .mockResolvedValueOnce(response({ message: "Redis 快照超时" }, false))
    vi.stubGlobal("fetch", fetch)
    const wrapper = mount(DashboardView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper.get(".dashboard-refresh .el-button").trigger("click")
    await flushPromises()

    expect(wrapper.get("[data-testid='channel-monitor']").classes()).toContain("monitor-stale")
    expect(wrapper.text()).toContain("数据暂不可用")
    expect(wrapper.text()).toContain("4")
    expect(wrapper.text()).toContain("Redis 快照超时")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("连接升级前后端时把新增运行字段降级为未知而不是崩溃", async () => {
    const { operations: _operations, ...viewerSnapshot } = snapshot
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(viewerSnapshot)))

    const wrapper = mount(DashboardView, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.find("[data-testid='channel-monitor']").exists()).toBe(false)
    expect(wrapper.text()).not.toContain("9,000")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })
})

describe("余额图表", () => {
  it("配置 14 日折线与余额阈值并在卸载时释放", async () => {
    const wrapper = mount(BalanceChart, {
      props: { points: snapshot.operations.balances, threshold: 10000 },
    })
    await flushPromises()
    expect(chart.setOption).toHaveBeenCalled()
    const option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.series[0].markLine.data[0].yAxis).toBe(10000)
    expect(option.series[0].data).toEqual([9800, 9000])
    wrapper.unmount()
    expect(chart.dispose).toHaveBeenCalled()
  })

  it("未提供动态阈值时不绘制固定告警线", async () => {
    chart.setOption.mockClear()
    const wrapper = mount(BalanceChart, { props: { points: snapshot.operations.balances } })
    await flushPromises()
    const option = chart.setOption.mock.calls.at(-1)?.[0]
    expect(option.series[0].markLine).toBeUndefined()
    wrapper.unmount()
  })
})
