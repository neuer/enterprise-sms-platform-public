import type { SmsSign } from "../api/signs"

/** 已绑定厂商编号才可同步审核状态。 */
export const canSyncSign = (item: SmsSign): boolean => item.vendor_sign_id !== null

/** 关联已有厂商签名：仅可写角色、待审核且尚未绑定厂商编号。 */
export const canAdoptSign = (item: SmsSign, canWrite: boolean): boolean =>
  canWrite && item.vendor_state === "pending" && item.vendor_sign_id === null

/** 仅厂商驳回的签名可修改重交。 */
export const canEditSign = (item: SmsSign): boolean => item.vendor_state === "rejected"

/** 已通过审核的签名不可删除。 */
export const canDeleteSign = (item: SmsSign): boolean => item.vendor_state !== "approved"
