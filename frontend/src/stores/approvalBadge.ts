import { defineStore } from "pinia"
import { ref } from "vue"

import { listApprovals } from "../api/approvals"

const POLL_INTERVAL_MS = 30_000

export const useApprovalBadgeStore = defineStore("approvalBadge", () => {
  const pending = ref(0)
  let timer: number | undefined

  /** 刷新待审批角标计数；失败静默，等待下个轮询周期或审批页同步。 */
  async function refresh(): Promise<void> {
    try {
      const result = await listApprovals({ status: "pending", page: 1, size: 1 })
      pending.value = result.counts.pending
    } catch {
      // 导航角标只是待办感知辅助，不阻断任何业务路径。
    }
  }

  function start(): void {
    void refresh()
    if (timer !== undefined) return
    timer = window.setInterval(() => void refresh(), POLL_INTERVAL_MS)
  }

  function stop(): void {
    if (timer === undefined) return
    window.clearInterval(timer)
    timer = undefined
  }

  return { pending, refresh, start, stop }
})
