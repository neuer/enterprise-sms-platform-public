import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus, { ElMessage, ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { vi } from "vitest"
import { useSessionStore } from "../src/stores/session"

import OpsView from "../src/views/OpsView.vue"

async function mountOps(query: Record<string, string> = {}, options: { attach?: boolean } = {}) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: "/ops", component: { template: "<div />" } }],
  })
  await router.push({ path: "/ops", query })
  await router.isReady()
  return mount(OpsView, {
    attachTo: options.attach ? document.body : undefined,
    global: { plugins: [pinia, ElementPlus, router] },
  })
}

function messageBoxButton(label: string): HTMLButtonElement {
  const button = [...document.querySelectorAll<HTMLButtonElement>(".el-overlay .el-message-box button")].find(
    (item) => {
      const overlay = item.closest(".el-overlay") as HTMLElement | null
      return overlay?.style.display !== "none" && item.textContent?.includes(label)
    },
  )
  if (!button) throw new Error(`确认框中没有「${label}」`)
  return button
}

function visibleConfirmBoxes(): HTMLElement[] {
  return [...document.querySelectorAll<HTMLElement>(".el-message-box.confirm-request")].filter((node) => {
    const overlay = node.closest(".el-overlay") as HTMLElement | null
    return Boolean(overlay) && overlay?.style.display !== "none" && overlay?.getAttribute("aria-hidden") !== "true"
  })
}

function visibleConfirmText(): string {
  return visibleConfirmBoxes()
    .map((node) => node.textContent ?? "")
    .join("")
}

function cleanupConfirms(): void {
  window.dispatchEvent(new Event("sms:session-clearing"))
  document.querySelectorAll(".el-overlay").forEach((node) => {
    if (node.querySelector(".el-message-box")) node.remove()
  })
}

const publicId = "c0a80101-0000-4000-8000-000000000134"
const outboxEventId = "c0a80101-0000-4000-8000-000000000199"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name: string) => (name === "content-length" && body === undefined ? "0" : null) },
    json: async () => body,
    blob: async () => new Blob(["masked-export"]),
  }
}

