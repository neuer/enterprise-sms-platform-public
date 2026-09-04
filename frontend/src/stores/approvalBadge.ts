import { defineStore } from "pinia"
import { ref } from "vue"

import { listApprovals } from "../api/approvals"
import { usePolling } from "../composables/usePolling"

const POLL_INTERVAL_MS = 30_000

export const useApprovalBadgeStore = defineStore("approvalBadge", () => {
  const pending = ref(0)

  /** 刷新待审批角标计数；失败静默，等待下个轮询周期或审批页同步。 */
  async function refresh(): Promise<void> {
    try {
      const result = await listApprovals({ status: "pending", page: 1, size: 1 })
      pending.value = result.counts.pending
    } catch {
      // 导航角标只是待办感知辅助，不阻断任何业务路径。
    }
  }

  // store 非组件作用域：由 App.vue 依据登录态 / 角色 / 审批页路由手动 start / stop。
  const polling = usePolling(refresh, { intervalMs: POLL_INTERVAL_MS, immediate: true })

  return { pending, refresh, start: polling.start, stop: polling.stop }
})
