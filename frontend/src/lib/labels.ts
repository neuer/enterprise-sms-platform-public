/**
 * 跨页面共享文案单点：消息类别、角色、分页默认值、状态标签、厂商审核状态。
 * 各视图/组件禁止再维护同名映射，新增取值在此扩展。
 */

export const CATEGORY_LABELS: Record<string, string> = {
  verify: "验证码",
  notice: "通知",
  market: "营销",
}

export const ROLE_LABELS: Record<string, string> = {
  admin: "系统管理员",
  approver: "审批人",
  operator: "操作员",
  viewer: "只读用户",
}

export const DEFAULT_PAGE_SIZE = 20

/**
 * 批次 / 明细 / 分片状态中文标签唯一事实源（StatusTag 与列表页共用）；
 * sent/other 为消息明细状态，submitted 为分片状态，dead 为回调终止态。
 */
export const STATUS_LABELS: Record<string, string> = {
  pending: "待处理",
  pending_approval: "待审批",
  scheduled: "已排期",
  queued: "排队中",
  sending: "发送中",
  submitted: "已提交",
  sent: "已提交",
  completed: "已完成",
  completed_unknown: "完成(含未知)",
  delivered: "已送达",
  approved: "已通过",
  failed: "失败",
  rejected: "已驳回",
  cancelled: "已取消",
  expired: "已过期",
  uncertain: "结果未知",
  unknown_terminal: "未知终态",
  balance_blocked: "余额阻断",
  unknown: "未知",
  dead: "终止重试",
  other: "其他",
}

/** 厂商审核状态主标签；draft 为模板特有的平台草稿态（签名无此态）。 */
export const VENDOR_REVIEW_LABELS: Record<string, string> = {
  draft: "草稿",
  pending: "待审核",
  approved: "已通过",
  rejected: "已拒绝",
}

/** 平台送审后厂商侧的三态；模板另有 draft 平台草稿态，由视图自行兜底。 */
export type VendorReviewState = "pending" | "approved" | "rejected"

/** 厂商审核状态副行；tone=verm 时视图以警示色呈现（驳回原因）。 */
export interface VendorReviewSub {
  text: string
  tone?: "verm"
}

/**
 * 厂商审核状态副行：approved/pending 按厂商编号区分待同步/提交中，
 * rejected 直接显示驳回原因。模板与签名视图共用，仅厂商编号字段不同。
 */
export function vendorReviewSub(
  vendorState: VendorReviewState,
  vendorId: string | null,
  rejectReason: string | null,
): VendorReviewSub {
  switch (vendorState) {
    case "approved":
      return { text: vendorId ? `厂商 #${vendorId}` : "厂商编号待同步" }
    case "pending":
      return { text: vendorId ? `厂商审核中 · #${vendorId}` : "提交厂商中…" }
    case "rejected":
      return { text: rejectReason || "厂商未附驳回原因", tone: "verm" }
  }
}
