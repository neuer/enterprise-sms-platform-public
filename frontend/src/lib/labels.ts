/**
 * 跨页面共享文案单点：消息类别、角色、分页默认值。
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
