import { afterEach, describe, expect, it, vi } from "vitest"

import {
  listSecurityDailyReports,
  retrySecurityDailyReport,
  sendSecurityDailyReport,
} from "../src/api/securityDaily"

describe("安全日报 API client", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("使用 Web Bearer API 路径、分页过滤和确认投递请求", async () => {
    const fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [], total: 0, page: 1, page_size: 20 }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetch)

    await listSecurityDailyReports({
      dateFrom: "2026-07-01",
      dateTo: "2026-07-15",
      status: "high",
      generationStatus: "failed",
      page: 2,
    })
    expect(fetch.mock.calls[0][0]).toBe(
      "/api/v1/web/admin/security-daily/reports?page=2&page_size=20&date_from=2026-07-01&date_to=2026-07-15&status=high&generation_status=failed",
    )

    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ request_id: "id", report_date: "2026-07-15", action: "send", state: "pending", idempotent: false }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    )
    fetch.mockResolvedValueOnce(
      new Response(JSON.stringify({ request_id: "id-2", report_date: "2026-07-15", action: "retry", state: "pending", idempotent: false }), {
        status: 202,
        headers: { "Content-Type": "application/json" },
      }),
    )
    await sendSecurityDailyReport("2026-07-15")
    expect(fetch.mock.calls[1][0]).toBe("/api/v1/web/admin/security-daily/reports/2026-07-15/send")
    expect(JSON.parse(fetch.mock.calls[1][1].body as string)).toEqual({ confirm: true })

    await retrySecurityDailyReport("2026-07-15")
    expect(fetch.mock.calls[2][0]).toBe("/api/v1/web/admin/security-daily/reports/2026-07-15/retry")
    expect(JSON.parse(fetch.mock.calls[2][1].body as string)).toEqual({ confirm: true })
  })
})
