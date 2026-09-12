import {
  dateKeyWeekday,
  daysAgoDateKey,
  formatDateTime,
  formatDateTimeMinute,
  formatDurationHms,
  formatHm,
  formatHms,
  shanghaiDateKey,
} from "../src/lib/time"

describe("时间格式化单点（Asia/Shanghai）", () => {
  // 固定 UTC 时刻，断言与浏览器本地时区无关的 +08:00 输出
  const instant = new Date("2026-09-05T02:03:04Z")

  it("formatDateTime 输出 YYYY-MM-DD HH:mm:ss", () => {
    expect(formatDateTime(instant)).toBe("2026-09-05 10:03:04")
    expect(formatDateTime("2026-09-05T02:03:04Z")).toBe("2026-09-05 10:03:04")
    expect(formatDateTime(instant.getTime())).toBe("2026-09-05 10:03:04")
  })

  it("UTC 与 +08:00 跨日时按上海日历日进位", () => {
    expect(formatDateTime("2026-01-01T17:30:00Z")).toBe("2026-01-02 01:30:00")
  })

  it("formatDateTimeMinute / formatHm / formatHms 截断到对应精度", () => {
    expect(formatDateTimeMinute(instant)).toBe("09-05 10:03")
    expect(formatHm(instant)).toBe("10:03")
    expect(formatHms(instant)).toBe("10:03:04")
  })

  it.each([
    ["null", null],
    ["undefined", undefined],
    ["空串", ""],
    ["非法字符串", "not-a-date"],
    ["NaN 时间戳", Number.NaN],
  ])("formatDateTime 对%s返回兜底文案", (_name, value) => {
    expect(formatDateTime(value)).toBe("—")
    expect(formatDateTime(value, "无")).toBe("无")
    expect(formatDateTimeMinute(value)).toBe("—")
    expect(formatHm(value)).toBe("—")
    expect(formatHms(value)).toBe("—")
  })

  it("formatDurationHms floor 到秒且小时可超 24", () => {
    expect(formatDurationHms(3_661_999)).toBe("01:01:01")
    expect(formatDurationHms(90_000_000)).toBe("25:00:00")
    expect(formatDurationHms(0)).toBe("00:00:00")
  })

  it("shanghaiDateKey 按上海日历日取 YYYY-MM-DD", () => {
    expect(shanghaiDateKey(instant)).toBe("2026-09-05")
    expect(shanghaiDateKey(new Date("2026-09-05T16:30:00Z"))).toBe("2026-09-06")
  })

  it("daysAgoDateKey 以当日 00:00+08:00 为基准做整天偏移", () => {
    expect(daysAgoDateKey(1, instant)).toBe("2026-09-04")
    expect(daysAgoDateKey(0, instant)).toBe("2026-09-05")
    expect(daysAgoDateKey(1, new Date("2026-03-01T01:00:00Z"))).toBe("2026-02-28")
  })
})

describe("严格日历星期", () => {
  it.each([
    ["2026-01-01", "周四"],
    ["2026-03-08", "周日"],
    ["2026-11-01", "周日"],
    ["2024-02-29", "周四"],
    ["2026-12-31", "周四"],
    ["2026-09-12", "周六"],
  ])("%s 为 %s", (day, weekday) => {
    expect(dateKeyWeekday(day)).toBe(weekday)
  })
  it.each(["", "2026-02-29", "2026-02-30", "2026-13-01", "2026-1-01", "invalid"])("拒绝非法日期 %s", (day) =>
    expect(dateKeyWeekday(day)).toBe("—"),
  )
})
