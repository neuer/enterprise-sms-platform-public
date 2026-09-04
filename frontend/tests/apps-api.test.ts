import { describe, expect, it } from "vitest"

import { estimateWorstCaseCapacity } from "../src/api/apps"

describe("应用准入估算", () => {
  it("1×10000 与 100×100 的最坏号码上限相同", () => {
    const oneLarge = estimateWorstCaseCapacity({
      rate_limit_per_min: 1,
      recipient_limit_per_min: 10000,
      segment_limit_per_min: 10000,
      daily_quota: 0,
    })
    const manySmall = estimateWorstCaseCapacity({
      rate_limit_per_min: 100,
      recipient_limit_per_min: 10000,
      segment_limit_per_min: 10000,
      daily_quota: 5000,
    })
    expect(oneLarge.recipientsPerMin).toBe(10000)
    expect(manySmall.recipientsPerMin).toBe(10000)
    expect(oneLarge.dailySegments).toBeNull()
    expect(manySmall.dailySegments).toBe(5000)
  })
})
