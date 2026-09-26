import { describe, expect, it } from "vitest"

import { apiErrorMessage } from "../src/lib/securityDaily"
import { reportTrendDims, REPORT_TREND_OTHER_LABEL } from "../src/lib/reportTrend"
import { ApiRequestError } from "../src/api/client"
import type { ReportRow } from "../src/api/reports"

describe("securityDaily apiErrorMessage", () => {
  it("带错误码的 5xx 附重试建议，4xx 不附", () => {
    const serverError = new ApiRequestError(503, "SECURITY_DAILY_UNAVAILABLE", "控制面不可用")
    expect(apiErrorMessage(serverError, "兜底")).toBe("控制面不可用（错误码 SECURITY_DAILY_UNAVAILABLE），请刷新重试")

    const clientError = new ApiRequestError(422, "VALIDATION", "参数错误")
    expect(apiErrorMessage(clientError, "兜底")).toBe("参数错误（错误码 VALIDATION）")
  })

  it("无错误码异常与非 Error 值回退 errorText 语义", () => {
    expect(apiErrorMessage(new Error("网络中断"), "兜底提示")).toBe("网络中断")
    expect(apiErrorMessage("字符串", "兜底提示")).toBe("兜底提示")
  })
})

describe("reportTrendDims 维度归并", () => {
  function row(dimValue: string, dimLabel: string, total: number): ReportRow {
    return { dim_value: dimValue, dim_label: dimLabel, total } as ReportRow
  }

  it("按指标加总、按总量降序、同量按标签字典序", () => {
    const dims = reportTrendDims(
      [row("a", "应用A", 3), row("b", "应用B", 7), row("a", "应用A", 2), row("c", "应用C", 1)],
      "total",
    )
    expect(dims).toEqual([
      { key: "b", label: "应用B" },
      { key: "a", label: "应用A" },
      { key: "c", label: "应用C" },
    ])
    // 同量按标签字典序
    const tied = reportTrendDims([row("b", "应用B", 5), row("a", "应用A", 5)], "total")
    expect(tied.map((dim) => dim.key)).toEqual(["a", "b"])
  })

  it("不超过 6 个维度时全部保留", () => {
    const dims = reportTrendDims(
      Array.from({ length: 6 }, (_, i) => row(`k${i}`, `维度${i}`, 6 - i)),
      "total",
    )
    expect(dims).toHaveLength(6)
    expect(dims.some((dim) => dim.key === REPORT_TREND_OTHER_LABEL)).toBe(false)
  })

  it("超过 6 个维度时 Top 5 之外归并为「其他」", () => {
    const dims = reportTrendDims(
      Array.from({ length: 8 }, (_, i) => row(`k${i}`, `维度${i}`, 100 - i)),
      "total",
    )
    expect(dims).toHaveLength(6)
    expect(dims.at(-1)).toEqual({ key: REPORT_TREND_OTHER_LABEL, label: REPORT_TREND_OTHER_LABEL })
    expect(dims.slice(0, 5).map((dim) => dim.key)).toEqual(["k0", "k1", "k2", "k3", "k4"])
  })
})
