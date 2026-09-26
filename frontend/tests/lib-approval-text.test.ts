import { describe, expect, it } from "vitest"

import { categoryLabel, formatSegments, triggerRule } from "../src/lib/approvalText"
import type { ApprovalListItem } from "../src/api/approvals"

function item(partial: Partial<ApprovalListItem>): ApprovalListItem {
  return partial as ApprovalListItem
}

describe("approvalText 审批文案单点", () => {
  it("triggerRule：阈值快照与当前阈值分别标注", () => {
    expect(triggerRule(item({ category: "market", trigger_threshold: 50, trigger_threshold_source: "snapshot" }))).toBe(
      "营销 ≥ 50 个号码 · 提交时阈值快照",
    )
    expect(
      triggerRule(item({ category: "notice", trigger_threshold: 100, trigger_threshold_source: "snapshot" })),
    ).toBe("通知 ≥ 100 个号码 · 提交时阈值快照")
  })

  it("triggerRule：历史阈值不可确认与空阈值", () => {
    expect(
      triggerRule(item({ category: "market", trigger_threshold: 10, trigger_threshold_source: "legacy_unknown" })),
    ).toBe("历史阈值不可确认")
    expect(
      triggerRule(item({ category: "market", trigger_threshold: null, trigger_threshold_source: "snapshot" })),
    ).toBe("历史阈值不可确认")
  })

  it("formatSegments：空值占位，数值统一计费条单位", () => {
    expect(formatSegments(null)).toBe("—")
    expect(formatSegments(1)).toBe("1 计费条")
    expect(formatSegments(12345)).toBe("12,345 计费条")
  })

  it("categoryLabel 直接取共享类别标签", () => {
    expect(categoryLabel("notice")).toBe("通知")
    expect(categoryLabel("market")).toBe("营销")
  })
})
