import { defaultSessionDocument, type SessionOperationOrigin } from "../api/sessionDocument"
import { SESSION_CLEARING_EVENT } from "../api/sessionEvents"
import { ElMessageBox, type ElMessageBoxOptions } from "element-plus"
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

interface ConfirmRequest {
  id: number
  origin: SessionOperationOrigin
  settled: boolean
  /** 仅在视图存活期间持有；关闭或退役时立刻丢掉，避免业务文案残留。 */
  message: string | VNode | null
  resolve: (value: boolean) => void
  owner: Set<ConfirmRequest> | null
}

interface QueuedConfirm {
  request: ConfirmRequest
  run: () => void
}

let nextConfirmId = 1
let active: ConfirmRequest | null = null
const live = new Set<ConfirmRequest>()
const queue: QueuedConfirm[] = []
const ownerStack: Array<Set<ConfirmRequest>> = []

function confirmMarker(id: number): string {
  return `confirm-owned-${id}`
}

function takeOwner(): Set<ConfirmRequest> | null {
  return ownerStack[ownerStack.length - 1] ?? null
}

function dropMessage(request: ConfirmRequest): void {
  request.message = null
}

/**
 * 只拆除本请求的确认视图。禁止 ElMessageBox.close()：那会关掉密码框和新会话的对话框。
 */
function dismissOwnedBox(id: number): void {
  if (typeof document === "undefined") return
  const box = document.querySelector(`.el-message-box.${confirmMarker(id)}`)
  if (!box) return
  box.querySelector(".el-message-box__message")?.replaceChildren()
  const title = box.querySelector(".el-message-box__title span")
  if (title) title.textContent = ""
  const overlay = box.closest(".el-overlay")
  if (overlay instanceof HTMLElement) {
    overlay.style.display = "none"
    overlay.setAttribute("aria-hidden", "true")
    overlay.inert = true
  }
  const close = box.querySelector<HTMLButtonElement>(".el-message-box__headerbtn")
  try {
    close?.click()
  } catch {
    // 节点已拆掉时不再抛给调用方。
  }
}

function pump(): void {
  if (active && !active.settled) return
  active = null
  while (queue.length) {
    const next = queue.shift()
    if (!next || next.request.settled) continue
    active = next.request
    next.run()
    return
  }
}

function settle(request: ConfirmRequest, value: boolean, pumpNext = true): void {
  if (request.settled) return
  request.settled = true
  dropMessage(request)
  live.delete(request)
  request.owner?.delete(request)
  const wasActive = active === request
  if (wasActive) active = null
  request.resolve(value)
  if (wasActive && pumpNext) pump()
}

/** 按请求集合关闭视图并静默结束 Promise；未列入的对话框保持不动。 */
function cancelRequests(requests: Iterable<ConfirmRequest>): void {
  const doomed = new Set(requests)
  if (!doomed.size) return
  for (let index = queue.length - 1; index >= 0; index -= 1) {
    if (doomed.has(queue[index].request)) queue.splice(index, 1)
  }
  const boxes = [...doomed].map((request) => request.id)
  for (const request of doomed) dropMessage(request)
  const activeDoomed = active !== null && doomed.has(active)
  for (const request of doomed) settle(request, false, false)
  if (activeDoomed) active = null
  for (const id of boxes) dismissOwnedBox(id)
  pump()
}

function handleSessionClearing(): void {
  // 只结束已经过代际的请求。清理事件之后才打开的新会话对话框不在此列。
  cancelRequests([...live].filter((request) => !defaultSessionDocument.isOriginCurrent(request.origin)))
}

if (typeof window !== "undefined") {
  window.addEventListener(SESSION_CLEARING_EVENT, handleSessionClearing)
}

function present(
  message: string | VNode,
  title: string,
  options: ElMessageBoxOptions,
  origin: SessionOperationOrigin,
): Promise<boolean> {
  const owner = takeOwner()
  const request: ConfirmRequest = {
    id: nextConfirmId,
    origin,
    settled: false,
    message,
    resolve: () => undefined,
    owner,
  }
  nextConfirmId += 1
  live.add(request)
  owner?.add(request)
  return new Promise<boolean>((resolve) => {
    request.resolve = resolve
    const run = (): void => {
      if (request.settled) return
      const boxed = request.message
      dropMessage(request)
      if (boxed === null) {
        settle(request, false)
        return
      }
      try {
        const pending = ElMessageBox.confirm(boxed, title, {
          ...options,
          customClass: ["confirm-request", confirmMarker(request.id), options.customClass].filter(Boolean).join(" "),
        })
        void Promise.resolve(pending).then(
          () => settle(request, defaultSessionDocument.isOriginCurrent(request.origin)),
          () => settle(request, false),
        )
      } catch {
        settle(request, false)
      }
    }
    if (active && !active.settled) queue.push({ request, run })
    else {
      active = request
      run()
    }
  })
}

/**
 * 无审计段的纯文本确认框：确认返回 true，取消 / 关闭吞掉 rejection 返回 false。
 * 调用形态：`if (!(await confirmAction({ … }))) return`，业务错误在后续 try/catch 单独处理。
 */
export async function confirmAction(options: ConfirmActionOptions): Promise<boolean> {
  const origin = defaultSessionDocument.captureOrigin()
  const box: ElMessageBoxOptions = { type: "warning" }
  if (options.confirmText) box.confirmButtonText = options.confirmText
  if (options.cancelText) box.cancelButtonText = options.cancelText
  return present(options.body, options.title, box, origin)
}

/**
 * 审计确认对话框单点：后果说明 + 「写入审计日志」提示段的固定两段式结构
 * （confirm-dialog / confirm-audit-note 样式见 styles/workspace 分片），
 * 确认返回 true，取消 / 关闭吞掉 rejection 返回 false，调用点不再重复 cancel/close 过滤。
 */
export async function confirmAuditedAction(options: ConfirmAuditedActionOptions): Promise<boolean> {
  const origin = defaultSessionDocument.captureOrigin()
  const message = h("div", { class: "confirm-dialog" }, [
    typeof options.body === "string" ? h("p", options.body) : options.body,
    h("p", { class: "confirm-audit-note" }, options.auditNote),
  ])
  return present(
    message,
    options.title,
    {
      type: options.danger === false ? "info" : "warning",
      ...(options.confirmText ? { confirmButtonText: options.confirmText } : {}),
      cancelButtonText: options.cancelText ?? "取消",
    },
    origin,
  )
}

/** 将确认绑定发起页面和原会话；调用方可补充目标/草稿是否仍匹配。 */
export function useConfirmActions() {
  let disposed = false
  const mine = new Set<ConfirmRequest>()
  if (getCurrentScope()) {
    onScopeDispose(() => {
      disposed = true
      cancelRequests(mine)
    })
  }
  async function guarded<T extends { isCurrent?: () => boolean }>(
    show: (options: T) => Promise<boolean>,
    options: T,
  ): Promise<boolean> {
    if (disposed) return false
    const origin = defaultSessionDocument.captureOrigin()
    ownerStack.push(mine)
    let pending: Promise<boolean>
    try {
      pending = show(options)
    } finally {
      ownerStack.pop()
    }
    const confirmed = await pending
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
