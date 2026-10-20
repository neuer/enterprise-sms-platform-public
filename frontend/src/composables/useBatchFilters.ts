import { computed, ref, watch, type Ref } from "vue"

import { listApps, type ManagedApp } from "../api/apps"
import type { BatchFilters } from "../api/queries"
import { CATEGORY_OPTIONS, STATUS_LABELS } from "../lib/labels"
import { rangeToIsoParams, toApiDateTime } from "../lib/time"
import { useLatestRead } from "./useLatestRead"

// 状态分组为前端推导；各组计数来自服务端分面 status_counts（不含状态条件本身）。
// 单状态组的中文名直接引用 STATUS_LABELS 单点；「全部 / 进行中 / 其他终态」为多状态聚合组，lib 无对应键，保留局部文案。
const statusGroups = [
  { key: "all", label: "全部", statuses: [] as string[] },
  { key: "active", label: "进行中", statuses: ["queued", "sending"] },
  { key: "pending_approval", label: STATUS_LABELS.pending_approval, statuses: ["pending_approval"] },
  { key: "scheduled", label: STATUS_LABELS.scheduled, statuses: ["scheduled"] },
  { key: "balance_blocked", label: STATUS_LABELS.balance_blocked, statuses: ["balance_blocked"] },
  { key: "completed", label: STATUS_LABELS.completed, statuses: ["completed"] },
  { key: "completed_unknown", label: STATUS_LABELS.completed_unknown, statuses: ["completed_unknown"] },
  { key: "closed", label: "其他终态", statuses: ["cancelled", "rejected", "expired"] },
]

const categoryOptions = [{ label: "全部", value: "" }, ...CATEGORY_OPTIONS]
const channelOptions = [
  { label: "全部", value: "" },
  { label: "API", value: "api" },
  { label: "Web", value: "web" },
]
const isTestOptions = [
  { label: "全部", value: "" },
  { label: "正式", value: "false" },
  { label: "测试", value: "true" },
]

/**
 * 批次列表筛选状态：筛选字段、状态分组计数、条件变更提示与管理员应用下拉懒加载。
 * 查询时序（分页、提交、重置）仍由页面处理。
 */
export function useBatchFilters(isAdmin: Ref<boolean>) {
  const statusCounts = ref<Record<string, number> | null>(null)
  const category = ref("")
  const statusGroup = ref("all")
  const channel = ref("")
  const batchNo = ref("")
  const range = ref<[Date, Date] | null>(null)
  // 更多筛选（低频字段，收进气泡）
  const isTest = ref("") // "" | "false" | "true"
  const appId = ref("")
  const dept = ref("")
  const moreOpen = ref(false)
  const appOptions = ref<ManagedApp[]>([])
  const appliedFiltersKey = ref("")

  function groupCount(key: string, statuses: string[]): number | null {
    if (statusCounts.value === null) return null
    if (key === "all") return Object.values(statusCounts.value).reduce((sum, n) => sum + n, 0)
    return statuses.reduce((sum, status) => sum + (statusCounts.value?.[status] ?? 0), 0)
  }

  const statusChipOptions = computed(() =>
    statusGroups.map((group) => ({
      value: group.key,
      label: group.label,
      count: groupCount(group.key, group.statuses),
      class: group.key === "balance_blocked" && (groupCount(group.key, group.statuses) ?? 0) > 0 ? "hot" : undefined,
    })),
  )

  const currentFiltersKey = computed(() =>
    JSON.stringify([
      batchNo.value.trim(),
      category.value,
      channel.value,
      isTest.value,
      appId.value.trim(),
      isAdmin.value ? dept.value.trim() : "",
      range.value?.[0] ? toApiDateTime(range.value[0]) : "",
      range.value?.[1] ? toApiDateTime(range.value[1]) : "",
    ]),
  )
  const filtersDirty = computed(
    () => appliedFiltersKey.value !== "" && currentFiltersKey.value !== appliedFiltersKey.value,
  )
  const moreActiveCount = computed(
    () =>
      (isTest.value !== "" ? 1 : 0) +
      (appId.value.trim() !== "" ? 1 : 0) +
      (isAdmin.value && dept.value.trim() !== "" ? 1 : 0),
  )
  const moreActive = computed(() => moreActiveCount.value > 0)
  const filtersCollapsed = ref(true)
  const collapsedActiveCount = computed(
    () => (category.value ? 1 : 0) + (channel.value ? 1 : 0) + (range.value ? 1 : 0) + moreActiveCount.value,
  )

  /** 按当前筛选组装批次列表查询参数。 */
  function listQuery(page: number): BatchFilters {
    const group = statusGroups.find((item) => item.key === statusGroup.value) ?? statusGroups[0]
    return {
      page,
      category: category.value || undefined,
      status: group.statuses.length ? group.statuses.join(",") : undefined,
      is_test: isTest.value === "" ? undefined : isTest.value === "true",
      channel: channel.value || undefined,
      app_id: appId.value.trim() ? Number(appId.value.trim()) : undefined,
      dept: isAdmin.value && dept.value.trim() ? dept.value.trim() : undefined,
      batch_no: batchNo.value.trim() || undefined,
      ...rangeToIsoParams(range.value),
    }
  }

  /** 清空全部筛选字段（不触发查询）。 */
  function clearFilters(): void {
    category.value = ""
    statusGroup.value = "all"
    channel.value = ""
    batchNo.value = ""
    isTest.value = ""
    appId.value = ""
    dept.value = ""
    range.value = null
  }

  // 应用下拉仅管理员可用（应用管理接口为管理员域）；懒加载，首次打开更多筛选时拉取
  const appsRead = useLatestRead()
  let appsRequested = false
  watch(moreOpen, (open) => {
    if (!open || appsRequested || !isAdmin.value) return
    appsRequested = true
    const signal = appsRead.start()
    listApps(signal)
      .then((apps) => {
        if (!signal.aborted) appOptions.value = apps.filter((app) => app.status === 1)
      })
      .catch(() => {
        if (!signal.aborted) appOptions.value = []
      })
  })

  return {
    statusCounts,
    category,
    statusGroup,
    channel,
    batchNo,
    range,
    isTest,
    appId,
    dept,
    moreOpen,
    appOptions,
    appliedFiltersKey,
    categoryOptions,
    channelOptions,
    isTestOptions,
    statusChipOptions,
    currentFiltersKey,
    filtersDirty,
    moreActiveCount,
    moreActive,
    filtersCollapsed,
    collapsedActiveCount,
    listQuery,
    clearFilters,
  }
}
