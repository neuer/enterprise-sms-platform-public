import {
  CATEGORY_LABELS,
  DEFAULT_PAGE_SIZE,
  ROLE_LABELS,
  STATUS_LABELS,
  VENDOR_REVIEW_LABELS,
  vendorReviewSub,
} from "../src/lib/labels"

describe("共享文案单点", () => {
  it("DEFAULT_PAGE_SIZE 固定为 20（前端 UI 约定）", () => {
    expect(DEFAULT_PAGE_SIZE).toBe(20)
  })

  it("类别/角色/厂商审核映射覆盖全部取值且无空洞", () => {
    expect(Object.keys(CATEGORY_LABELS).sort()).toEqual(["market", "notice", "verify"])
    expect(Object.keys(ROLE_LABELS).sort()).toEqual(["admin", "approver", "operator", "viewer"])
    expect(Object.keys(VENDOR_REVIEW_LABELS).sort()).toEqual(["approved", "draft", "pending", "rejected"])

    for (const mapping of [CATEGORY_LABELS, ROLE_LABELS, VENDOR_REVIEW_LABELS, STATUS_LABELS]) {
      for (const [key, label] of Object.entries(mapping)) {
        expect(label, `映射 ${key} 不得为空`).not.toBe("")
      }
    }
  })

  it("STATUS_LABELS 覆盖批次/明细/分片状态机的关键取值", () => {
    // 与 AGENTS.md 硬性规则 9 状态机对齐，防止新增状态漏配文案
    for (const [status, label] of [
      ["pending_approval", "待审批"],
      ["scheduled", "已排期"],
      ["queued", "排队中"],
      ["sending", "发送中"],
      ["completed", "已完成"],
      ["completed_unknown", "完成(含未知)"],
      ["cancelled", "已取消"],
      ["rejected", "已驳回"],
      ["expired", "已过期"],
      ["balance_blocked", "余额阻断"],
      ["split_capacity_blocked", "拆分容量阻塞"],
      ["uncertain", "结果未知"],
      ["unknown_terminal", "未知终态"],
      ["failed", "失败"],
      ["delivered", "已送达"],
      ["dead", "终止重试"],
    ] as const) {
      expect(STATUS_LABELS[status]).toBe(label)
    }
  })

  it("vendorReviewSub 按厂商编号区分 approved/pending 副行", () => {
    expect(vendorReviewSub("approved", "8821", null)).toEqual({ text: "厂商 #8821" })
    expect(vendorReviewSub("approved", null, null)).toEqual({ text: "厂商编号待同步" })
    expect(vendorReviewSub("pending", "8821", null)).toEqual({ text: "厂商审核中 · #8821" })
    expect(vendorReviewSub("pending", null, null)).toEqual({ text: "提交厂商中…" })
  })

  it("vendorReviewSub 对 rejected 返回警示色副行与驳回原因兜底", () => {
    expect(vendorReviewSub("rejected", null, "内容违规")).toEqual({ text: "内容违规", tone: "verm" })
    expect(vendorReviewSub("rejected", null, null)).toEqual({ text: "厂商未附驳回原因", tone: "verm" })
  })
})