function result(url: string, method: string): unknown {
  if (method === "POST") {
    if (url.includes("/replay")) return { processed_items: 3 }
    if (url.includes("/reevaluate")) {
      return { parse_state: "unattempted", replay_eligibility: "automatic", reason: "unattempted", parser_version: 1 }
    }
    if (url.includes("/unmatched-reports/export")) {
      return {
        id: publicId,
        status: "pending",
        decrypted: false,
        row_count: null,
        download_url: null,
        expires_at: null,
        created_at: "2026-07-12T08:00:00+08:00",
      }
    }
    if (url.includes("/unmatched-reports"))
      return {
        items: [
          {
            id: 4,
            vendor_task_id: "vendor-1",
            custom_id: "legacy-1",
            phone_mask: "138****8000",
            report_status: 1,
            report_desc: "DELIVRD",
            report_time: "2026-07-12T08:00:00+08:00",
            created_at: "2026-07-12T08:00:00+08:00",
          },
        ],
        total: 45,
        page: 1,
        page_size: 20,
      }
    if (url.includes("/queue/resume")) return { resumed_batches: 2, paused_codes: ["999"] }
    if (url.includes("/outbox/") && url.endsWith("/retry")) return undefined
    if (url.includes("/chunks/") && url.includes("/resolution")) {
      return {
        id: 8,
        chunk_id: 9,
        batch_id: 1,
        action: "keep_unknown",
        state: "proposed",
        proposer_account_id: 1,
        confirmer_account_id: null,
        child_batch_id: null,
      }
    }
    if (url.includes("/resolutions/") && url.includes("/confirm")) {
      return {
        id: 8,
        chunk_id: 9,
        batch_id: 1,
        action: "keep_unknown",
        state: "confirmed",
        proposer_account_id: 1,
        confirmer_account_id: 2,
        child_batch_id: null,
      }
    }
    return undefined
  }
  if (url.includes(`/reports/export/${publicId}`) && !url.endsWith("/download"))
    return {
      id: publicId,
      status: "done",
      decrypted: false,
      row_count: 45,
      download_url: `/api/v1/web/reports/export/${publicId}/download`,
      expires_at: "2026-07-19T08:00:00+08:00",
      created_at: "2026-07-12T08:00:00+08:00",
    }
  if (url.endsWith("/alerts/current"))
    return {
      refreshed_at: "2026-07-12T08:00:00+08:00",
      complete: false,
      unknown_sources: ["control_redis"],
      items: [
        {
          key: "job_failed:poll_report",
          alert_type: "job_failed",
          level: "crit",
          title: "任务连续失败",
          detail: { job_name: "poll_report" },
          since: "2026-07-12T07:00:00+08:00",
          checked_at: "2026-07-12T08:00:00+08:00",
          target: "jobs",
        },
      ],
    }
  if (url.includes("/alerts"))
    return {
      items: [
        {
          id: 1,
          alert_type: "job_failed",
          level: "crit",
          title: "任务连续失败",
          detail: { job_name: "poll_report" },
          channels: "log-sink",
          created_at: "2026-07-12T08:00:00+08:00",
        },
      ],
      total: 45,
      page: 1,
      page_size: 20,
    }
  if (url.includes("/raw-logs"))
    return {
      items: [
        {
          id: 2,
          source: "report",
          item_count: 3,
          custom_id_count: 2,
          processed: false,
          error: "ValueError",
          fetched_at: "2026-07-12T08:00:00+08:00",
          capture_state: "complete",
          parse_state: "unattempted",
          replay_eligibility: "automatic",
        },
      ],
      total: 45,
      page: 1,
      page_size: 20,
    }
  if (url.includes("/chunks/uncertain"))
    return {
      items: [
        {
          chunk_id: 3,
          batch_no: "BATCH-1",
          custom_id: "CUSTOM-1",
          phone_count: 50,
          vendor_code: null,
          uncertain_since: "2026-07-12T08:00:00+08:00",
          age_seconds: 90000,
          status: "uncertain",
          resolution_id: null,
          resolution_action: null,
          resolution_state: null,
          proposer_account_id: null,
        },
      ],
      total: 45,
      page: 1,
      page_size: 20,
    }
  if (url.includes("/jobs"))
    return [
      {
        job_name: "poll_report",
        last_run_at: "2026-07-12T08:00:00+08:00",
        last_status: "failed",
        last_duration_ms: 120,
        last_items: 0,
        success_rate_24h: 0.75,
        stalled: true,
      },
      {
        job_name: "housekeeping",
        last_run_at: null,
        last_status: null,
        last_duration_ms: null,
        last_items: 0,
        success_rate_24h: 0,
        stalled: false,
      },
    ]
  if (url.includes("/outbox/events"))
    return {
      items: [
        {
          id: outboxEventId,
          event_type: "usage.release",
          aggregate_type: "usage_reservation",
          aggregate_id: "9f1c2e7a",
          task_name: "app.tasks.outbox.release_usage",
          queue: "realtime",
          state: "dead",
          attempts: 12,
          max_attempts: 12,
          failure_count: 3,
          last_error: "BrokerTimeout",
          next_attempt_at: "2026-07-12T08:00:00+08:00",
          created_at: "2026-07-12T08:00:00+08:00",
          updated_at: "2026-07-12T08:00:00+08:00",
        },
      ],
      total: 1,
      page: 1,
      page_size: 20,
    }
  if (url.endsWith("/admin/outbox"))
    return { pending: 3, published: 2, processing: 1, dead: 1, failed_attempts: 7, oldest_age_seconds: 301 }
  if (url.includes("/queue/status")) return { realtime_code: "999", bulk_code: "999", balance: 20000, threshold: 10000 }
  return { total: 0, items: [] }
}

