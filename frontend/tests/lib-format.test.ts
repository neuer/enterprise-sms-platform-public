import { formatNumber, formatPercent } from "../src/lib/format"

describe("数值展示格式化单点", () => {
  it("formatPercent 默认一位小数", () => {
    expect(formatPercent(0.836)).toBe("83.6%")
    expect(formatPercent(1)).toBe("100.0%")
    expect(formatPercent(0)).toBe("0.0%")
  })

  it("formatPercent 支持自定义小数位", () => {
    expect(formatPercent(0.83654, 2)).toBe("83.65%")
    expect(formatPercent(0.5, 0)).toBe("50%")
  })

  it("formatNumber 固定 zh-CN 千分位，空值显示「—」", () => {
    expect(formatNumber(1234567)).toBe("1,234,567")
    expect(formatNumber(0)).toBe("0")
    expect(formatNumber(null)).toBe("—")
    expect(formatNumber(undefined)).toBe("—")
  })
})
