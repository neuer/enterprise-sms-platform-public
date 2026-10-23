import { vi } from "vitest"

import { createDetailExport, getReport } from "../src/api/reports"

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  })
}

const filters = {
  granularity: "day" as const,
  groupBy: "dept" as const,
  category: "all" as const,
  start: "2026-07-01",
  end: "2026-07-02",
}

describe("报表查询参数", () => {
  it("未传不带 is_test，false 与 true 都写入查询串和导出体", async () => {
    const fetch = vi.fn(async (_url: string, init?: RequestInit) =>
      jsonResponse({ items: [], total: 0 }, init?.method === "POST" ? 202 : 200),
    )
    vi.stubGlobal("fetch", fetch)
    try {
      await getReport(filters)
      await getReport({ ...filters, isTest: false })
      await getReport({ ...filters, isTest: true })
      await createDetailExport({ ...filters, isTest: false }, false)
      const urls = fetch.mock.calls.map(([url]) => String(url))
      expect(new URL(urls[0], "http://localhost").searchParams.has("is_test")).toBe(false)
      expect(new URL(urls[1], "http://localhost").searchParams.get("is_test")).toBe("false")
      expect(new URL(urls[2], "http://localhost").searchParams.get("is_test")).toBe("true")
      const body = JSON.parse(String(fetch.mock.calls[3][1]?.body))
      expect(body.filters.is_test).toBe(false)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
