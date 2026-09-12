import { defaultSessionDocument } from "../api/sessionDocument"
import { ElMessageBox } from "element-plus"
import { getCurrentScope, onScopeDispose, h, type VNode } from "vue"

export interface ConfirmActionOptions {
  /** 对话框标题。 */
  title: string
  isCurrent?: () => boolean
  /** 后果说明：纯文本（自动包裹为段落）。 */
  body: string
  /** 确认按钮文案；缺省沿用 Element 语言包默认。 */
  confirmText?: string
  /** 取消按钮文案；缺省沿用 Element 语言包默认。 */
  cancelText?: string
}

export interface ConfirmAuditedActionOptions extends Omit<ConfirmActionOptions, "body" | "cancelText"> {
  /** 后果说明：纯文本（自动包裹为段落）或完整 h() 片段（如含强调、后果列表）。 */
  body: string | VNode
  /** 审计提示段落文案（「…写入审计日志」），固定以 confirm-audit-note 样式呈现。 */
  auditNote: string
  /** 取消按钮文案；缺省「取消」。 */
  cancelText?: string
  /** 高危操作使用 warning 图标（缺省 true）；非破坏操作（如启用）传 false 使用 info 图标。 */
  danger?: boolean
}

/**
 * 无审计段的纯文本确认框：确认返回 true，取消 / 关闭吞掉 rejection 返回 false。
 * 调用形态：`if (!(await confirmAction({ … }))) return`，业务错误在后续 try/catch 单独处理。
 */
export async function confirmAction(options: ConfirmActionOptions): Promise<boolean> {
  try {
    await ElMessageBox.confirm(options.body, options.title, {
      type: "warning",
      ...(options.confirmText ? { confirmButtonText: options.confirmText } : {}),
      ...(options.cancelText ? { cancelButtonText: options.cancelText } : {}),
    })
    return true
  } catch {
    return false
  }
}

/**
 * 审计确认对话框单点：后果说明 + 「写入审计日志」提示段的固定两段式结构
 * （confirm-dialog / confirm-audit-note 样式见 styles/workspace 分片），
 * 确认返回 true，取消 / 关闭吞掉 rejection 返回 false，调用点不再重复 cancel/close 过滤。
 */
export async function confirmAuditedAction(options: ConfirmAuditedActionOptions): Promise<boolean> {
  const message = h("div", { class: "confirm-dialog" }, [
    typeof options.body === "string" ? h("p", options.body) : options.body,
    h("p", { class: "confirm-audit-note" }, options.auditNote),
  ])
  try {
    await ElMessageBox.confirm(message, options.title, {
      type: options.danger === false ? "info" : "warning",
      ...(options.confirmText ? { confirmButtonText: options.confirmText } : {}),
      cancelButtonText: options.cancelText ?? "取消",
    })
    return true
  } catch {
    return false
  }
}

/** 将确认绑定发起页面和原会话；调用方可补充目标/草稿是否仍匹配。 */
export function useConfirmActions() {
  let disposed = false
  if (getCurrentScope())
    onScopeDispose(() => {
      disposed = true
    })
  async function guarded<T extends { isCurrent?: () => boolean }>(
    show: (options: T) => Promise<boolean>,
    options: T,
  ): Promise<boolean> {
    const origin = defaultSessionDocument.captureOrigin()
    const confirmed = await show(options)
    return confirmed && !disposed && defaultSessionDocument.isOriginCurrent(origin) && (options.isCurrent?.() ?? true)
  }
  return {
    captureCurrent: () => {
      const origin = defaultSessionDocument.captureOrigin()
      return () => !disposed && defaultSessionDocument.isOriginCurrent(origin)
    },
    confirmAction: (options: ConfirmActionOptions) => guarded(confirmAction, options),
    confirmAuditedAction: (options: ConfirmAuditedActionOptions) => guarded(confirmAuditedAction, options),
  }
}
