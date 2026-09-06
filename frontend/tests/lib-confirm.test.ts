import { ElMessageBox } from "element-plus"
import { h, type VNode } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

import { confirmAction, confirmAuditedAction } from "../src/lib/confirm"

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
    expect(options).toEqual({ type: "warning" })
  })

  it("confirmAction 取消 / 关闭返回 false", async () => {
    vi.spyOn(ElMessageBox, "confirm").mockRejectedValue("cancel" as never)
    expect(await confirmAction({ title: "确认取消", body: "确认？" })).toBe(false)
  })
})