function tab(wrapper: ReturnType<typeof mount>, label: string) {
  return wrapper.findAll(".ops-tabs button").find((item) => item.text().includes(label))!
}

// ElMessageBox.confirm 的消息为 h() VNode（lib/confirm.ts confirm-dialog），递归提取文本用于断言。
function vnodeText(node: unknown): string {
  if (typeof node === "string") return node
  if (Array.isArray(node)) return node.map(vnodeText).join("")
  if (node && typeof node === "object" && "children" in node) return vnodeText((node as { children: unknown }).children)
  return ""
}

describe("统一运维中心", () => {
  it("按 Tab 懒加载并执行确认式 raw 重放和任务触发", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = await mountOps()
    await flushPromises()

    expect(wrapper.text()).toContain("任务连续失败")
    expect(wrapper.find("[data-testid='current-alert-incomplete']").exists()).toBe(true)
    expect(wrapper.text()).toContain("control Redis")
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/alerts/current"))).toHaveLength(1)
    expect(fetch.mock.calls.some(([url]) => String(url).includes("/raw-logs"))).toBe(false)

    await tab(wrapper, "原始报文").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("ValueError")
    expect(wrapper.text()).not.toContain("payload_enc")
    await wrapper
      .findAll("button")
      .find((item) => item.text() === "重放")!
      .trigger("click")
    await flushPromises()
    expect(
      fetch.mock.calls.some(([url, init]) => String(url).endsWith("/raw-logs/2/replay") && init?.method === "POST"),
    ).toBe(true)

    await tab(wrapper, "任务健康").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("poll_report")
    expect(wrapper.text()).toContain("轮询厂商状态报告，保存原始报文后解析并更新发送结果")
    expect(wrapper.text()).toContain("无记录")
    // 健康列中文化：stalled→停滞，原始英文枚举不得上屏；成功率走 formatPercent 单点
    expect(wrapper.text()).toContain("停滞")
    expect(wrapper.text()).not.toContain("stalled")
    expect(wrapper.text()).toContain("75.0%")
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("手动触发"))!
      .trigger("click")
    await flushPromises()
    expect(
      fetch.mock.calls.some(
        ([url, init]) => String(url).includes("/jobs/poll_report/trigger") && init?.method === "POST",
      ),
    ).toBe(true)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("展示 unmatched 掩码并支持密文导出与 break-glass 恢复", async () => {
    sessionStorage.setItem("sms_token", "jwt-ops")
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = await mountOps()
    await flushPromises()

    await tab(wrapper, "无主报告").trigger("click")
    await flushPromises()
    const filterBar = wrapper.get("#ops-panel-unmatched .ops-filter-bar")
    expect(filterBar.find("[data-testid='ops-unmatched-phone']").exists()).toBe(true)
    expect(filterBar.text()).toContain("授权明文")
    expect(wrapper.text()).toContain("138****8000")
    expect(wrapper.find(".phone-mask").text()).toBe("138****8000")
    expect(wrapper.findAll(".ops-hash").map((item) => item.attributes("title"))).toEqual(["legacy-1", "vendor-1"])
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("导出对账"))!
      .trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain(`导出任务 #${publicId}`)
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith(`/reports/export/${publicId}`))).toBe(true)
    expect(wrapper.text()).toContain("45 行")
    expect(wrapper.get("[data-testid='download-unmatched-export']").text()).toContain("下载 CSV")

    vi.stubGlobal(
      "URL",
      Object.assign(URL, {
        createObjectURL: vi.fn(() => "blob:masked-export"),
        revokeObjectURL: vi.fn(),
      }),
    )
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
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("恢复队列"))!
      .trigger("click")
    await flushPromises()
    expect(
      fetch.mock.calls.some(
        ([url, init]) => String(url).includes("/queue/resume?force=true") && init?.method === "POST",
      ),
    ).toBe(true)

    wrapper.unmount()
    sessionStorage.removeItem("sms_token")
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("Outbox 面板展示投递统计并支持确认式死信重推", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = await mountOps()
    await flushPromises()

    expect(fetch.mock.calls.some(([url]) => String(url).includes("/outbox"))).toBe(false)

    await tab(wrapper, "Outbox 投递").trigger("click")
    await flushPromises()

    expect(wrapper.get("[data-testid='outbox-stats']").text()).toContain("死信")
    expect(wrapper.text()).toContain("usage.release")
    expect(wrapper.text()).toContain("release_usage")
    expect(wrapper.text()).toContain("BrokerTimeout")
    expect(wrapper.text()).toContain("12/12")

    await wrapper.get(`[data-testid='outbox-retry-${outboxEventId}']`).trigger("click")
    await flushPromises()
    expect(confirm).toHaveBeenCalledTimes(1)
    expect(vnodeText(confirm.mock.calls[0][0])).toContain("usage.release")
    expect(
      fetch.mock.calls.some(
        ([url, init]) => String(url).endsWith(`/outbox/${outboxEventId}/retry`) && init?.method === "POST",
      ),
    ).toBe(true)
    expect(fetch.mock.calls.filter(([url]) => String(url).includes("/outbox/events")).length).toBeGreaterThanOrEqual(2)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("告警行可打开详情抽屉查看结构化 detail", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    const wrapper = await mountOps()
    await flushPromises()

    await wrapper
      .findAll("button")
      .find((item) => item.text() === "告警历史")!
      .trigger("click")
    await flushPromises()
    await wrapper.get("[data-testid='alert-detail-1']").trigger("click")
    await flushPromises()

    const detail = wrapper.get("[data-testid='alert-detail-json']")
    expect(detail.text()).toContain("job_name")
    expect(detail.text()).toContain("poll_report")
    expect(wrapper.text()).toContain("告警详情")
    expect(wrapper.text()).toContain("任务连续失败")

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("快速离开再回到告警页时，过期响应不得覆盖最新列表", async () => {
    let releaseInitial: ((value: unknown) => void) | undefined
    const initialAlerts = new Promise((resolve) => {
      releaseInitial = resolve
    })
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method || "GET"
      if (method === "GET" && url.endsWith("/alerts/current")) {
        if (fetch.mock.calls.filter(([item]) => String(item).endsWith("/alerts/current")).length === 1) {
          await initialAlerts
          return response({
            refreshed_at: "2026-07-12T08:00:00+08:00",
            complete: true,
            unknown_sources: [],
            items: [
              {
                key: "stale",
                alert_type: "job_failed",
                level: "crit",
                title: "过期告警不应出现",
                detail: { job_name: "stale" },
                since: "2026-07-12T08:00:00+08:00",
                checked_at: "2026-07-12T08:00:00+08:00",
                target: "jobs",
              },
            ],
          })
        }
      }
      return response(result(url, method))
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = await mountOps()
    await tab(wrapper, "结果未知").trigger("click")
    await flushPromises()
    await tab(wrapper, "告警").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("任务连续失败")
    expect(wrapper.text()).not.toContain("过期告警不应出现")
    releaseInitial?.(undefined)
    await flushPromises()
    expect(wrapper.text()).toContain("任务连续失败")
    expect(wrapper.text()).not.toContain("过期告警不应出现")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("对账导出轮询超过约 5 分钟未完成时停止并提示超时", async () => {
    vi.useFakeTimers()
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      if (url.includes(`/reports/export/${publicId}`) && !url.endsWith("/download")) {
        return response({
          id: publicId,
          status: "pending",
          decrypted: false,
          row_count: null,
          download_url: null,
          expires_at: null,
          created_at: "2026-07-12T08:00:00+08:00",
        })
      }
      return response(result(url, init?.method || "GET"))
    })
    vi.stubGlobal("fetch", fetch)
    const wrapper = await mountOps()
    await flushPromises()

    await tab(wrapper, "无主报告").trigger("click")
    await flushPromises()
    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("导出对账"))!
      .trigger("click")
    await flushPromises()

    const statusCalls = () =>
      fetch.mock.calls.filter(([url]) => String(url).includes(`/reports/export/${publicId}`)).length
    expect(statusCalls()).toBe(1)

    // 150 次 × 2s ≈ 5 分钟兜底：到达上限后停止轮询并给出中文超时提示
    await vi.advanceTimersByTimeAsync(298_000)
    expect(statusCalls()).toBe(150)
    expect(wrapper.text()).toContain("导出状态查询超时（已超过 5 分钟）。同一任务仍在后台，可重新查询，不必再次创建。")

    await vi.advanceTimersByTimeAsync(30_000)
    expect(statusCalls()).toBe(150)
    wrapper.unmount()
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it("unmatched 手机号格式不合法时查询与导出均被提交前拦截", async () => {
    const warning = vi.spyOn(ElMessage, "warning")
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    const wrapper = await mountOps()
    await flushPromises()

    await tab(wrapper, "无主报告").trigger("click")
    await flushPromises()
    const unmatchedCalls = () => fetch.mock.calls.filter(([url]) => String(url).includes("/unmatched-reports")).length
    expect(unmatchedCalls()).toBe(1)

    await wrapper.get("[data-testid='ops-unmatched-phone']").setValue("12345")
    await wrapper.get("[data-testid='ops-unmatched-search']").trigger("click")
    await flushPromises()
    expect(warning).toHaveBeenCalledWith("手机号须为 11 位以 1 开头的数字")
    expect(unmatchedCalls()).toBe(1)

    await wrapper
      .findAll("button")
      .find((item) => item.text().includes("导出对账"))!
      .trigger("click")
    await flushPromises()
    expect(unmatchedCalls()).toBe(1)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("从查询参数打开指定运维页签", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => response(result(input, init?.method || "GET")))
    vi.stubGlobal("fetch", fetch)
    const wrapper = await mountOps({ tab: "uncertain" })
    await flushPromises()
    expect(wrapper.text()).toContain("CUSTOM-1")
    expect(wrapper.text()).toContain("结果未知")
    expect(fetch.mock.calls.some(([url]) => String(url).includes("/chunks/uncertain"))).toBe(true)
    expect(fetch.mock.calls.some(([url]) => String(url).includes("/alerts"))).toBe(false)
    expect(wrapper.text()).toContain("禁止自动重发")
    wrapper.unmount()
    vi.unstubAllGlobals()
  })

  it("保守终态分片可提出并确认双人处置，且不把旧分片改回待发送", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      if (url.includes("/chunks/uncertain") && (init?.method || "GET") === "GET") {
        return response({
          items: [
            {
              chunk_id: 9,
              batch_no: "BATCH-9",
              custom_id: "CUSTOM-9",
              phone_count: 2,
              vendor_code: null,
              uncertain_since: "2026-07-12T08:00:00+08:00",
              age_seconds: 260000,
              status: "unknown_terminal",
              resolution_id: null,
              resolution_action: null,
              resolution_state: null,
              proposer_account_id: null,
            },
          ],
          total: 1,
          page: 1,
          page_size: 20,
        })
      }
      return response(result(url, init?.method || "GET"))
    })
    vi.stubGlobal("fetch", fetch)
    vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = await mountOps({ tab: "uncertain" })
    await flushPromises()
    expect(wrapper.text()).toContain("未知终态")
    expect(wrapper.text()).toContain("新批次重发")
    await wrapper.get("[data-testid='uncertain-propose-keep_unknown']").trigger("click")
    await flushPromises()
    const proposeCall = fetch.mock.calls.find(
      ([url, init]) => String(url).includes("/chunks/9/resolution") && init?.method === "POST",
    )
    expect(proposeCall).toBeTruthy()
    expect(JSON.parse(String(proposeCall![1]?.body))).toEqual({ action: "keep_unknown" })
    expect(vnodeText(vi.mocked(ElMessageBox.confirm).mock.calls[0][0])).toContain("不会把旧分片改回待发送")
    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it("重放入口以 replay_eligibility 为准，协议异常捕获态不展示重放与重评估", async () => {
    const fetch = vi.fn(async (input: string, init?: RequestInit) => {
      const url = String(input)
      if (url.includes("/raw-logs") && (init?.method || "GET") === "GET") {
        return response({
          items: [
            {
              id: 5,
              source: "report",
              item_count: 1,
              custom_id_count: 1,
              processed: false,
              error: "VendorApiError",
              fetched_at: "2026-07-12T08:00:00+08:00",
              capture_state: "protocol_invalid",
              parse_state: "protocol_invalid",
              replay_eligibility: "never",
            },
            {
              id: 6,
              source: "reply",
              item_count: 2,
              custom_id_count: 0,
              processed: false,
              error: null,
              fetched_at: "2026-07-12T08:00:00+08:00",
              capture_state: "complete_too_large",
              parse_state: "unattempted",
              replay_eligibility: "manual",
            },
          ],
          total: 2,
          page: 1,
          page_size: 20,
        })
      }
      return response(result(url, init?.method || "GET"))
    })
    vi.stubGlobal("fetch", fetch)
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const wrapper = await mountOps({ tab: "raw" })
    await flushPromises()
    // 协议异常报文：不得出现重放入口，也不得出现重评估（后端拒绝该捕获态）
    expect(wrapper.text()).toContain("协议异常")
    expect(wrapper.text()).toContain("超限完整")
    const rows = wrapper.findAll("#ops-panel-raw .ops-table .el-table__row")
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain("协议异常")
    expect(rows[0].findAll("button").filter((item) => item.text() === "重放")).toHaveLength(0)
    expect(rows[0].findAll("button").filter((item) => item.text() === "重评估")).toHaveLength(0)
    // manual 资格且捕获完整：重放与重评估均可用
    expect(rows[1].findAll("button").filter((item) => item.text() === "重放")).toHaveLength(1)

    const success = vi.spyOn(ElMessage, "success")
    await rows[1]
      .findAll("button")
      .find((item) => item.text() === "重评估")!
      .trigger("click")
    await flushPromises()
    expect(vnodeText(confirm.mock.calls[0][0])).toContain("不投影业务")
    expect(
      fetch.mock.calls.some(([url, init]) => String(url).endsWith("/raw-logs/6/reevaluate") && init?.method === "POST"),
    ).toBe(true)
    expect(success.mock.calls.some(([message]) => String(message).includes("本次操作已记入审计"))).toBe(true)

    wrapper.unmount()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })
})

