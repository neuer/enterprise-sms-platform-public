<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, nextTick, onMounted, ref, watch } from "vue"
import { useRoute, useRouter } from "vue-router"

import {
  createUnmatchedExport,
  getCurrentAlerts,
  getOutboxStatus,
  getQueueStatus,
  listAlerts,
  listJobs,
  listOutboxEvents,
  listRawLogs,
  listUncertain,
  listUnmatched,
  proposeUncertainResolution,
  confirmUncertainResolution,
  replayRaw,
  resumeQueue,
  retryOutboxEvent,
  triggerJob,
  type AlertItem,
  type CurrentAlertItem,
  type CurrentAlertSnapshot,
  type JobItem,
  type OutboxEventItem,
  type OutboxState,
  type OutboxStats,
  type QueueStatus,
  type RawLogItem,
  type UncertainItem,
  type UncertainResolutionAction,
  type UnmatchedItem,
} from "../api/ops"
import { jobDescription } from "../lib/jobDescriptions"
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { PHONE_RE } from "../lib/phone"
import { formatDateTime } from "../lib/time"
import {
  downloadExport,
  getExportTask,
  issueExportStepUp,
  type ExportTask,
} from "../api/reports"
import EmptyState from "../components/EmptyState.vue"
import PhoneMask from "../components/PhoneMask.vue"
import StatusTag from "../components/StatusTag.vue"
import { usePolling } from "../composables/usePolling"
import { useSessionStore } from "../stores/session"
import CallbackView from "./CallbackView.vue"

type TabName = "alerts" | "callbacks" | "raw" | "uncertain" | "unmatched" | "jobs" | "queue" | "outbox"
type AlertMode = "current" | "history"
const OPS_TABS: TabName[] = ["alerts", "callbacks", "raw", "uncertain", "unmatched", "jobs", "queue", "outbox"]
const OPS_TAB_ITEMS: { name: TabName; label: string }[] = [
  { name: "alerts", label: "告警" },
  { name: "callbacks", label: "回调任务" },
  { name: "raw", label: "原始报文" },
  { name: "uncertain", label: "结果未知" },
  { name: "unmatched", label: "无主报告" },
  { name: "jobs", label: "任务健康" },
  { name: "queue", label: "队列恢复" },
  { name: "outbox", label: "Outbox 投递" },
]
const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const RESOLUTION_ACTIONS: { action: UncertainResolutionAction; label: string }[] = [
  { action: "confirm_accepted", label: "确认已受理" },
  { action: "confirm_not_accepted", label: "确认未受理" },
  { action: "keep_unknown", label: "保持未知" },
  { action: "resend_new_batch", label: "新批次重发" },
]

function tabFromQuery(raw: unknown): TabName | null {
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== "string") return null
  return OPS_TABS.includes(value as TabName) ? value as TabName : null
}

const activeTab = ref<TabName>(tabFromQuery(route.query.tab) ?? "alerts")
const visitedTabs = ref<TabName[]>([activeTab.value])
const loading = ref(false)
const errorMessage = ref("")
const alerts = ref<AlertItem[]>([])
const alertMode = ref<AlertMode>("current")
const currentAlerts = ref<CurrentAlertSnapshot | null>(null)
const alertPage = ref(1)
const alertTotal = ref(0)
const alertType = ref("")
const alertLevel = ref<"" | AlertItem["level"]>("")
const alertRange = ref<[Date, Date] | null>(null)
const rawLogs = ref<RawLogItem[]>([])
const rawPage = ref(1)
const rawTotal = ref(0)
const rawSource = ref<"" | RawLogItem["source"]>("")
const rawProcessed = ref<"" | "true" | "false">("")
const uncertain = ref<UncertainItem[]>([])
const uncertainPage = ref(1)
const uncertainTotal = ref(0)
const unmatched = ref<UnmatchedItem[]>([])
const unmatchedPage = ref(1)
const unmatchedTotal = ref(0)
const jobs = ref<JobItem[]>([])
const queue = ref<QueueStatus | null>(null)
const unmatchedPhone = ref("")
const unmatchedRange = ref<[Date, Date] | null>(null)
const exportDecrypted = ref(false)
const exportTask = ref<ExportTask | null>(null)
const exportBusy = ref(false)
const exportError = ref("")
const forceResume = ref(false)
const queueRecovered = ref(false)
const outboxStats = ref<OutboxStats | null>(null)
const outboxEvents = ref<OutboxEventItem[]>([])
const outboxPage = ref(1)
const outboxTotal = ref(0)
const outboxState = ref<"" | OutboxState>("")
const retryingOutboxId = ref<string | null>(null)
const selectedAlert = ref<AlertItem | null>(null)
const alertDetailVisible = ref(false)
const queueBlocked = computed(() => Boolean(queue.value?.realtime_code || queue.value?.bulk_code))

// 当前告警 60s 轮询：仅在告警页签的「当前」视图运转，页面隐藏自动暂停。
const currentAlertPolling = usePolling(() => load("alerts"), {
  intervalMs: 60_000,
  enabled: computed(() => activeTab.value === "alerts" && alertMode.value === "current"),
})

// 对账导出状态轮询：2s 间隔、终态自停；150 次（≈5 分钟）仍未完成给出兜底超时提示。
const EXPORT_POLL_MAX_ATTEMPTS = 150

/** 查询一次导出任务状态；终态或查询失败返回 true 停止轮询。 */
async function pollExportTask(): Promise<boolean> {
  if (!exportTask.value) return true
  try {
    exportTask.value = await getExportTask(exportTask.value.id)
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出状态查询失败"
    return true
  }
  return exportTask.value.status !== "pending" && exportTask.value.status !== "running"
}

const exportPolling = usePolling(pollExportTask, {
  intervalMs: 2_000,
  maxAttempts: EXPORT_POLL_MAX_ATTEMPTS,
  onTimeout: () => {
    exportError.value = "导出状态查询超时（已超过 5 分钟），请稍后重新发起导出"
  },
})

// 与服务端 Query(pattern=^1\d{10}$) 同一规则（硬性规则 8）；服务端仍为权威校验。

const OUTBOX_STATE_META: Record<OutboxState, { label: string; tag: "info" | "warning" | "success" | "danger" }> = {
  dead: { label: "死信", tag: "danger" },
  pending: { label: "待投递", tag: "warning" },
  leased: { label: "已租约", tag: "info" },
  published: { label: "已发布", tag: "info" },
  processing: { label: "处理中", tag: "info" },
  completed: { label: "已完成", tag: "success" },
}
const OUTBOX_STATE_OPTIONS = (Object.keys(OUTBOX_STATE_META) as OutboxState[]).map((value) => ({
  value,
  label: OUTBOX_STATE_META[value].label,
}))

