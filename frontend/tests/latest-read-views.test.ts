import { flushPromises, mount, type VueWrapper } from "@vue/test-utils"
import ElementPlus, { ElMessage } from "element-plus"
import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import type { Component } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import ApprovalView from "../src/views/ApprovalView.vue"
import AuditView from "../src/views/AuditView.vue"
import BatchView from "../src/views/BatchView.vue"
import BlacklistView from "../src/views/BlacklistView.vue"
import CallbackView from "../src/views/CallbackView.vue"
import OpsView from "../src/views/OpsView.vue"
import ReportView from "../src/views/ReportView.vue"
import SensitiveWordView from "../src/views/SensitiveWordView.vue"

vi.mock("echarts/core", () => ({ init: vi.fn(), use: vi.fn() }))
vi.mock("echarts/charts", () => ({ BarChart: {} }))
vi.mock("echarts/components", () => ({ GridComponent: {}, TooltipComponent: {} }))
vi.mock("echarts/renderers", () => ({ CanvasRenderer: {} }))

interface ReadCase {
  name: string
  view: Component
  path: string
  request: string
  trigger: (wrapper: VueWrapper) => Promise<void>
  body: Record<string, unknown>
}

const submit = (selector: string) => (wrapper: VueWrapper) => wrapper.get(selector).trigger("submit")
const basicPage = { items: [], total: 123, page: 1, page_size: 20 }
const cases: ReadCase[] = [
  {
    name: "批次",
    view: BatchView,
    path: "/batches",
    request: "/web/batches?",
    trigger: submit("form.batch-filter-bar"),
    body: { ...basicPage, status_counts: {} },
  },
  {
    name: "报表",
    view: ReportView,
    path: "/reports",
    request: "/reports/stats?",
    trigger: submit("form.report-filter-bar"),
    body: {
      ...basicPage,
      size: 20,
      granularity: "day",
      group_by: "app",
      category: "all",
      metric: "total",
      start: "2026-01-01",
      end: "2026-01-01",
      can_export_decrypted: false,
      dimension_total: 0,
      summary: { total: 123, total_segments: 123, delivered: 123, failed: 0, unknown: 0, success_rate: 1 },
      dim_summary: [],
      trend: { periods: [], series: [] },
    },
  },
  {
    name: "审批",
    view: ApprovalView,
    path: "/approvals",
    request: "/web/approvals?",
    trigger: async (wrapper) => {
      const active = wrapper.get("[data-testid='approval-status-pending']").classes().includes("on")
      await wrapper.get(`[data-testid='approval-status-${active ? "approved" : "pending"}']`).trigger("click")
    },
    body: { ...basicPage, counts: { pending: 123, approved: 123, rejected: 0, expired: 0, pending_urgent: 0 } },
  },
  {
    name: "黑名单",
    view: BlacklistView,
    path: "/blacklist",
    request: "/admin/blacklist?",
    trigger: submit("form.blacklist-filter-bar"),
    body: basicPage,
  },
  {
    name: "敏感词",
    view: SensitiveWordView,
    path: "/sensitive-words",
    request: "/admin/sensitive-words?",
    trigger: submit("form.sensitive-filter-bar"),
    body: basicPage,
  },
  {
    name: "审计",
    view: AuditView,
    path: "/audit",
    request: "/admin/audit-logs?",
    trigger: submit("form.audit-filter-bar"),
    body: basicPage,
  },
  {
    name: "回调",
    view: CallbackView,
    path: "/callbacks",
    request: "/admin/callbacks?",
    trigger: submit("form.callback-filter-bar"),
    body: { ...basicPage, dead_total: 0 },
  },
  {
    name: "Ops只读POST",
    view: OpsView,
    path: "/ops?tab=unmatched",
    request: "/admin/unmatched-reports",
    trigger: submit("form.ops-filter-bar"),
    body: basicPage,
  },
]

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe("页面真实请求取消链", () => {
  it.each(cases)(
    "$name：替换及卸载取消慢读取，最新结果与真实错误仍可见",
    async ({ view, path, request, trigger, body }) => {
      const pending: Array<{ signal: AbortSignal; resolve: (value: Response) => void }> = []
      const errorToast = vi.spyOn(ElMessage, "error")
      vi.stubGlobal(
        "fetch",
        vi.fn((url: string, init: RequestInit = {}) => {
          if (!url.includes(request)) {
            const data = url.endsWith("/admin/configs") ? [{ key: "sensitive_hit_action", value: "block" }] : []
            return Promise.resolve(new Response(JSON.stringify(data)))
          }
          return new Promise<Response>((resolve, reject) => {
            const signal = init.signal as AbortSignal
            pending.push({ signal, resolve })
            signal.addEventListener("abort", () => reject(signal.reason), { once: true })
          })
        }),
      )
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: "/:pathMatch(.*)*", component: { template: "<div />" } }],
      })
      await router.push(path)
      await router.isReady()
      const wrapper = mount(view, { global: { plugins: [createPinia(), ElementPlus, router] } })
      await flushPromises()
      expect(pending).toHaveLength(1)

      await trigger(wrapper)
      await flushPromises()
      expect(pending).toHaveLength(2)
      expect(pending[0].signal.aborted).toBe(true)
      expect(pending[1].signal.aborted).toBe(false)
      expect(errorToast).not.toHaveBeenCalled()
      pending[1].resolve(new Response(JSON.stringify(body)))
      // 已被取消的旧响应即使随后到达也不能覆盖最新数据。
      pending[0].resolve(new Response(JSON.stringify({ ...body, total: 987 })))
      await flushPromises()
      expect(wrapper.text()).toContain("123")
      expect(wrapper.text()).not.toContain("987")

      await trigger(wrapper)
      pending[2].resolve(
        new Response(JSON.stringify({ code: "READ_FAILED", message: "真实读取失败" }), { status: 500 }),
      )
      await flushPromises()
      expect(wrapper.text()).toContain("真实读取失败")

      await trigger(wrapper)
      expect(pending).toHaveLength(4)
      wrapper.unmount()
      await flushPromises()
      expect(pending[3].signal.aborted).toBe(true)
      expect(errorToast).not.toHaveBeenCalled()
    },
  )
})