it("拆分后的页签切换取消旧读取，迟到失败不污染当前页签，切回保留筛选", async () => {
  const reads: { signal: AbortSignal; resolve: (value: ReturnType<typeof response>) => void }[] = []
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init: RequestInit = {}) => {
      if (url.includes("/raw-logs"))
        return new Promise<ReturnType<typeof response>>((resolve) =>
          reads.push({ signal: init.signal as AbortSignal, resolve }),
        )
      return Promise.resolve(response(result(url, init.method ?? "GET")))
    }),
  )
  const wrapper = await mountOps({ tab: "raw" })
  await flushPromises()
  expect(reads).toHaveLength(1)
  await wrapper.get("#ops-tab-jobs").trigger("click")
  await flushPromises()
  expect(reads[0].signal.aborted).toBe(true)
  reads[0].resolve(response({ code: "STALE_FAILURE", message: "旧页签错误" }, 500))
  await flushPromises()
  expect(wrapper.text()).not.toContain("旧页签错误")
  expect((wrapper.get("#ops-panel-jobs").element.parentElement as HTMLElement).style.display).not.toBe("none")
  expect((wrapper.get("#ops-panel-raw").element.parentElement as HTMLElement).style.display).toBe("none")
  await wrapper.get("#ops-tab-raw").trigger("click")
  await flushPromises()
  expect(reads).toHaveLength(2)
  expect((wrapper.get("#ops-panel-raw").element.parentElement as HTMLElement).style.display).not.toBe("none")
  expect((wrapper.get("#ops-panel-jobs").element.parentElement as HTMLElement).style.display).toBe("none")
  wrapper.unmount()
  expect(reads[1].signal.aborted).toBe(true)
  reads[1].resolve(response({ items: [], total: 0, page: 1, page_size: 20 }))
  await flushPromises()
  vi.unstubAllGlobals()
})