const ALERT_LEVEL_LABELS: Record<AlertItem["level"], string> = {
  info: "提示",
  warn: "警告",
  crit: "严重",
}
const ALERT_LEVEL_OPTIONS: { key: string; label: string; value: "" | AlertItem["level"] }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "info", label: "提示", value: "info" },
  { key: "warn", label: "警告", value: "warn" },
  { key: "crit", label: "严重", value: "crit" },
]
const CURRENT_SOURCE_LABELS: Record<string, string> = {
  postgresql: "PostgreSQL 运行事实",
  control_redis: "control Redis",
  usage_projection: "用量投影巡检",
  balance: "厂商余额巡检",
}
const RAW_SOURCE_OPTIONS: { key: string; label: string; value: "" | RawLogItem["source"] }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "report", label: "报告", value: "report" },
  { key: "reply", label: "回复", value: "reply" },
]
const RAW_PROCESSED_OPTIONS: { key: string; label: string; value: "" | "true" | "false" }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "false", label: "待重放", value: "false" },
  { key: "true", label: "已处理", value: "true" },
]

const alertEmpty = computed(() =>
  alertType.value.trim() || alertLevel.value || alertRange.value
    ? { title: "没有符合筛选条件的告警", description: "调整告警类型、等级或时间范围后重新查询，也可重置筛选。" }
    : { title: "暂无告警记录", description: "告警渠道为空时仅落 alert_log 与日志，不产生外呼。" },
)
const currentCritCount = computed(() => currentAlerts.value?.items.filter((item) => item.level === "crit").length ?? 0)
const currentWarnCount = computed(() => currentAlerts.value?.items.filter((item) => item.level === "warn").length ?? 0)
const currentUnknownText = computed(() =>
  currentAlerts.value?.unknown_sources.map((source) => CURRENT_SOURCE_LABELS[source] ?? source).join("、") ?? "",
)
const rawEmpty = computed(() =>
  rawSource.value || rawProcessed.value
    ? { title: "没有符合筛选条件的报文", description: "调整来源或处理状态后重新查询，也可重置筛选。" }
    : { title: "暂无原始报文", description: "厂商报文拉取后先以密文落入保险箱，再受控解密解析。" },
)
const unmatchedEmpty = computed(() =>
  unmatchedPhone.value.trim() || unmatchedRange.value
    ? { title: "没有符合筛选条件的无主报告", description: "调整手机号或时间范围后重新查询，也可重置筛选。" }
    : { title: "暂无迁移期无主报告", description: "无法匹配平台批次的状态报告在此留存，仅供对账核查。" },
)
const outboxEmpty = computed(() =>
  outboxState.value
    ? { title: "没有符合筛选条件的投递事件", description: "调整事件状态后重新查询，也可重置查看全部事件。" }
    : { title: "暂无投递事件", description: "Outbox 事件由业务事务写入，dispatcher 按租约逐步投递。" },
)
const UNCERTAIN_EMPTY = {
  title: "当前没有结果未知的分片",
  description: "uncertain 禁止自动重发；仅 reconcile 可按厂商报文修复，到期进入保守终态后须双人确认处置。",
}
const JOBS_EMPTY = {
  title: "暂无任务心跳记录",
  description: "心跳由 API 进程内巡检汇总，任务须以 tracked_job 声明预期间隔。",
}

function rangeValues(range: [Date, Date] | null): { start?: string; end?: string } {
  if (!range) return {}
  return { start: range[0].toISOString(), end: range[1].toISOString() }
}

function duration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

function resolutionLabel(action: string | null | undefined): string {
  return RESOLUTION_ACTIONS.find((item) => item.action === action)?.label ?? action ?? "—"
}

function resolutionStateLabel(state: string | null | undefined): string {
  const labels: Record<string, string> = {
    proposed: "待确认",
    approved: "已批准",
    effect_pending: "待生效",
    applying: "生效中",
    effect_applied: "已生效",
    closed: "已关闭",
    approval_rejected: "已驳回",
    retryable_effect_error: "生效失败可重试",
    manual_intervention_required: "需人工介入",
    cancelled_before_effect: "已取消",
  }
  return (state && labels[state]) || state || "—"
}

let loadToken = 0

async function load(tab: TabName = activeTab.value): Promise<void> {
  if (tab === "callbacks") return
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    if (tab === "alerts") {
      if (alertMode.value === "current") {
        const result = await getCurrentAlerts()
        if (token !== loadToken || activeTab.value !== tab || alertMode.value !== "current") return
        currentAlerts.value = result
        return
      }
      const result = await listAlerts({
        page: alertPage.value,
        alertType: alertType.value,
        level: alertLevel.value || undefined,
        ...rangeValues(alertRange.value),
      })
      if (token !== loadToken || activeTab.value !== tab) return
      alerts.value = result.items
      alertPage.value = result.page
      alertTotal.value = result.total
    }
    if (tab === "raw") {
      const result = await listRawLogs({
        page: rawPage.value,
        source: rawSource.value || undefined,
        processed: rawProcessed.value === "" ? undefined : rawProcessed.value === "true",
      })
      if (token !== loadToken || activeTab.value !== tab) return
      rawLogs.value = result.items
      rawPage.value = result.page
      rawTotal.value = result.total
    }
    if (tab === "uncertain") {
      const result = await listUncertain({ page: uncertainPage.value })
      if (token !== loadToken || activeTab.value !== tab) return
      uncertain.value = result.items
      uncertainPage.value = result.page
      uncertainTotal.value = result.total
    }
    if (tab === "unmatched") {
      const result = await listUnmatched({
        page: unmatchedPage.value,
        phone: unmatchedPhone.value,
        ...rangeValues(unmatchedRange.value),
      })
      if (token !== loadToken || activeTab.value !== tab) return
      unmatched.value = result.items
      unmatchedPage.value = result.page
      unmatchedTotal.value = result.total
    }
    if (tab === "jobs") {
      const items = await listJobs()
      if (token !== loadToken || activeTab.value !== tab) return
      jobs.value = items
    }
    if (tab === "queue") {
      const snapshot = await getQueueStatus()
      if (token !== loadToken || activeTab.value !== tab) return
      queue.value = snapshot
    }
    if (tab === "outbox") {
      const [stats, events] = await Promise.all([
        getOutboxStatus(),
        listOutboxEvents({ page: outboxPage.value, state: outboxState.value || undefined }),
      ])
      if (token !== loadToken || activeTab.value !== tab) return
      outboxStats.value = stats
      outboxEvents.value = events.items
      outboxPage.value = events.page
      outboxTotal.value = events.total
    }
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "运维数据加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function setAlertMode(mode: AlertMode): void {
  if (alertMode.value === mode) return
  alertMode.value = mode
  void load("alerts")
}

