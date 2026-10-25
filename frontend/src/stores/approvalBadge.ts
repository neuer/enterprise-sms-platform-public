import { defineStore } from "pinia"
import { ref } from "vue"

import { listApprovals } from "../api/approvals"
import { usePolling } from "../composables/usePolling"

// 与 views/ApprovalView.vue 的列表轮询间隔同值（30s）；调整需双向同步两处。
const POLL_INTERVAL_MS = 30_000

export const useApprovalBadgeStore = defineStore("approvalBadge", () => {
  const pending = ref(0)
  const epoch = ref(0)
  let requestEpoch = 0

  /** 刷新无筛选的待审批角标；迟到响应和退出后的结果不得回写。 */
  async function refresh(): Promise<void> {
    const current = ++requestEpoch
    const seen = epoch.value
    try {
      const result = await listApprovals({ status: "pending", page: 1, size: 1 })
      if (current !== requestEpoch || seen !== epoch.value) return
      pending.value = result.counts.pending
    } catch {
      // 导航角标只是待办感知辅助，不阻断任何业务路径。
    }
  }

  /** 仅无筛选列表可以复用自己的 pending 计数；筛选结果必须另查全局口径。 */
  function syncFromList(
    count: number,
    filters: { category?: string; dept?: string; q?: string },
    seenEpoch: number,
  ): void {
    if (seenEpoch !== epoch.value) return
    const filtered = Boolean(filters.category || filters.dept?.trim() || filters.q?.trim())
    if (!filtered) {
      pending.value = count
      return
    }
    void refresh()
  }

  function clear(): void {
    epoch.value += 1
    requestEpoch += 1
    pending.value = 0
  }

  // store 非组件作用域：由 App.vue 依据登录态 / 角色 / 审批页路由手动 start / stop。
  const polling = usePolling(refresh, { intervalMs: POLL_INTERVAL_MS, immediate: true })

  return { pending, epoch, refresh, syncFromList, clear, start: polling.start, stop: polling.stop }
})