function uncertainTerminalFetch() {
  return vi.fn(async (input: string, init?: RequestInit) => {
    const url = String(input)
    if (url.includes("/chunks/uncertain") && (init?.method || "GET") === "GET") {
      return response({
        items: [
          {
            chunk_id: 9,
            batch_no: "BATCH-9",
            custom_id: "CUSTOM-9",
            phone_count: 2,
            vendor_code: null,
            uncertain_since: "2026-07-12T08:00:00+08:00",
            age_seconds: 260000,
            status: "unknown_terminal",
            resolution_id: null,
            resolution_action: null,
            resolution_state: null,
            proposer_account_id: null,
          },
        ],
        total: 1,
        page: 1,
        page_size: 20,
      })
    }
    if (url.includes("/chunks/9/resolution") && init?.method === "POST") return response({ id: 8 })
    return response(result(url, init?.method || "GET"))
  })
}

it.each(["取消", "关闭", "Escape"] as const)("未知处置经%s退出后释放忙锁且不写请求", async (exit) => {
  const fetch = uncertainTerminalFetch()
  vi.stubGlobal("fetch", fetch)
  const wrapper = await mountOps({ tab: "uncertain" }, { attach: true })
  try {
    await flushPromises()
    const propose = () => wrapper.get("[data-testid='uncertain-propose-keep_unknown']").trigger("click")
    await propose()
    await flushPromises()
    expect(visibleConfirmText()).toContain("不会把旧分片改回待发送")
    if (exit === "取消") messageBoxButton("取消").click()
    else if (exit === "关闭") {
      const box = visibleConfirmBoxes()[0]
      const close = box?.querySelector<HTMLButtonElement>(".el-message-box__headerbtn, button[aria-label]")
      if (!close) throw new Error(`关闭按钮缺失：${box?.innerHTML ?? "无对话框"}`)
      close.click()
    } else document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true }))
    await flushPromises()
    expect(visibleConfirmText()).not.toContain("不会把旧分片改回待发送")
    expect(fetch.mock.calls.some(([url, init]) => String(url).includes("/resolution") && init?.method === "POST")).toBe(
      false,
    )
    await propose()
    await flushPromises()
    expect(visibleConfirmText()).toContain("确认提出处置")
  } finally {
    wrapper.unmount()
    cleanupConfirms()
    vi.unstubAllGlobals()
  }
})