function reloadFromFirstPage(tab: "alerts" | "raw" | "unmatched" | "outbox"): void {
  if (tab === "alerts") alertPage.value = 1
  if (tab === "raw") rawPage.value = 1
  if (tab === "unmatched") unmatchedPage.value = 1
  if (tab === "outbox") outboxPage.value = 1
  void load(tab)
}

function setAlertLevel(value: "" | AlertItem["level"]): void {
  if (alertLevel.value === value) return
  alertLevel.value = value
  reloadFromFirstPage("alerts")
}

function resetAlerts(): void {
  alertType.value = ""
  alertLevel.value = ""
  alertRange.value = null
  reloadFromFirstPage("alerts")
}

function setRawSource(value: "" | RawLogItem["source"]): void {
  if (rawSource.value === value) return
  rawSource.value = value
  reloadFromFirstPage("raw")
}

function setRawProcessed(value: "" | "true" | "false"): void {
  if (rawProcessed.value === value) return
  rawProcessed.value = value
  reloadFromFirstPage("raw")
}

function setOutboxState(value: "" | OutboxState): void {
  if (outboxState.value === value) return
  outboxState.value = value
  reloadFromFirstPage("outbox")
}

function unmatchedPhoneProblem(): string | null {
  const value = unmatchedPhone.value.trim()
  return value === "" || PHONE_RE.test(value) ? null : "手机号须为 11 位以 1 开头的数字"
}

function searchUnmatched(): void {
  const issue = unmatchedPhoneProblem()
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  reloadFromFirstPage("unmatched")
}

function resetUnmatched(): void {
  unmatchedPhone.value = ""
  unmatchedRange.value = null
  reloadFromFirstPage("unmatched")
}

function shortTaskName(taskName: string): string {
  return taskName.split(".").pop() ?? taskName
}

function outboxStateMeta(state: OutboxState): { label: string; tag: "info" | "warning" | "success" | "danger" } {
  return OUTBOX_STATE_META[state]
}

function levelLabel(level: AlertItem["level"]): string {
  return ALERT_LEVEL_LABELS[level]
}

function levelTag(level: AlertItem["level"]): "danger" | "warning" | "info" {
  if (level === "crit") return "danger"
  if (level === "warn") return "warning"
  return "info"
}

function currentDuration(item: CurrentAlertItem): string {
  if (!item.since || !currentAlerts.value) return "起始时间未知"
  const seconds = Math.max(0, (Date.parse(currentAlerts.value.refreshed_at) - Date.parse(item.since)) / 1000)
  return `持续 ${duration(seconds)}`
}

function currentImpact(item: CurrentAlertItem): string {
  const detail = item.detail
  if (typeof detail.count === "number") return `${detail.count} 项`
  if (typeof detail.consecutive_failures === "number") return `连续 ${detail.consecutive_failures} 次`
  if (typeof detail.mismatched_dimensions === "number") {
    return `${detail.mismatched_dimensions} 个维度 · 差值 ${String(detail.absolute_delta ?? "—")}`
  }
  if (typeof detail.balance === "number") return `余额 ${detail.balance} / 阈值 ${String(detail.threshold ?? "—")}`
  if (typeof detail.dead === "number") return `活动 ${String(detail.active ?? 0)} · 死信 ${detail.dead}`
  const pauses = [detail.realtime_code && `实时 ${detail.realtime_code}`, detail.bulk_code && `批量 ${detail.bulk_code}`]
    .filter(Boolean)
  if (pauses.length) return pauses.join(" · ")
  if (typeof detail.source === "string") return detail.source === "report" ? "状态报告" : "上行回复"
  return "—"
}

function goCurrentTarget(item: CurrentAlertItem): void {
  activeTab.value = item.target
}

function openAlertDetail(item: AlertItem): void {
  selectedAlert.value = item
  alertDetailVisible.value = true
}

function selectTab(tab: TabName): void {
  activeTab.value = tab
}

async function moveTab(direction: -1 | 1): Promise<void> {
  const current = OPS_TABS.indexOf(activeTab.value)
  const next = (current + direction + OPS_TABS.length) % OPS_TABS.length
  activeTab.value = OPS_TABS[next]
  await nextTick()
  document.getElementById(`ops-tab-${activeTab.value}`)?.focus()
}

async function proposeResolution(item: UncertainItem, action: UncertainResolutionAction): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        h("p", `对批次 ${item.batch_no} 提出「${resolutionLabel(action)}」。确认后须另一名管理员复核；重发只会创建新批次，不会把旧分片改回待发送。`),
        h("p", { class: "ops-confirm-audit" }, "提出行为与操作人将写入审计日志。"),
      ]),
      "确认提出处置",
      { type: "warning", confirmButtonText: "提出处置", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    await proposeUncertainResolution(item.chunk_id, action)
    ElMessage.success("已提出处置 · 本次操作已记入审计")
    await load("uncertain")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "提出处置失败")
  }
}

async function confirmResolution(item: UncertainItem): Promise<void> {
  if (item.resolution_id == null) return
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        h("p", `确认批次 ${item.batch_no} 的处置「${resolutionLabel(item.resolution_action)}」。提案人不能确认自己的单。`),
        h("p", { class: "ops-confirm-audit" }, "确认行为与操作人将写入审计日志。"),
      ]),
      "确认处置",
      { type: "warning", confirmButtonText: "确认处置", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    await confirmUncertainResolution(item.resolution_id)
    ElMessage.success("处置已确认 · 本次操作已记入审计")
    await load("uncertain")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "确认处置失败")
  }
}

async function retryOutbox(item: OutboxEventItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        h("p", [
          "将死信事件 ",
          h("strong", item.event_type),
          `（${item.aggregate_type}/${item.aggregate_id}）重置为待投递，dispatcher 将按租约重新投递。`,
        ]),
        h("p", { class: "ops-confirm-audit" }, "重推行为与操作人将写入审计日志。"),
      ]),
      "确认重推 Outbox 事件",
      { type: "warning", confirmButtonText: "重推事件", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    retryingOutboxId.value = item.id
    await retryOutboxEvent(item.id)
    ElMessage.success("事件已重置为待投递 · 本次操作已记入审计")
    await load("outbox")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "事件重推失败")
  } finally {
    retryingOutboxId.value = null
  }
}

async function replay(item: RawLogItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        h("p", `重放 raw #${item.id}：仅允许未处理且载荷完整的报文；重放重新走受控解密解析，不会产生重复下发。`),
        h("p", { class: "ops-confirm-audit" }, "重放行为与操作人将写入审计日志。"),
      ]),
      "确认报文重放",
      { type: "warning", confirmButtonText: "确认重放", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    const result = await replayRaw(item.id)
    ElMessage.success(`重放完成，处理 ${result.processed_items} 项 · 本次操作已记入审计`)
    await load("raw")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "重放失败")
  }
}

