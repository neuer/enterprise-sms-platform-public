import {
  audienceToIsTest,
  composeWidth,
  dimColor,
  formatRate,
  rankWidth,
  rateClass,
  reportQuery,
  reportRowKey,
  shareOf,
} from "../src/views/report/reportModel"

const base = {
  granularity: "day" as const,
  groupBy: "app" as const,
  category: "all" as const,
  start: "2026-10-01",
  end: "2026-10-23",
}

describe("报表查询与展示计算", () => {
  it("全部不传 is_test，普通传 false，测试传 true", () => {
    expect(audienceToIsTest("all")).toBeUndefined()
    expect(audienceToIsTest("normal")).toBe(false)
    expect(audienceToIsTest("test")).toBe(true)
    expect(reportQuery(base, "all")).toEqual(base)
    expect(reportQuery(base, "normal")).toEqual({ ...base, isTest: false })
    expect(reportQuery(base, "test")).toEqual({ ...base, isTest: true })
  })

  it("成功率着色和占比按展示阈值计算", () => {
    expect(rateClass(0.98)).toBe("g")
    expect(rateClass(0.95)).toBe("y")
    expect(rateClass(0.949)).toBe("r")
    expect(formatRate(0.5)).toBe("50.0%")
    expect(shareOf(1, 0)).toBe("0.0%")
    expect(shareOf(1, 4)).toBe("25.0%")
    expect(composeWidth(0, 4)).toBe("0%")
    expect(composeWidth(1, 0)).toBe("0%")
    expect(composeWidth(1, 4)).toBe("25%")
  })

  it("排行宽度按当前指标的最大值归一，行键用周期和维度", () => {
    const rows = [
      { total: 10, total_segments: 4 },
      { total: 5, total_segments: 8 },
    ]
    expect(rankWidth(5, rows, "total")).toBe("50%")
    expect(rankWidth(8, rows, "total_segments")).toBe("100%")
    expect(rankWidth(1, [{ total: 0, total_segments: 0 }], "total")).toBe("0%")
    expect(reportRowKey({ period_start: "2026-10-01", dim_value: "app-1" })).toBe("2026-10-01-app-1")
    expect(dimColor(0)).toBe("var(--chart-green)")
    expect(dimColor(6)).toBe("var(--chart-green)")
  })
})
