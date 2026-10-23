import { ElMessage } from "element-plus"
import { afterEach, describe, expect, it, vi } from "vitest"

const opsApi = vi.hoisted(() => ({
  getQueueStatus: vi.fn(),
  resumeQueue: vi.fn(),
}))
vi.mock("../src/api/ops", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/api/ops")>()
  return { ...actual, ...opsApi }
})

const confirm = vi.hoisted(() => ({ confirmAuditedAction: vi.fn() }))
vi.mock("../src/lib/confirm", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/confirm")>()
  return { ...actual, useConfirmActions: () => confirm }
})

import { useOpsQueue } from "../src/composables/useOpsQueue"

const blocked = {
  realtime_code: "999",
  bulk_code: null,
} as Awaited<ReturnType<typeof opsApi.getQueueStatus>>

describe("运维队列 useOpsQueue", () => {
  afterEach(() => {
    opsApi.getQueueStatus.mockReset()
    opsApi.resumeQueue.mockReset()
    confirm.confirmAuditedAction.mockReset()
  })

  it("加载失败保留错误文案并结束加载", async () => {
    opsApi.getQueueStatus.mockRejectedValue(new Error("队列状态不可用"))
    const queue = useOpsQueue()
    await queue.load()
    expect(queue.errorMessage.value).toBe("队列状态不可用")
    expect(queue.loading.value).toBe(false)
    expect(queue.queue.value).toBeNull()
  })

  it("双队列都在运行时不弹出恢复确认", async () => {
    opsApi.getQueueStatus.mockResolvedValue({ realtime_code: null, bulk_code: null } as typeof blocked)
    const queue = useOpsQueue()
    await queue.load()
    await queue.recover()
    expect(confirm.confirmAuditedAction).not.toHaveBeenCalled()
    expect(opsApi.resumeQueue).not.toHaveBeenCalled()
  })

  it("取消确认不调用恢复接口", async () => {
    opsApi.getQueueStatus.mockResolvedValue(blocked)
    confirm.confirmAuditedAction.mockResolvedValue(false)
    const queue = useOpsQueue()
    await queue.load()
    await queue.recover()
    expect(opsApi.resumeQueue).not.toHaveBeenCalled()
    expect(queue.recovering.value).toBe(false)
    expect(queue.queueRecovered.value).toBe(false)
  })

  it("恢复失败提示错误并解除在途守卫", async () => {
    opsApi.getQueueStatus.mockResolvedValue(blocked)
    confirm.confirmAuditedAction.mockResolvedValue(true)
    opsApi.resumeQueue.mockRejectedValue(new Error("余额不足"))
    const error = vi.spyOn(ElMessage, "error").mockImplementation(() => ({}) as never)
    const queue = useOpsQueue()
    await queue.load()
    await queue.recover()
    expect(error).toHaveBeenCalledWith("余额不足")
    expect(queue.recovering.value).toBe(false)
    expect(queue.queueRecovered.value).toBe(false)
  })
})
