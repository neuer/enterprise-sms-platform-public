import { ref } from "vue"

import type { ManagedApp } from "../api/apps"
import { getReport, type ReportRow } from "../api/reports"
import { shanghaiDateKey } from "../lib/time"
import { useLatestRead } from "./useLatestRead"

/** 应用管理今日用量联查：完整读完才发布，失败或取消不把部分结果显示为完整用量。 */
export function useAppDailyUsage() {
  /** 今日用量联查结果（dim_value = app.id 字符串）；完整联查成功前单元格显示「—」。 */
  const dailyUsage = ref<Map<string, ReportRow>>(new Map())
  const usageRead = useLatestRead()
  const usageUnavailable = ref(true)

  /** 今日消耗（计费条）：联查成功但无记录为 0；联查失败返回 null，由界面显示「—」。 */
  function consumedOf(app: ManagedApp): number | null {
    if (usageUnavailable.value) return null
    return dailyUsage.value.get(String(app.id))?.total_segments ?? 0
  }

  /** 成功率直接取服务端口径（services/stats.py），前端不自行计算。 */
  function rateOf(app: ManagedApp): number | null {
    if (usageUnavailable.value) return null
    return dailyUsage.value.get(String(app.id))?.success_rate ?? null
  }

  /** 今日用量按应用维度读取有界分页；完整读完后发布，失败不把部分结果显示为完整用量。 */
  async function loadDailyUsage(): Promise<void> {
    const signal = usageRead.start()
    const today = shanghaiDateKey()
    try {
      const usage = new Map<string, ReportRow>()
      const pageSize = 100
      let expectedTotal: number | undefined
      let currentPage = 1
      while (true) {
        const result = await getReport(
          {
            granularity: "day",
            groupBy: "app",
            category: "all",
            start: today,
            end: today,
          },
          { page: currentPage, size: pageSize },
          signal,
        )
        if (signal.aborted) return
        if (
          !Array.isArray(result.items) ||
          !Number.isSafeInteger(result.total) ||
          result.total < 0 ||
          result.size !== pageSize ||
          result.page !== currentPage
        ) {
          throw new Error("用量统计响应无效")
        }
        expectedTotal ??= result.total
        const expectedRows = Math.min(pageSize, expectedTotal - (currentPage - 1) * pageSize)
        if (result.total !== expectedTotal || result.items.length !== expectedRows) {
          throw new Error("用量统计分页不完整")
        }
        for (const row of result.items) {
          if (usage.has(row.dim_value)) throw new Error("用量统计分页存在重复应用")
          usage.set(row.dim_value, row)
        }
        if (currentPage * pageSize >= expectedTotal) break
        currentPage += 1
      }
      if (usage.size !== expectedTotal) throw new Error("用量统计分页不完整")
      dailyUsage.value = usage
      usageUnavailable.value = false
    } catch {
      if (signal.aborted) return
      dailyUsage.value = new Map()
      usageUnavailable.value = true
    }
  }

  return { usageUnavailable, consumedOf, rateOf, loadDailyUsage }
}
