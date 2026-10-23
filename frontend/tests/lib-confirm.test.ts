import { createPinia, setActivePinia } from "pinia"
import { useSessionStore } from "../src/stores/session"
import { ElMessageBox } from "element-plus"
import { effectScope, h, nextTick, type VNode } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"
import { SESSION_CLEARING_EVENT } from "../src/api/sessionEvents"

import { confirmAction, confirmAuditedAction, useConfirmActions } from "../src/lib/confirm"

/** 递归提取 VNode 文本，断言对话框正文与审计细字。 */
function vnodeText(node: unknown): string {
  if (typeof node === "string") return node
  if (Array.isArray(node)) return node.map(vnodeText).join("")
  if (node && typeof node === "object" && "children" in node) {
    return vnodeText((node as { children: unknown }).children)
  }
  return ""
}

describe("确认对话框单点 lib/confirm", () => {
  afterEach(() => {
    window.dispatchEvent(new Event(SESSION_CLEARING_EVENT))
    ElMessageBox.close()
    vi.restoreAllMocks()
  })

  it("confirmAuditedAction 构造后果段 + 审计提示段的固定结构", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const confirmed = await confirmAuditedAction({
      title: "删除模板",
      body: "确认删除模板「账单通知」？删除后不可恢复。",
      auditNote: "删除行为与操作人将写入审计日志。",
      confirmText: "确认删除",
    })
    expect(confirmed).toBe(true)
    expect(confirm).toHaveBeenCalledTimes(1)
    const [message, title, options] = confirm.mock.calls[0] as [VNode, string, Record<string, unknown>]
    expect(title).toBe("删除模板")
    expect(message.props).toMatchObject({ class: "confirm-dialog" })
    const children = message.children as VNode[]
    expect(children).toHaveLength(2)
    expect(children[1].props).toMatchObject({ class: "confirm-audit-note" })
    expect(vnodeText(message)).toContain("删除后不可恢复")
    expect(vnodeText(message)).toContain("写入审计日志")
    expect(options).toMatchObject({ type: "warning", confirmButtonText: "确认删除", cancelButtonText: "取消" })
  })

  it("body 为 VNode 时原样使用（支持强调与后果列表）", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    await confirmAuditedAction({
      title: "确认恢复双队列",
      body: h("p", ["FORCE 已开启：将", h("strong", "绕过余额与暂停原因守卫"), "。"]),
      auditNote: "恢复行为将写入审计日志。",
      confirmText: "恢复队列",
    })
    const [message] = confirm.mock.calls[0] as unknown as [VNode]
    const children = message.children as VNode[]
    expect(children[0].type).toBe("p")
    expect(vnodeText(children[0])).toBe("FORCE 已开启：将绕过余额与暂停原因守卫。")
  })

  it("danger 为 false 时使用 info 图标（启用等非破坏操作）", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    await confirmAuditedAction({
      title: "确认启用账号",
      body: "将重新允许登录平台。",
      auditNote: "启用行为将写入审计日志。",
      confirmText: "启用",
      danger: false,
    })
    const [, , options] = confirm.mock.calls[0] as [unknown, unknown, Record<string, unknown>]
    expect(options.type).toBe("info")
  })

  it.each(["cancel", "close"])("操作者 %s 时吞掉 rejection 并返回 false", async (reason) => {
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue(reason as never)
    const confirmed = await confirmAuditedAction({
      title: "确认停用",
      body: "停用后不可登录。",
      auditNote: "停用行为将写入审计日志。",
      confirmText: "确认停用",
    })
    expect(confirmed).toBe(false)
  })

  it("confirmAction 透传纯文本与按钮文案，确认返回 true", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    const confirmed = await confirmAction({
      title: "确认取消",
      body: "取消批次 B2024001？配额将按规则回补。",
      confirmText: "确认取消",
      cancelText: "保留",
    })
    expect(confirmed).toBe(true)
    const [message, title, options] = confirm.mock.calls[0] as [string, string, Record<string, unknown>]
    expect(message).toBe("取消批次 B2024001？配额将按规则回补。")
    expect(title).toBe("确认取消")
    expect(options).toMatchObject({ type: "warning", confirmButtonText: "确认取消", cancelButtonText: "保留" })
  })

  it("confirmAction 未给按钮文案时不覆盖 Element 语言包默认", async () => {
    const confirm = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm" as never)
    await confirmAction({ title: "确认重发", body: "失败号码将生成新批次。" })
    const [, , options] = confirm.mock.calls[0] as [unknown, unknown, Record<string, unknown>]
    expect(options).toMatchObject({ type: "warning" })
    expect(options.confirmButtonText).toBeUndefined()
    expect(options.cancelButtonText).toBeUndefined()
    expect(String(options.customClass)).toMatch(/^confirm-request confirm-owned-\d+$/)
  })

  it("confirmAction 取消 / 关闭返回 false", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel" as never)
    expect(await confirmAction({ title: "确认取消", body: "确认？" })).toBe(false)
  })
})