it("卸载未知处置页时关闭确认且不提交，确认成功只提交一次", async () => {
  const fetch = uncertainTerminalFetch()
  vi.stubGlobal("fetch", fetch)
  const wrapper = await mountOps({ tab: "uncertain" }, { attach: true })
  await flushPromises()
  await wrapper.get("[data-testid='uncertain-propose-keep_unknown']").trigger("click")
  await flushPromises()
  expect(visibleConfirmText()).toContain("不会把旧分片改回待发送")
  wrapper.unmount()
  await flushPromises()
  expect(visibleConfirmText()).not.toContain("不会把旧分片改回待发送")
  expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false)

  const again = await mountOps({ tab: "uncertain" }, { attach: true })
  await flushPromises()
  await again.get("[data-testid='uncertain-propose-keep_unknown']").trigger("click")
  await again.get("[data-testid='uncertain-propose-keep_unknown']").trigger("click")
  await flushPromises()
  expect(visibleConfirmBoxes()).toHaveLength(1)
  messageBoxButton("确认提出").click()
  await flushPromises()
  const posts = fetch.mock.calls.filter(
    ([url, init]) => String(url).includes("/chunks/9/resolution") && init?.method === "POST",
  )
  expect(posts).toHaveLength(1)
  again.unmount()
  cleanupConfirms()
  vi.unstubAllGlobals()
})

