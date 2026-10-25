import { effectScope, nextTick } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

const downloadImportInvalidFile = vi.hoisted(() => vi.fn())
const sendWebMessage = vi.hoisted(() => vi.fn())
const saveBlob = vi.hoisted(() => vi.fn())

vi.mock("../src/api/webMessages", () => ({
  downloadImportInvalidFile,
  sendWebMessage,
  uploadPhones: vi.fn(),
  previewBilling: vi.fn(),
}))
vi.mock("../src/api/signs", () => ({ listSigns: vi.fn(async () => []) }))
vi.mock("../src/api/templates", () => ({ listTemplates: vi.fn(async () => []) }))
vi.mock("../src/lib/download", () => ({ saveBlob }))

import { useSendDraft } from "../src/views/send/useSendDraft"

function imported(importId: string) {
  return {
    import_id: importId,
    valid: 1,
    invalid: 1,
    duplicate: 0,
    blacklisted: 0,
    invalid_download_url: `/api/v1/web/messages/import/${importId}/invalid-file`,
    expires_at: "2026-10-26T08:00:00+08:00",
    status: "ready" as const,
    error: null,
  }
}

function mountDraft() {
  const scope = effectScope()
  const draft = scope.run(() => useSendDraft())!
  return { scope, draft }
}

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe("发送草稿生命周期", () => {
  it("定时跨过当前时刻后禁用发送，提交瞬间再次用当前时间拦截", async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date("2026-10-25T10:00:00+08:00"))
    const { scope, draft } = mountDraft()
    draft.form.content = "通知"
    draft.form.mobilesText = "13800138000"
    draft.form.scheduleEnabled = true
    draft.form.scheduledAt = "2026-10-25T10:00:02+08:00"
    await nextTick()
    expect(draft.scheduleInPast.value).toBe(false)

    vi.setSystemTime(new Date("2026-10-25T10:00:03+08:00"))
    await draft.submit()
    expect(sendWebMessage).not.toHaveBeenCalled()
    expect(draft.errorMessage.value).toContain("未来")

    await vi.advanceTimersByTimeAsync(1000)
    expect(draft.scheduleInPast.value).toBe(true)
    expect(draft.sendDisabled.value).toBe(true)
    scope.stop()
  })

  it("已消费导入包在实质编辑后不能再作为新批次提交", async () => {
    const { scope, draft } = mountDraft()
    draft.form.source = "import"
    draft.form.content = "内容A"
    draft.imported.value = imported("imp-a")
    sendWebMessage.mockResolvedValue({
      batch_no: "B1",
      status: "scheduled",
      idempotent: false,
      scheduled_at: "2026-10-26T09:00:00+08:00",
      accepted_count: 1,
      rejected_count: 0,
      approval_required: false,
    })
    await draft.submit()
    expect(sendWebMessage).toHaveBeenCalledTimes(1)
    expect(draft.submittedSummary.value).toContain("2026-10-26")

    draft.form.content = "内容B"
    draft.form.scheduledAt = "2026-10-27T09:00:00+08:00"
    await nextTick()
    expect(draft.imported.value).toBeNull()
    expect(draft.consumedImportNotice.value).toContain("重新上传")
    await draft.submit()
    expect(sendWebMessage).toHaveBeenCalledTimes(1)
    scope.stop()
  })

  it("剔除清单的迟到响应在重置后不写文件也不报内部错误", async () => {
    let release: (value: Blob) => void = () => undefined
    downloadImportInvalidFile.mockImplementation(
      () =>
        new Promise((resolve) => {
          release = resolve
        }),
    )
    const { scope, draft } = mountDraft()
    draft.imported.value = imported("imp-a")
    const pending = draft.downloadInvalidFile()
    draft.resetImport()
    release(new Blob(["mask"]))
    await pending
    expect(saveBlob).not.toHaveBeenCalled()
    expect(draft.errorMessage.value).toBe("")
    scope.stop()
  })
})