async function trigger(item: JobItem): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        h("p", `手动触发 ${item.job_name} 将立即投递一次执行，不改变 beat 既有调度。`),
        h("p", { class: "ops-confirm-audit" }, "触发行为与操作人将写入审计日志。"),
      ]),
      "确认任务触发",
      { type: "warning", confirmButtonText: "手动触发", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    await triggerJob(item.job_name)
    ElMessage.success("任务已投递 · 本次操作已记入审计")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "任务触发失败")
  }
}

async function exportUnmatched(): Promise<void> {
  const issue = unmatchedPhoneProblem()
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  exportBusy.value = true
  exportError.value = ""
  try {
    exportTask.value = await createUnmatchedExport({
      phone: unmatchedPhone.value,
      ...rangeValues(unmatchedRange.value),
    }, exportDecrypted.value)
    ElMessage.success("对账导出任务已创建 · 本次操作已记入审计")
    exportPolling.restart()
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出创建失败"
    ElMessage.error(exportError.value)
  } finally {
    exportBusy.value = false
  }
}

async function downloadUnmatchedExport(): Promise<void> {
  if (!exportTask.value) return
  let password = ""
  try {
    let stepUpToken: string | undefined
    if (exportTask.value.decrypted) {
      const prompt = await ElMessageBox.prompt(
        "明文导出属于高风险操作，请重新输入当前认证源密码。",
        "下载明文导出",
        {
          inputType: "password",
          inputPlaceholder: "当前密码",
          confirmButtonText: "验证并下载",
          cancelButtonText: "取消",
        },
      )
      password = prompt.value
      stepUpToken = (await issueExportStepUp(exportTask.value.id, password)).token
    }
    const blob = await downloadExport(exportTask.value, stepUpToken)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = `unmatched-reports-${exportTask.value.id}.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "下载失败")
    }
  } finally {
    password = ""
  }
}

async function recover(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "ops-confirm-dialog" }, [
        forceResume.value
          ? h("p", ["FORCE 已开启：将", h("strong", "绕过余额与暂停原因守卫"), "，实时与批量队列立即恢复投递。"])
          : h("p", "仅在余额达标且暂停码为 999 时恢复，不满足条件时服务端拒绝。"),
        h("p", { class: "ops-confirm-audit" }, "恢复行为、force 取值与操作人将写入审计日志。"),
      ]),
      "确认恢复双队列",
      { type: "warning", confirmButtonText: "恢复队列", cancelButtonText: "取消", customClass: "ops-confirm-box" },
    )
    const result = await resumeQueue(forceResume.value)
    queueRecovered.value = true
    ElMessage.success(`已恢复 ${result.resumed_batches} 个批次 · 本次操作已记入审计`)
    await load("queue")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "队列恢复失败")
  }
}

watch(activeTab, (value) => {
  if (!visitedTabs.value.includes(value)) visitedTabs.value.push(value)
  void load(value)
  if (tabFromQuery(route.query.tab) === value) return
  const query = { ...route.query }
  if (value === "alerts") delete query.tab
  else query.tab = value
  void router.replace({ query })
})
watch(() => route.query.tab, (raw) => {
  const next = tabFromQuery(raw)
  if (next && next !== activeTab.value) activeTab.value = next
})
onMounted(() => {
  void load()
  void getQueueStatus().then((result) => { queue.value = result }).catch(() => undefined)
  currentAlertPolling.start()
})
</script>

<template>
  <section class="page-heading ops-heading">
    <div><p class="eyebrow">CONTROL ROOM / 运维控制室</p><h1>运维中心</h1><p>所有恢复动作均有守卫与审计；原始报文只展示无 PII 元数据。</p></div>
    <span class="ops-mode"><i></i> 审计在线 · 安全元数据</span>
  </section>

  <section v-if="queueBlocked || queueRecovered" :class="['circuit-banner', { recovered: queueRecovered && !queueBlocked }]">
    <span class="circuit-icon" aria-hidden="true">♨</span>
    <div>
      <strong>{{ queueRecovered && !queueBlocked ? '实时队列已恢复' : '余额熔断 · 实时队列暂缓' }}</strong>
      <p>{{ queueRecovered && !queueBlocked ? '积压将按 QPS 令牌逐步入队，恢复动作已记入审计。' : '实时发送暂停；批量通道状态以队列恢复面板为准。' }}</p>
    </div>
    <div class="circuit-actions">
      <el-button @click="activeTab = 'queue'">查看余额与队列</el-button>
      <el-button v-if="queueBlocked" type="primary" @click="recover">已充值，恢复队列</el-button>
      <el-tag v-else type="success">已记入审计</el-tag>
    </div>
  </section>

  <nav class="ops-tabs" role="tablist" aria-label="运维中心模块">
    <button
      v-for="tab in OPS_TAB_ITEMS"
      :id="`ops-tab-${tab.name}`"
      :key="tab.name"
      type="button"
      role="tab"
      :aria-selected="activeTab === tab.name"
      :aria-controls="`ops-panel-${tab.name}`"
      :tabindex="activeTab === tab.name ? 0 : -1"
      :class="{ active: activeTab === tab.name }"
      @click="selectTab(tab.name)"
      @keydown.left.prevent="moveTab(-1)"
      @keydown.right.prevent="moveTab(1)"
    >{{ tab.label }}</button>
  </nav>

  <aside class="ops-rules" aria-label="运维守卫与数据边界">
    <div><span>守卫与审计</span><p>重放 / 手动触发 / 死信重推 / 队列恢复均二次确认并写审计；uncertain 禁止自动重发，仅 reconcile 可按厂商报文修复；到期进入保守终态后须双人确认处置，重发只建新批次。</p></div>
    <div><span>PII 边界</span><p>原始报文只展示无 PII 元数据；手机号明文仅经请求体提交、服务端立即转 HMAC 精确查询，不写入日志与存储；明文导出需二次认证。</p></div>
  </aside>

  <el-alert v-if="errorMessage" class="ops-alert" :title="errorMessage" type="error" :closable="false"><template #default><el-button link type="primary" @click="load()">重新加载</el-button></template></el-alert>

  <section
    v-if="visitedTabs.includes('alerts')"
    v-show="activeTab === 'alerts'"
    id="ops-panel-alerts"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-alerts"
  >
    <header class="ops-panel-title ops-alert-title">
      <div><strong>{{ alertMode === 'current' ? '当前未恢复告警' : '告警触发历史' }}</strong><small>{{ alertMode === 'current' ? '从权威运行事实实时计算，不按最近告警时间猜测' : '去重后的触发快照，不代表异常仍在持续或外部渠道已送达' }}</small></div>
      <div class="ops-seg" role="group" aria-label="告警视图" data-testid="ops-alert-mode">
        <button type="button" :class="{ on: alertMode === 'current' }" @click="setAlertMode('current')">当前告警</button>
        <button type="button" :class="{ on: alertMode === 'history' }" @click="setAlertMode('history')">告警历史</button>
      </div>
    </header>

    <template v-if="alertMode === 'current'">
      <el-alert
        v-if="currentAlerts && !currentAlerts.complete"
        data-testid="current-alert-incomplete"
        class="ops-alert"
        type="warning"
        :closable="false"
        title="当前告警状态不完整"
        :description="`以下来源暂时无法确认：${currentUnknownText}。未显示为正常，请先恢复数据源。`"
        show-icon
      />
      <div v-if="currentAlerts" class="ops-current-summary" data-testid="current-alert-summary">
        <div><span>当前未恢复</span><strong>{{ currentAlerts.items.length }}</strong></div>
        <div><span>严重</span><strong class="crit">{{ currentCritCount }}</strong></div>
        <div><span>警告</span><strong class="warn">{{ currentWarnCount }}</strong></div>
        <p>刷新于 {{ formatDateTime(currentAlerts.refreshed_at) }} · 页面可见时每 60 秒更新</p>
      </div>
      <section class="ops-results">
        <el-table :data="currentAlerts?.items ?? []" class="ops-table">
          <el-table-column label="等级" width="88"><template #default="{ row }"><el-tag :type="levelTag(row.level)" :effect="row.level === 'crit' ? 'dark' : 'plain'">{{ levelLabel(row.level) }}</el-tag></template></el-table-column>
          <el-table-column prop="title" label="当前问题" min-width="260" />
          <el-table-column label="影响" min-width="150"><template #default="{ row }">{{ currentImpact(row) }}</template></el-table-column>
          <el-table-column prop="alert_type" label="类型" min-width="170" />
          <el-table-column label="持续状态" width="150"><template #default="{ row }">{{ currentDuration(row) }}</template></el-table-column>
          <el-table-column label="最后确认" width="180"><template #default="{ row }">{{ formatDateTime(row.checked_at) }}</template></el-table-column>
          <el-table-column label="操作" width="90" fixed="right"><template #default="{ row }"><el-button link type="primary" :data-testid="`current-alert-target-${row.key}`" @click="goCurrentTarget(row)">去处理</el-button></template></el-table-column>
          <template #empty><EmptyState :title="currentAlerts?.complete ? '当前没有未恢复告警' : '当前状态尚未完整确认'" :description="currentAlerts?.complete ? '所有已登记权威状态均为正常；历史触发仍可在“告警历史”查看。' : '请先恢复上方列出的数据源，系统不会把未知状态显示为正常。'" /></template>
        </el-table>
        <div class="ops-mobile-list"><article v-for="item in currentAlerts?.items ?? []" :key="item.key"><header><el-tag :type="levelTag(item.level)">{{ levelLabel(item.level) }}</el-tag><time>{{ currentDuration(item) }}</time></header><strong>{{ item.title }}</strong><p>{{ currentImpact(item) }}</p><p>{{ item.alert_type }} · {{ formatDateTime(item.checked_at) }}</p><el-button link type="primary" @click="goCurrentTarget(item)">去处理</el-button></article></div>
      </section>
    </template>

    <template v-else>
      <form class="ops-filter-bar" @submit.prevent="reloadFromFirstPage('alerts')">
        <label class="ops-fld"><span>告警类型（精确）</span>
          <el-input v-model="alertType" class="ops-keyword" clearable placeholder="如 job_failed" aria-label="告警类型精确筛选" />
        </label>
        <div class="ops-fld"><span>等级</span>
          <div class="ops-seg" role="group" aria-label="告警等级筛选" data-testid="ops-alert-level-seg">
            <button
              v-for="option in ALERT_LEVEL_OPTIONS"
              :key="option.key"
              type="button"
              :class="{ on: alertLevel === option.value }"
              :data-testid="`ops-alert-level-${option.key}`"
              @click="setAlertLevel(option.value)"
            >{{ option.label }}</button>
          </div>
        </div>
        <label class="ops-fld"><span>时间范围</span>
          <el-date-picker v-model="alertRange" class="ops-dates" type="datetimerange" popper-class="qingluan-date-popper" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间" />
        </label>
        <div class="ops-filter-go">
          <el-button type="primary" @click="reloadFromFirstPage('alerts')">查询</el-button>
          <el-button @click="resetAlerts">重置</el-button>
        </div>
        <p class="ops-privacy">服务端分页过滤；每条为去重后的触发快照，不表示异常仍在持续。“配置路由”也不等同于外部渠道已经送达。</p>
      </form>
      <section class="ops-results">
        <el-table :data="alerts" class="ops-table"><el-table-column label="等级" width="88"><template #default="{ row }"><el-tag :type="levelTag(row.level)" :effect="row.level === 'crit' ? 'dark' : 'plain'">{{ levelLabel(row.level) }}</el-tag></template></el-table-column><el-table-column prop="title" label="告警" min-width="220" /><el-table-column prop="alert_type" label="类型" min-width="150" /><el-table-column prop="channels" label="配置路由" width="120" /><el-table-column label="记录时间" width="180"><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="70" fixed="right"><template #default="{ row }"><el-button link type="primary" :data-testid="`alert-detail-${row.id}`" @click="openAlertDetail(row)">详情</el-button></template></el-table-column><template #empty><EmptyState :title="alertEmpty.title" :description="alertEmpty.description" /></template></el-table>
        <div class="ops-mobile-list"><article v-for="item in alerts" :key="item.id"><header><el-tag :type="levelTag(item.level)">{{ levelLabel(item.level) }}</el-tag><time>{{ formatDateTime(item.created_at) }}</time></header><strong>{{ item.title }}</strong><p>{{ item.alert_type }} · {{ item.channels }}</p><el-button link type="primary" @click="openAlertDetail(item)">详情</el-button></article><EmptyState v-if="!alerts.length" :title="alertEmpty.title" :description="alertEmpty.description" /></div>
        <footer class="ops-pagination"><span>共 {{ alertTotal }} 条 · 每页 20</span><el-pagination v-model:current-page="alertPage" data-testid="ops-alert-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="alertTotal" layout="prev, pager, next" @current-change="load('alerts')" /></footer>
      </section>
    </template>
  </section>

  <section
    v-if="visitedTabs.includes('callbacks')"
    v-show="activeTab === 'callbacks'"
    id="ops-panel-callbacks"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-callbacks"
  >
    <CallbackView embedded />
  </section>

  <section
    v-if="visitedTabs.includes('raw')"
    v-show="activeTab === 'raw'"
    id="ops-panel-raw"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-raw"
  >
    <header class="ops-panel-title"><div><strong>原始报文保险箱</strong><small>密文载荷与完整性摘要不对外返回</small></div></header>
    <form class="ops-filter-bar" @submit.prevent>
      <div class="ops-fld"><span>来源</span>
        <div class="ops-seg" role="group" aria-label="报文来源筛选" data-testid="ops-raw-source-seg">
          <button
            v-for="option in RAW_SOURCE_OPTIONS"
            :key="option.key"
            type="button"
            :class="{ on: rawSource === option.value }"
            :data-testid="`ops-raw-source-${option.key}`"
            @click="setRawSource(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <div class="ops-fld"><span>处理状态</span>
        <div class="ops-seg" role="group" aria-label="处理状态筛选" data-testid="ops-raw-processed-seg">
          <button
            v-for="option in RAW_PROCESSED_OPTIONS"
            :key="option.key"
            type="button"
            :class="{ on: rawProcessed === option.value }"
            :data-testid="`ops-raw-processed-${option.key}`"
            @click="setRawProcessed(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <p class="ops-privacy">点选即重查；拉走即消费，完整响应先以 AES-GCM 密文落库，页面只展示无 PII 元数据。</p>
    </form>
    <section class="ops-results">
      <el-table :data="rawLogs" class="ops-table"><el-table-column prop="id" label="RAW" width="80" /><el-table-column prop="source" label="来源" width="100" /><el-table-column label="记录 / customId" min-width="150"><template #default="{ row }">{{ row.item_count }} / {{ row.custom_id_count }}</template></el-table-column><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="row.processed ? 'success' : 'danger'">{{ row.processed ? '已处理' : '待重放' }}</el-tag></template></el-table-column><el-table-column label="完整性" width="120"><template #default="{ row }"><el-tag :type="row.capture_state === 'complete' ? 'success' : row.capture_state === 'complete_too_large' ? 'warning' : 'danger'">{{ row.capture_state === 'complete' ? '完整' : row.capture_state === 'complete_too_large' ? '超限完整' : '截断' }}</el-tag></template></el-table-column><el-table-column prop="error" label="错误摘要" min-width="180" /><el-table-column label="时间" width="180"><template #default="{ row }">{{ formatDateTime(row.fetched_at) }}</template></el-table-column><el-table-column label="操作" width="90"><template #default="{ row }"><el-button v-if="!row.processed && row.capture_state !== 'truncated'" link type="danger" @click="replay(row)">重放</el-button></template></el-table-column><template #empty><EmptyState :title="rawEmpty.title" :description="rawEmpty.description" /></template></el-table>
      <div class="ops-mobile-list"><article v-for="item in rawLogs" :key="item.id"><header><strong>RAW-{{ item.id }} · {{ item.source }}</strong><el-tag :type="item.processed ? 'success' : 'danger'">{{ item.processed ? '已处理' : '待重放' }}</el-tag></header><p>{{ item.item_count }} 项 · {{ item.custom_id_count }} customId · {{ item.capture_state === 'complete' ? '完整' : item.capture_state === 'complete_too_large' ? '超限完整' : '截断' }}</p><small>{{ item.error || formatDateTime(item.fetched_at) }}</small><el-button v-if="!item.processed && item.capture_state !== 'truncated'" link type="danger" @click="replay(item)">重放</el-button></article><EmptyState v-if="!rawLogs.length" :title="rawEmpty.title" :description="rawEmpty.description" /></div>
      <footer class="ops-pagination"><span>共 {{ rawTotal }} 条 · 每页 20</span><el-pagination v-model:current-page="rawPage" data-testid="ops-raw-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="rawTotal" layout="prev, pager, next" @current-change="load('raw')" /></footer>
    </section>
  </section>

  <section
    v-if="visitedTabs.includes('uncertain')"
    v-show="activeTab === 'uncertain'"
    id="ops-panel-uncertain"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-uncertain"
  >
    <header class="ops-panel-title"><div><strong>结果未知分片</strong><small>禁止自动重发；保守终态后双人确认处置，重发只建新批次</small></div></header>
    <section class="ops-results">
      <el-table :data="uncertain" class="ops-table"><el-table-column prop="batch_no" label="批次" min-width="160" /><el-table-column label="状态" width="110"><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column><el-table-column label="customId" min-width="160"><template #default="{ row }"><code class="ops-hash" :title="row.custom_id">{{ row.custom_id }}</code></template></el-table-column><el-table-column prop="phone_count" label="号码数" width="90" /><el-table-column label="停留" width="110"><template #default="{ row }"><el-tag :type="row.age_seconds >= 86400 ? 'danger' : 'warning'">{{ duration(row.age_seconds) }}</el-tag></template></el-table-column><el-table-column label="处置" min-width="220"><template #default="{ row }"><template v-if="row.status === 'unknown_terminal' && !row.resolution_id"><el-button v-for="option in RESOLUTION_ACTIONS" :key="option.action" link type="primary" :data-testid="`uncertain-propose-${option.action}`" @click="proposeResolution(row, option.action)">{{ option.label }}</el-button></template><template v-else-if="row.resolution_state === 'proposed'"><span>待确认 · {{ resolutionLabel(row.resolution_action) }}</span><el-button v-if="session.accountId !== row.proposer_account_id" link type="danger" data-testid="uncertain-confirm" @click="confirmResolution(row)">确认处置</el-button></template><span v-else-if="row.resolution_state">{{ resolutionStateLabel(row.resolution_state) }} · {{ resolutionLabel(row.resolution_action) }}</span><span v-else>禁止自动重发</span></template></el-table-column><template #empty><EmptyState :title="UNCERTAIN_EMPTY.title" :description="UNCERTAIN_EMPTY.description" /></template></el-table>
      <div class="ops-mobile-list"><article v-for="item in uncertain" :key="item.chunk_id"><header><strong>{{ item.batch_no }}</strong><StatusTag :status="item.status" /></header><code>{{ item.custom_id }}</code><p>{{ item.phone_count }} 个号码 · {{ duration(item.age_seconds) }}</p><template v-if="item.status === 'unknown_terminal' && !item.resolution_id"><el-button v-for="option in RESOLUTION_ACTIONS" :key="option.action" link type="primary" @click="proposeResolution(item, option.action)">{{ option.label }}</el-button></template><el-button v-else-if="item.resolution_state === 'proposed' && session.accountId !== item.proposer_account_id" link type="danger" @click="confirmResolution(item)">确认处置</el-button></article><EmptyState v-if="!uncertain.length" :title="UNCERTAIN_EMPTY.title" :description="UNCERTAIN_EMPTY.description" /></div>
      <footer class="ops-pagination"><span>共 {{ uncertainTotal }} 项 · 每页 20</span><el-pagination v-model:current-page="uncertainPage" data-testid="ops-uncertain-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="uncertainTotal" layout="prev, pager, next" @current-change="load('uncertain')" /></footer>
    </section>
  </section>

  <section
    v-if="visitedTabs.includes('unmatched')"
    v-show="activeTab === 'unmatched'"
    id="ops-panel-unmatched"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-unmatched"
  >
    <header class="ops-panel-title"><div><strong>迁移期无主报告</strong><small>无法匹配平台批次的状态报告在此留存，仅供对账核查</small></div></header>
    <form class="ops-filter-bar" @submit.prevent="searchUnmatched">
      <label class="ops-fld"><span>手机号</span>
        <el-input v-model="unmatchedPhone" class="ops-phone" maxlength="11" clearable placeholder="手机号精确查询" data-testid="ops-unmatched-phone" aria-label="无主报告手机号" />
      </label>
      <label class="ops-fld"><span>报告时间</span>
        <el-date-picker v-model="unmatchedRange" class="ops-dates" type="datetimerange" popper-class="qingluan-date-popper" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间" />
      </label>
      <div class="ops-filter-go">
        <el-button data-testid="ops-unmatched-search" @click="searchUnmatched">查询</el-button>
        <el-button @click="resetUnmatched">重置</el-button>
        <el-checkbox v-model="exportDecrypted">授权明文</el-checkbox>
        <el-button type="primary" :loading="exportBusy" @click="exportUnmatched">导出对账</el-button>
      </div>
      <p class="ops-privacy">手机号明文仅随请求体提交，服务端立即转换为 HMAC 精确查询，不写入日志与存储；勾选「授权明文」导出的文件仍以密文落盘，下载时需重新输入当前认证源密码。</p>
    </form>
    <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" />
    <el-alert v-if="exportTask" :title="`导出任务 #${exportTask.id} · ${exportTask.status}`" :type="exportTask.status === 'failed' ? 'error' : 'success'" :closable="false"><template #default><div class="export-task-detail"><span v-if="exportTask.row_count !== null">{{ exportTask.row_count }} 行</span><span v-if="exportTask.expires_at">有效期至 {{ formatDateTime(exportTask.expires_at) }}</span><el-button v-if="exportTask.status === 'done' && exportTask.download_url" data-testid="download-unmatched-export" type="primary" link @click="downloadUnmatchedExport">下载 CSV</el-button></div></template></el-alert>
    <section class="ops-results">
      <el-table :data="unmatched" class="ops-table"><el-table-column label="号码" width="140"><template #default="{ row }"><PhoneMask :value="row.phone_mask" /></template></el-table-column><el-table-column label="customId" min-width="170"><template #default="{ row }"><code class="ops-hash" :title="row.custom_id || ''">{{ row.custom_id || "—" }}</code></template></el-table-column><el-table-column label="厂商任务" min-width="150"><template #default="{ row }"><code class="ops-hash" :title="row.vendor_task_id || ''">{{ row.vendor_task_id || "—" }}</code></template></el-table-column><el-table-column prop="report_desc" label="结果" width="120" /><el-table-column label="报告时间" width="180"><template #default="{ row }">{{ formatDateTime(row.report_time) }}</template></el-table-column><template #empty><EmptyState :title="unmatchedEmpty.title" :description="unmatchedEmpty.description" /></template></el-table>
      <div class="ops-mobile-list"><article v-for="item in unmatched" :key="item.id"><header><PhoneMask :value="item.phone_mask" /><el-tag type="warning">无主报告</el-tag></header><code>{{ item.custom_id || '—' }}</code><p>{{ item.report_desc || '未知结果' }} · {{ formatDateTime(item.report_time) }}</p></article><EmptyState v-if="!unmatched.length" :title="unmatchedEmpty.title" :description="unmatchedEmpty.description" /></div>
      <footer class="ops-pagination"><span>共 {{ unmatchedTotal }} 条 · 每页 20</span><el-pagination v-model:current-page="unmatchedPage" data-testid="ops-unmatched-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="unmatchedTotal" layout="prev, pager, next" @current-change="load('unmatched')" /></footer>
    </section>
  </section>

  <section
    v-if="visitedTabs.includes('jobs')"
    v-show="activeTab === 'jobs'"
    id="ops-panel-jobs"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-jobs"
  >
    <header class="ops-panel-title"><div><strong>后台任务心跳</strong><small>预期间隔由 beat 与 API 启动时读取，修改后需重启两个容器 · 共 {{ jobs.length }} 项</small></div></header>
    <section class="ops-results">
      <el-table :data="jobs" class="ops-table"><el-table-column prop="job_name" label="任务" min-width="180" /><el-table-column label="中文用途" min-width="270"><template #default="{ row }"><span class="job-description">{{ jobDescription(row.job_name) }}</span></template></el-table-column><el-table-column label="健康" width="100"><template #default="{ row }"><span class="job-health" :class="{ danger: row.stalled || row.last_status === 'failed' }"><i></i>{{ row.stalled ? 'stalled' : row.last_status || '无记录' }}</span></template></el-table-column><el-table-column prop="last_duration_ms" label="耗时 ms" width="100" /><el-table-column prop="last_items" label="处理量" width="90" /><el-table-column label="24h 成功率" width="120"><template #default="{ row }">{{ row.last_run_at ? (row.success_rate_24h * 100).toFixed(1) + '%' : '—' }}</template></el-table-column><el-table-column label="最近运行" width="180"><template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template></el-table-column><el-table-column label="操作" width="110"><template #default="{ row }"><el-button link type="primary" @click="trigger(row)">手动触发</el-button></template></el-table-column><template #empty><EmptyState :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description" /></template></el-table>
      <div class="ops-mobile-list"><article v-for="item in jobs" :key="item.job_name"><header><strong>{{ item.job_name }}</strong><span class="job-health" :class="{ danger: item.stalled }"><i></i>{{ item.stalled ? 'stalled' : item.last_status || '无记录' }}</span></header><p class="job-description">{{ jobDescription(item.job_name) }}</p><p>{{ item.last_items }} 项 · {{ item.last_duration_ms ?? 0 }}ms · {{ item.last_run_at ? (item.success_rate_24h * 100).toFixed(1) + '%' : '—' }}</p><el-button link type="primary" @click="trigger(item)">手动触发</el-button></article><EmptyState v-if="!jobs.length" :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description" /></div>
    </section>
  </section>

  <section
    v-if="visitedTabs.includes('queue')"
    v-show="activeTab === 'queue'"
    id="ops-panel-queue"
    v-loading="loading"
    class="ops-panel queue-recovery"
    role="tabpanel"
    aria-labelledby="ops-tab-queue"
  >
    <header class="ops-panel-title"><div><strong>双队列恢复</strong><small>PostgreSQL 状态先恢复，Redis 仅作为投递通道</small></div></header>
    <template v-if="queue"><div class="queue-status-grid"><article><span>REALTIME</span><strong>{{ queue.realtime_code ? `暂停 · ${queue.realtime_code}` : '运行中' }}</strong></article><article><span>BULK</span><strong>{{ queue.bulk_code ? `暂停 · ${queue.bulk_code}` : '运行中' }}</strong></article><article><span>余额</span><strong>{{ queue.balance === null ? '无快照' : `余额 ${queue.balance.toLocaleString()}` }}</strong><small>阈值 {{ queue.threshold.toLocaleString() }}</small></article></div><div class="break-glass"><el-switch v-model="forceResume" data-testid="force-resume" inline-prompt active-text="FORCE" inactive-text="SAFE" /><p>{{ forceResume ? '将绕过余额与暂停原因守卫，操作会写审计。' : '仅余额达到阈值且暂停码为 999 时允许恢复。' }}</p><el-button type="danger" @click="recover">恢复队列</el-button></div></template>
  </section>

  <section
    v-if="visitedTabs.includes('outbox')"
    v-show="activeTab === 'outbox'"
    id="ops-panel-outbox"
    v-loading="loading"
    class="ops-panel"
    role="tabpanel"
    aria-labelledby="ops-tab-outbox"
  >
    <header class="ops-panel-title"><div><strong>事务性 Outbox 投递</strong><small>PostgreSQL 为唯一事实源 · 死信事件确认后可人工重推</small></div></header>
    <form class="ops-filter-bar" @submit.prevent>
      <div class="ops-fld"><span>事件状态</span>
        <div class="ops-seg" role="group" aria-label="Outbox 事件状态筛选" data-testid="ops-outbox-state">
          <button type="button" :class="{ on: outboxState === '' }" data-testid="ops-outbox-state-all" @click="setOutboxState('')">全部</button>
          <button
            v-for="option in OUTBOX_STATE_OPTIONS"
            :key="option.value"
            type="button"
            :class="{ on: outboxState === option.value }"
            :data-testid="`ops-outbox-state-${option.value}`"
            @click="setOutboxState(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <p class="ops-privacy">点选即重查；事件由业务事务写入，dispatcher 按租约逐步投递，重推不二次投递已完成事件。</p>
    </form>
    <div v-if="outboxStats" class="outbox-stats" data-testid="outbox-stats">
      <article><span>待投递</span><strong>{{ outboxStats.pending }}</strong></article>
      <article><span>已发布</span><strong>{{ outboxStats.published }}</strong></article>
      <article><span>处理中</span><strong>{{ outboxStats.processing }}</strong></article>
      <article class="danger"><span>死信</span><strong>{{ outboxStats.dead }}</strong></article>
      <article><span>失败尝试</span><strong>{{ outboxStats.failed_attempts }}</strong></article>
      <article><span>最老积压</span><strong>{{ duration(outboxStats.oldest_age_seconds) }}</strong></article>
    </div>
    <section class="ops-results">
      <el-table :data="outboxEvents" class="ops-table"><el-table-column label="事件" min-width="140"><template #default="{ row }"><strong>{{ row.event_type }}</strong></template></el-table-column><el-table-column label="聚合引用" min-width="180"><template #default="{ row }"><code class="batch-code">{{ row.aggregate_type }}/{{ row.aggregate_id }}</code></template></el-table-column><el-table-column label="任务" min-width="130"><template #default="{ row }">{{ shortTaskName(row.task_name) }}</template></el-table-column><el-table-column prop="queue" label="队列" width="90" /><el-table-column label="状态" width="90"><template #default="{ row }"><el-tag :type="outboxStateMeta(row.state).tag">{{ outboxStateMeta(row.state).label }}</el-tag></template></el-table-column><el-table-column label="尝试" width="80"><template #default="{ row }">{{ row.attempts }}/{{ row.max_attempts }}</template></el-table-column><el-table-column prop="failure_count" label="失败" width="70" /><el-table-column label="最近错误" min-width="130"><template #default="{ row }">{{ row.last_error || '—' }}</template></el-table-column><el-table-column label="更新时间" width="170"><template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template></el-table-column><el-table-column label="操作" width="80" fixed="right"><template #default="{ row }"><el-button v-if="row.state === 'dead'" link type="danger" :loading="retryingOutboxId === row.id" :data-testid="`outbox-retry-${row.id}`" @click="retryOutbox(row)">重推</el-button></template></el-table-column><template #empty><EmptyState :title="outboxEmpty.title" :description="outboxEmpty.description" /></template></el-table>
      <div class="ops-mobile-list"><article v-for="item in outboxEvents" :key="item.id"><header><strong>{{ item.event_type }}</strong><el-tag :type="outboxStateMeta(item.state).tag">{{ outboxStateMeta(item.state).label }}</el-tag></header><code>{{ item.aggregate_type }}/{{ item.aggregate_id }}</code><p>{{ shortTaskName(item.task_name) }} · {{ item.queue }} · 尝试 {{ item.attempts }}/{{ item.max_attempts }} · 失败 {{ item.failure_count }}</p><small>{{ item.last_error || formatDateTime(item.updated_at) }}</small><el-button v-if="item.state === 'dead'" link type="danger" :loading="retryingOutboxId === item.id" @click="retryOutbox(item)">重推</el-button></article><EmptyState v-if="!outboxEvents.length" :title="outboxEmpty.title" :description="outboxEmpty.description" /></div>
      <footer class="ops-pagination"><span>共 {{ outboxTotal }} 条 · 每页 20</span><el-pagination v-model:current-page="outboxPage" data-testid="ops-outbox-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="outboxTotal" layout="prev, pager, next" @current-change="load('outbox')" /></footer>
    </section>
  </section>

  <el-drawer v-model="alertDetailVisible" title="告警详情" size="min(440px, 92vw)" :teleported="false" destroy-on-close>
    <template v-if="selectedAlert">
      <dl class="alert-detail-list">
        <div><dt>等级</dt><dd><el-tag :type="selectedAlert.level === 'crit' ? 'danger' : selectedAlert.level === 'warn' ? 'warning' : 'info'" :effect="selectedAlert.level === 'crit' ? 'dark' : 'plain'">{{ levelLabel(selectedAlert.level) }}</el-tag></dd></div>
        <div><dt>类型</dt><dd>{{ selectedAlert.alert_type }}</dd></div>
        <div><dt>渠道</dt><dd>{{ selectedAlert.channels }}</dd></div>
        <div><dt>时间</dt><dd>{{ formatDateTime(selectedAlert.created_at) }}</dd></div>
      </dl>
      <h3 class="alert-detail-heading">{{ selectedAlert.title }}</h3>
      <pre v-if="selectedAlert.detail" class="alert-detail-json" data-testid="alert-detail-json">{{ JSON.stringify(selectedAlert.detail, null, 2) }}</pre>
      <p v-else class="alert-detail-none">无附加详情</p>
    </template>
  </el-drawer>
</template>