it.each(["session", "dispose", "target"])("确认期间 %s 改变会撤销旧确认", async (change) => {
  setActivePinia(createPinia())
  const scope = effectScope()
  const actions = scope.run(useConfirmActions)!
  let resolve!: (value: never) => void
  vi.spyOn(ElMessageBox, "confirm").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }) as never,
  )
  let current = true
  const pending = actions.confirmAction({ title: "合成", body: "合成", isCurrent: () => current })
  if (change === "session") useSessionStore().clear()
  if (change === "dispose") scope.stop()
  if (change === "target") current = false
  resolve("confirm" as never)
  expect(await pending).toBe(false)
  scope.stop()
  vi.restoreAllMocks()
})

it("真实确认框在会话清理时立即关闭，且不调用全局 close", async () => {
  setActivePinia(createPinia())
  const closeAll = vi.spyOn(ElMessageBox, "close")
  const scope = effectScope()
  const actions = scope.run(useConfirmActions)!
  const prompt = ElMessageBox.prompt("请输入当前密码", "二次认证")
  const pending = actions.confirmAuditedAction({
    title: "确认取消",
    body: "取消批次 BATCH-OLD？配额将按规则回补。",
    auditNote: "取消行为与操作人将写入审计日志。",
    confirmText: "确认取消",
  })
  await nextTick()
  expect(document.body.textContent).toContain("取消批次 BATCH-OLD")
  expect(document.body.textContent).toContain("请输入当前密码")
  useSessionStore().clear()
  expect(await pending).toBe(false)
  expect(document.body.textContent).not.toContain("BATCH-OLD")
  expect(document.body.textContent).toContain("请输入当前密码")
  expect(closeAll).not.toHaveBeenCalled()

  useSessionStore().role = "admin"
  const next = actions.confirmAction({ title: "新会话", body: "新的确认 BATCH-NEW 仍然打开" })
  await nextTick()
  expect(document.body.textContent).toContain("BATCH-NEW")
  await nextTick()
  expect(document.body.textContent).toContain("BATCH-NEW")
  expect(closeAll).not.toHaveBeenCalled()
  scope.stop()
  await next
  prompt.catch(() => undefined)
  ElMessageBox.close()
})

it("页面销毁只取消本页确认和待显示队列，另一页的确认继续显示", async () => {
  const scopeA = effectScope()
  const scopeB = effectScope()
  const actionsA = scopeA.run(useConfirmActions)!
  const actionsB = scopeB.run(useConfirmActions)!
  const pendingA = actionsA.confirmAction({ title: "甲", body: "批次甲的业务文案" })
  const pendingB = actionsB.confirmAction({ title: "乙", body: "批次乙的业务文案" })
  await nextTick()
  expect(document.body.textContent).toContain("批次甲的业务文案")
  expect(document.body.textContent).not.toContain("批次乙的业务文案")
  scopeA.stop()
  expect(await pendingA).toBe(false)
  await nextTick()
  expect(document.body.textContent).not.toContain("批次甲的业务文案")
  expect(document.body.textContent).toContain("批次乙的业务文案")
  scopeB.stop()
  expect(await pendingB).toBe(false)
  await nextTick()
  expect(document.body.textContent).not.toContain("批次乙的业务文案")
})