it("取消任务确认后焦点回到触发按钮，确认期间按钮保持可聚焦", async () => {
  const fetch = vi.fn(async (input: string, init?: RequestInit) =>
    response(result(String(input), init?.method || "GET")),
  )
  vi.stubGlobal("fetch", fetch)
  const wrapper = await mountOps({ tab: "jobs" }, { attach: true })
  await flushPromises()
  const button = wrapper.get("[data-testid='job-trigger-poll_report']")
  ;(button.element as HTMLButtonElement).focus()
  await button.trigger("click")
  await button.trigger("click")
  await flushPromises()
  expect(visibleConfirmText()).toContain("立即投递一次执行")
  expect(button.classes()).not.toContain("is-loading")
  expect(button.attributes("disabled")).toBeUndefined()
  expect(visibleConfirmBoxes()).toHaveLength(1)
  messageBoxButton("取消").click()
  await flushPromises()
  await vi.waitFor(() => {
    expect(document.activeElement).toBe(button.element)
  })
  expect(fetch.mock.calls.some(([url, init]) => String(url).includes("/trigger") && init?.method === "POST")).toBe(
    false,
  )
  wrapper.unmount()
  cleanupConfirms()
  vi.unstubAllGlobals()
})

it("任务确认期间会话失效不提交，焦点留在当前页标题", async () => {
  const fetch = vi.fn(async (input: string, init?: RequestInit) =>
    response(result(String(input), init?.method || "GET")),
  )
  vi.stubGlobal("fetch", fetch)
  const wrapper = await mountOps({ tab: "jobs" }, { attach: true })
  await flushPromises()
  const button = wrapper.get("[data-testid='job-trigger-poll_report']")
  await button.trigger("click")
  await flushPromises()
  useSessionStore().clear()
  await flushPromises()
  expect(visibleConfirmText()).not.toContain("立即投递一次执行")
  expect(fetch.mock.calls.some(([url, init]) => String(url).includes("/trigger") && init?.method === "POST")).toBe(
    false,
  )
  await vi.waitFor(() => {
    expect(document.activeElement?.tagName).toBe("H1")
  })
  expect(document.activeElement).not.toBe(button.element)
  wrapper.unmount()
  cleanupConfirms()
  vi.unstubAllGlobals()
})

it("离开任务页后取消确认，焦点不回到已隐藏的触发按钮", async () => {
  const fetch = vi.fn(async (input: string, init?: RequestInit) =>
    response(result(String(input), init?.method || "GET")),
  )
  vi.stubGlobal("fetch", fetch)
  const wrapper = await mountOps({ tab: "jobs" }, { attach: true })
  await flushPromises()
  const button = wrapper.get("[data-testid='job-trigger-poll_report']")
  await button.trigger("click")
  await flushPromises()
  await wrapper.get("#ops-tab-alerts").trigger("click")
  messageBoxButton("取消").click()
  await flushPromises()
  await vi.waitFor(() => {
    expect(document.activeElement?.tagName).toBe("H1")
  })
  expect(document.activeElement).not.toBe(button.element)
  wrapper.unmount()
  cleanupConfirms()
  vi.unstubAllGlobals()
})
