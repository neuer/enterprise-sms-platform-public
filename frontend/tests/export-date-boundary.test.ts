import { describe, expect, it } from "vitest"
import { nextShanghaiMidnight } from "../src/lib/time"

describe("上海日期导出终点", () => {
  it.each([
    ["2026-01-31", "2026-02-01"],
    ["2026-12-31", "2027-01-01"],
    ["2024-02-28", "2024-02-29"],
    ["2024-02-29", "2024-03-01"],
  ])("%s 的不包含终点为 %s 零点", (start, end) => {
    expect(nextShanghaiMidnight(start)).toBe(`${end}T00:00:00+08:00`)
  })
  it("拒绝非法日期", () => {
    expect(() => nextShanghaiMidnight("2026-02-29")).toThrow()
  })
})
