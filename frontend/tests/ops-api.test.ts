import { beforeEach, vi } from "vitest"

import {
  createUnmatchedExport,
  getCurrentAlerts,
  listAlerts,
  listRawLogs,
  listUncertain,
  listUnmatched,
} from "../src/api/ops"

function response(body: unknown) {
  return {
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => body,
  }
}

describe("运维查询 API", () => {
  beforeEach(() => {
    sessionStorage.setItem("sms_token", "jwt")
    vi.unstubAllGlobals()
  })

  it("把分页和后端支持的筛选条件完整传给各列表端点", async () => {
    const fetch = vi.fn().mockResolvedValue(response({ items: [], total: 0, page: 1, page_size: 20 }))
    vi.stubGlobal("fetch", fetch)

    await getCurrentAlerts()
    await listAlerts({
      page: 2,
      pageSize: 50,
      alertType: "job_failed",
      level: "crit",
      start: "2026-07-01T00:00:00+08:00",
      end: "2026-07-20T23:59:59+08:00",
    })
    await listRawLogs({
      page: 3,
      pageSize: 20,
      source: "report",
      processed: false,
    })
    await listUncertain({ page: 4, pageSize: 20 })
    await listUnmatched({
      page: 5,
      pageSize: 20,
      phone: "13800138000",
      start: "2026-07-01T00:00:00+08:00",
      end: "2026-07-20T23:59:59+08:00",
    })

    expect(fetch.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/v1/web/admin/alerts/current",
      "/api/v1/web/admin/alerts?page=2&page_size=50&alert_type=job_failed&level=crit&start=2026-07-01T00%3A00%3A00%2B08%3A00&end=2026-07-20T23%3A59%3A59%2B08%3A00",
      "/api/v1/web/admin/raw-logs?page=3&page_size=20&source=report&processed=false",
      "/api/v1/web/admin/chunks/uncertain?page=4&page_size=20",
      "/api/v1/web/admin/unmatched-reports",
    ])
    // 手机号等查询条件只在 POST body 携带，不得出现在 URL（硬性规则 2：明文不得进访问日志）
    const unmatchedInit = fetch.mock.calls[4][1]
    expect(unmatchedInit.method).toBe("POST")
    expect(JSON.parse(String(unmatchedInit.body))).toEqual({
      phone: "13800138000",
      start: "2026-07-01T00:00:00+08:00",
      end: "2026-07-20T23:59:59+08:00",
      page: 5,
      page_size: 20,
    })
  })

  it("创建 unmatched 导出时沿用页面的号码与时间筛选", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({ id: "c0a80101-0000-4000-8000-000000000134", status: "pending" }),
    )
    vi.stubGlobal("fetch", fetch)

    await createUnmatchedExport(
      {
        phone: "13800138000",
        start: "2026-07-01T00:00:00+08:00",
        end: "2026-07-20T23:59:59+08:00",
      },
      true,
    )

    expect(JSON.parse(String(fetch.mock.calls[0][1].body))).toEqual({
      phone: "13800138000",
      start: "2026-07-01T00:00:00+08:00",
      end: "2026-07-20T23:59:59+08:00",
      decrypted: true,
    })
  })
})
