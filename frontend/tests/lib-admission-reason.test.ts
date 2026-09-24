import { ApiRequestError } from "../src/api/client"
import { ADMISSION_REASON_TEXT, admissionReasonOf, admissionReasonText } from "../src/lib/admissionReason"

describe("发送准入 reason 映射", () => {
  it("覆盖 send_admission 全部已知 503 reason", () => {
    // 与 backend/app/services/send_admission.py 的产出对齐；新增 reason 必须同步登记
    const known = [
      "queues_paused",
      "queue_paused",
      "realtime_paused",
      "bulk_paused",
      "outbox_backlog",
      "outbox_oldest",
      "outbox_dead",
      "callback_backlog",
      "uncertain_overdue",
      "vendor_failures",
      "snapshot_unavailable",
      "dispatcher_heartbeat_stale",
      "send_lanes_heartbeat_stale",
      "realtime_heartbeat_stale",
      "bulk_heartbeat_stale",
      "degraded_bulk",
      "degraded_volume",
      "recovery_volume",
      "recovery_segment_cost",
    ]
    for (const reason of known) {
      expect(ADMISSION_REASON_TEXT[reason], `${reason} 缺中文解释`).toBeTruthy()
    }
    expect(Object.keys(ADMISSION_REASON_TEXT).sort()).toEqual([...known].sort())
  })

  it("未知 reason 返回 null 由调用方回退通用文案", () => {
    expect(admissionReasonText("future_reason")).toBeNull()
  })

  it("从 503 DEPENDENCY_UNAVAILABLE 的 detail 提取 reason", () => {
    const error = new ApiRequestError(503, "DEPENDENCY_UNAVAILABLE", "发送通道暂时不可用，请稍后重试", {
      reason: "recovery_volume",
    })
    expect(admissionReasonOf(error)).toBe("recovery_volume")
    expect(admissionReasonText(admissionReasonOf(error)!)).toContain("恢复保护")
    expect(admissionReasonOf(new Error("x"))).toBeNull()
    expect(admissionReasonOf(null)).toBeNull()
    expect(admissionReasonOf({ detail: "string-detail" })).toBeNull()
  })
})
