<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref, watch } from "vue"
import { useRoute } from "vue-router"

import PhoneMask from "../components/PhoneMask.vue"
import PhoneReveal from "../components/PhoneReveal.vue"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"
import { listApps, type ManagedApp } from "../api/apps"
import {
  getBatch,
  getBatchMessages,
  cancelBatch,
  decryptMessagePhone,
  listBatches,
  resendFailedBatch,
  rescheduleBatch,
  type BatchItem,
  type BatchMessage,
} from "../api/queries"
import { CATEGORY_LABELS } from "../lib/labels"
import { formatDateTime, formatDateTimeMinute } from "../lib/time"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
// 测试环境未安装路由时 useRoute 返回 undefined；仅用于消费 /batches?batch_no= 深链。
const route = useRoute()

const items = ref<BatchItem[]>([])
const total = ref(0)
const statusCounts = ref<Record<string, number> | null>(null)
const page = ref(1)
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
const loading = ref(false)
const errorMessage = ref("")
const appliedFiltersKey = ref("")
const drawer = ref(false)
const selected = ref<BatchItem | null>(null)
const details = ref<BatchMessage[]>([])
const detailTotal = ref(0)
const detailPage = ref(1)
const detailStatus = ref("")
const detailsLoading = ref(false)
const rescheduleOpen = ref(false)
const scheduledAt = ref("")
const canWrite = computed(() => session.role === "operator" || session.role === "admin")
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")
const isAdmin = computed(() => session.role === "admin")

// 状态分组为前端推导；各组计数来自服务端分面 status_counts（不含状态条件本身）
const statusGroups = [
  { key: "all", label: "全部", statuses: [] as string[] },
  { key: "active", label: "进行中", statuses: ["queued", "sending"] },
  { key: "pending_approval", label: "待审批", statuses: ["pending_approval"] },
  { key: "scheduled", label: "已排期", statuses: ["scheduled"] },
  { key: "balance_blocked", label: "余额阻断", statuses: ["balance_blocked"] },
  { key: "completed", label: "已完成", statuses: ["completed"] },
  { key: "closed", label: "其他终态", statuses: ["cancelled", "rejected", "expired"] },
]

const channelLabel: Record<string, string> = { api: "API", web: "Web" }
const statusLabel: Record<string, string> = {
  pending_approval: "待审批", rejected: "已驳回", scheduled: "已排期", queued: "排队中",
  sending: "发送中", completed: "已完成", cancelled: "已取消", balance_blocked: "余额阻断",
  expired: "已过期", delivered: "已送达", failed: "失败", unknown: "未知", pending: "待处理",
  sent: "已提交", other: "其他",
}
const detailStatusOptions = [
  { label: "待处理", value: "pending" },
  { label: "已提交", value: "sent" },
  { label: "已送达", value: "delivered" },
  { label: "失败", value: "failed" },
  { label: "未知", value: "unknown" },
  { label: "其他", value: "other" },
]
const categoryOptions = [
  { label: "全部", value: "" },
  { label: "验证码", value: "verify" },
  { label: "通知", value: "notice" },
  { label: "营销", value: "market" },
]
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

function shortBatchNo(value: string): string {
  return value.length > 12 ? `${value.slice(0, 2)}…${value.slice(-4)}` : value
}

/** 结果构成直接使用服务端消息状态计数，不从总数反推。 */
function composeOf(row: BatchItem): {
  pending: number
  sent: number
  delivered: number
  failed: number
  unknown: number
  other: number
} {
  return {
    pending: row.pending,
    sent: row.sent,
    delivered: row.delivered,
    failed: row.failed,
    unknown: row.unknown,
    other: row.other,
  }
}

function activeOf(row: BatchItem): number {
  return row.pending + row.sent
}

function composePct(part: number, row: BatchItem): string {
  return row.total > 0 ? `${(part / row.total) * 100}%` : "0%"
}

function composeText(row: BatchItem): string {
  const parts = composeOf(row)
  return `待处理 ${parts.pending}，待回执 ${parts.sent}，送达 ${parts.delivered}，失败 ${parts.failed}，未知 ${parts.unknown}，其他 ${parts.other}，占受理总数 ${row.total}`
}

function groupCount(key: string, statuses: string[]): number | null {
  if (statusCounts.value === null) return null
  if (key === "all") return Object.values(statusCounts.value).reduce((sum, n) => sum + n, 0)
  return statuses.reduce((sum, status) => sum + (statusCounts.value?.[status] ?? 0), 0)
}

function selectGroup(key: string): void {
  if (statusGroup.value === key) return
  statusGroup.value = key
  page.value = 1
  void load()
}

const currentFiltersKey = computed(() =>
  JSON.stringify([
    batchNo.value.trim(), category.value, channel.value, isTest.value,
    appId.value.trim(), isAdmin.value ? dept.value.trim() : "",
    range.value?.[0]?.toISOString() ?? "", range.value?.[1]?.toISOString() ?? "",
  ]),
)
const filtersDirty = computed(
  () => appliedFiltersKey.value !== "" && currentFiltersKey.value !== appliedFiltersKey.value,
)
const moreActiveCount = computed(
  () =>
    (isTest.value !== "" ? 1 : 0)
    + (appId.value.trim() !== "" ? 1 : 0)
    + (isAdmin.value && dept.value.trim() !== "" ? 1 : 0),
)
const moreActive = computed(() => moreActiveCount.value > 0)

let listToken = 0
let detailToken = 0
let openToken = 0

async function load(): Promise<void> {
  const token = ++listToken
  loading.value = true
  errorMessage.value = ""
  try {
    const group = statusGroups.find((item) => item.key === statusGroup.value) ?? statusGroups[0]
    const result = await listBatches({
      page: page.value,
      category: category.value || undefined,
      status: group.statuses.length ? group.statuses.join(",") : undefined,
      is_test: isTest.value === "" ? undefined : isTest.value === "true",
      channel: channel.value || undefined,
      app_id: appId.value.trim() ? Number(appId.value.trim()) : undefined,
      dept: isAdmin.value && dept.value.trim() ? dept.value.trim() : undefined,
      batch_no: batchNo.value.trim() || undefined,
      start: range.value?.[0].toISOString(),
      end: range.value?.[1].toISOString(),
    })
    if (token !== listToken) return
    items.value = result.items
    total.value = result.total
    statusCounts.value = result.status_counts ?? null
    appliedFiltersKey.value = currentFiltersKey.value
  } catch (error) {
    if (token !== listToken) return
    errorMessage.value = error instanceof Error ? error.message : "批次列表加载失败"
  } finally {
    if (token === listToken) loading.value = false
  }
}

async function loadDetails(): Promise<void> {
  if (!selected.value) return
  const token = ++detailToken
  const batchNoValue = selected.value.batch_no
  detailsLoading.value = true
  try {
    const result = await getBatchMessages(batchNoValue, {
      status: detailStatus.value || undefined,
      page: detailPage.value,
    })
    if (token !== detailToken || selected.value?.batch_no !== batchNoValue) return
    details.value = result.items
    detailTotal.value = result.total
  } catch (error) {
    if (token !== detailToken) return
    // 抽屉打开时列表卡片的 el-alert 被遮挡，此处必须用浮层消息。
    ElMessage.error(error instanceof Error ? error.message : "批次明细加载失败")
  } finally {
    if (token === detailToken) detailsLoading.value = false
  }
}

async function openBatch(item: BatchItem): Promise<void> {
  const token = ++openToken
  drawer.value = true
  selected.value = item
  details.value = []
  detailTotal.value = 0
  detailPage.value = 1
  detailStatus.value = ""
  try {
    const [batch] = await Promise.all([getBatch(item.batch_no), loadDetails()])
    if (token !== openToken) return
    selected.value = batch
  } catch (error) {
    if (token !== openToken) return
    ElMessage.error(error instanceof Error ? error.message : "批次详情加载失败")
  }
}

function filterDetails(): void {
  detailPage.value = 1
  void loadDetails()
}

/** 行内授权查看：仅把明文返回给 PhoneReveal 内存展示，视图不落明文状态。 */
async function revealPhone(messageId: number): Promise<string> {
  const result = await decryptMessagePhone(messageId)
  return result.phone
}

const canScheduleOps = computed(() => canWrite.value && selected.value?.status === "scheduled")
const canResendFailed = computed(() => canWrite.value && (selected.value?.failed ?? 0) > 0)

async function cancelSelected(): Promise<void> {
  if (!selected.value || !canScheduleOps.value) return
  try {
    await ElMessageBox.confirm(`取消批次 ${selected.value.batch_no}？配额将按规则回补。`, "确认取消", { type: "warning" })
    await cancelBatch(selected.value.batch_no)
    drawer.value = false
    ElMessage.success("批次已取消")
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "取消失败") }
}

function openReschedule(): void {
  if (!canScheduleOps.value) return
  scheduledAt.value = selected.value?.scheduled_at || ""
  rescheduleOpen.value = true
}

async function saveReschedule(): Promise<void> {
  if (!selected.value || !scheduledAt.value) return
  try {
    await rescheduleBatch(selected.value.batch_no, new Date(scheduledAt.value).toISOString())
    rescheduleOpen.value = false
    drawer.value = false
    ElMessage.success("批次已改期并重新执行审批判定")
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "改期失败") }
}

async function resendFailed(): Promise<void> {
  if (!selected.value || !canResendFailed.value) return
  try {
    await ElMessageBox.confirm("失败号码将生成新批次并完整重走频控、审批和时间窗。", "确认重发", { type: "warning" })
    const result = await resendFailedBatch(selected.value.batch_no)
    ElMessage.success(`重发批次 ${result.batch_no} 已创建`)
    drawer.value = false
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "重发失败") }
}

/** 重发溯源跳转：复用批次号模糊筛选定位源批次 */
function traceResendOf(sourceBatchNo: string): void {
  drawer.value = false
  batchNo.value = sourceBatchNo
  statusGroup.value = "all"
  page.value = 1
  void load()
}

function search(): void { page.value = 1; void load() }
function reset(): void {
  category.value = ""; statusGroup.value = "all"; channel.value = ""; batchNo.value = ""
  isTest.value = ""; appId.value = ""; dept.value = ""; range.value = null
  search()
}

onMounted(() => {
  void load()
  // 深链 /batches?batch_no=xxx：从回复页等入口直达批次详情抽屉
  const target = typeof route?.query.batch_no === "string" ? route.query.batch_no.trim() : ""
  if (target) {
    getBatch(target)
      .then((batch) => openBatch(batch))
      .catch(() => ElMessage.error("未找到对应批次或无权限查看"))
  }
})

// 应用下拉仅管理员可用（应用管理接口为管理员域）；懒加载，首次打开更多筛选时拉取
let appsRequested = false
watch(moreOpen, (open) => {
  if (!open || appsRequested || !isAdmin.value) return
  appsRequested = true
  listApps()
    .then((apps) => { appOptions.value = apps.filter((app) => app.status === 1) })
    .catch(() => { appOptions.value = [] })
})
</script>

<template>
  <section class="page-heading batch-heading">
    <div>
      <p class="eyebrow">BATCH LEDGER / 发送账本</p>
      <h1>批次列表</h1>
      <p>按部门权限查看发送轨迹；号码在列表与详情中始终保持掩码。</p>
    </div>
    <span class="batch-scope"><i></i>当前口径 · {{ session.roleLabel }}</span>
  </section>

  <form class="batch-filter batch-filter-bar" @submit.prevent="search">
    <div class="batch-fld">
      <span>批次号</span>
      <el-input v-model="batchNo" class="batch-filter-search" placeholder="模糊匹配批次号" clearable maxlength="64" />
    </div>
    <div class="batch-fld">
      <span>类别</span>
      <div class="batch-seg" role="group" aria-label="类别" data-testid="batch-category-filter">
        <button
          v-for="opt in categoryOptions"
          :key="opt.value"
          type="button"
          :class="{ on: category === opt.value }"
          @click="category = opt.value"
        >{{ opt.label }}</button>
      </div>
    </div>
    <div class="batch-fld">
      <span>渠道</span>
      <div class="batch-seg" role="group" aria-label="渠道" data-testid="batch-channel-filter">
        <button
          v-for="opt in channelOptions"
          :key="opt.value"
          type="button"
          :class="{ on: channel === opt.value }"
          @click="channel = opt.value"
        >{{ opt.label }}</button>
      </div>
    </div>
    <div class="batch-fld">
      <span>创建时间</span>
      <el-date-picker v-model="range" type="datetimerange" format="YYYY-MM-DD HH:mm" popper-class="qingluan-date-popper" start-placeholder="创建开始" end-placeholder="创建结束" range-separator="至" class="batch-filter-dates" />
    </div>
    <div class="batch-fld">
      <el-popover v-model:visible="moreOpen" placement="bottom-end" :width="320" trigger="click">
        <template #reference>
          <button type="button" class="batch-more-trigger" :class="{ 'is-more-active': moreActive }" data-testid="batch-more-filters">更多筛选 ▾<b v-if="moreActiveCount" class="batch-more-count">{{ moreActiveCount }}</b></button>
        </template>
        <div class="batch-more">
          <label>测试发送</label>
          <div class="batch-seg" role="group" aria-label="测试发送" data-testid="batch-is-test-filter">
            <button
              v-for="opt in isTestOptions"
              :key="opt.value"
              type="button"
              :class="{ on: isTest === opt.value }"
              @click="isTest = opt.value"
            >{{ opt.label }}</button>
          </div>
          <label>应用</label>
          <el-select v-if="appOptions.length" v-model="appId" placeholder="全部应用" clearable filterable data-testid="batch-app-filter">
            <el-option v-for="app in appOptions" :key="app.id" :label="`${app.name}（${app.dept}）`" :value="String(app.id)" />
          </el-select>
          <el-input v-else v-model="appId" inputmode="numeric" placeholder="应用 ID（全部应用留空）" clearable data-testid="batch-app-filter" />
          <template v-if="isAdmin">
            <label>部门</label>
            <el-input v-model="dept" placeholder="全部部门" clearable maxlength="128" data-testid="batch-dept-filter" />
          </template>
        </div>
      </el-popover>
    </div>
    <div class="batch-filter-actions">
      <span v-if="filtersDirty" class="batch-dirty" data-testid="batch-filters-dirty">● 条件已变更</span>
      <el-button type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button @click="reset">重置</el-button>
    </div>
  </form>

  <div class="batch-chips" data-testid="batch-status-chips" role="group" aria-label="按状态分组筛选">
    <span class="batch-chips-lbl">状态</span>
    <button
      v-for="group in statusGroups"
      :key="group.key"
      type="button"
      class="batch-chip"
      :class="{ on: statusGroup === group.key, hot: group.key === 'balance_blocked' && (groupCount(group.key, group.statuses) ?? 0) > 0 }"
      :data-testid="`batch-chip-${group.key}`"
      @click="selectGroup(group.key)"
    >
      {{ group.label }}<b v-if="groupCount(group.key, group.statuses) !== null">{{ groupCount(group.key, group.statuses) }}</b>
    </button>
    <span class="batch-chips-meta">分组 = 进行中(queued+sending) · 待审批 · 已排期 · 余额阻断 · 已完成 · 其他终态(cancelled+rejected+expired)</span>
  </div>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" class="batch-error" />
  <div class="batch-ledger">
    <el-table v-loading="loading" :data="items" row-key="batch_no" class="query-table">
      <el-table-column label="批次 / 时间" min-width="248">
        <template #default="{ row }">
          <code class="batch-code">{{ row.batch_no }}</code>
          <div v-if="row.scheduled_at && row.status === 'scheduled' || row.is_test || row.resend_of" class="cell-subline">
            <small class="mono-time">{{ formatDateTime(row.created_at) }}</small>
            <span v-if="row.scheduled_at && row.status === 'scheduled'" class="cell-flag cell-flag--sched">定时 {{ formatDateTimeMinute(row.scheduled_at) }}</span>
            <span v-if="row.is_test" class="cell-flag cell-flag--test">测试</span>
            <button v-if="row.resend_of" type="button" class="cell-flag cell-flag--resend" :title="`重发自 ${row.resend_of}`" @click="traceResendOf(row.resend_of)">重发自 {{ shortBatchNo(row.resend_of) }} ↗</button>
          </div>
          <small v-else class="mono-time cell-subline">{{ formatDateTime(row.created_at) }}</small>
        </template>
      </el-table-column>
      <el-table-column label="类别 / 内容" min-width="240">
        <template #default="{ row }">
          <div class="cell-catline">
            <CategoryTag :category="row.category" />
            <p class="cell-content" :title="row.content">{{ row.content }}</p>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="来源" min-width="150">
        <template #default="{ row }">
          <div class="cell-src" :title="`${channelLabel[row.channel] || row.channel} · ${row.app_name || row.creator || '—'} · ${row.dept}`">
            {{ channelLabel[row.channel] || row.channel }} · {{ row.app_name || row.creator || "—" }}<span class="cell-src-dept"> · {{ row.dept }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="结果构成（非成功率）" min-width="180">
        <template #default="{ row }">
          <div class="compose-nums">
            <span><b>{{ row.delivered.toLocaleString() }}</b> / {{ row.total.toLocaleString() }}</span>
            <span v-if="row.failed > 0" class="is-failed">失败 {{ row.failed.toLocaleString() }}</span>
            <span v-else-if="row.status === 'scheduled' || row.status === 'pending_approval'" class="compose-hint">{{ row.status === "scheduled" ? "待进入流水线" : "待审批" }}</span>
          </div>
          <div class="compose" role="img" :aria-label="composeText(row)">
            <i class="compose-p" :style="{ width: composePct(composeOf(row).pending, row) }"></i>
            <i class="compose-s" :style="{ width: composePct(composeOf(row).sent, row) }"></i>
            <i class="compose-d" :style="{ width: composePct(composeOf(row).delivered, row) }"></i>
            <i class="compose-f" :style="{ width: composePct(composeOf(row).failed, row) }"></i>
            <i class="compose-u" :style="{ width: composePct(composeOf(row).unknown, row) }"></i>
            <i class="compose-o" :style="{ width: composePct(composeOf(row).other, row) }"></i>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="112">
        <template #default="{ row }">
          <StatusTag :status="row.status" :label="statusLabel[row.status] || row.status" />
          <small v-if="row.deferred_reason === 'market_window'" class="cell-deferred">窗外转定时</small>
        </template>
      </el-table-column>
      <el-table-column label="计费条" width="90" align="right">
        <template #default="{ row }"><span class="mono-value">{{ row.quota_cost.toLocaleString() }}</span></template>
      </el-table-column>
      <el-table-column label="操作" width="92" fixed="right">
        <template #default="{ row }"><el-button link type="primary" @click="openBatch(row)">查看详情</el-button></template>
      </el-table-column>
      <template #empty><EmptyState title="没有符合条件的批次" description="调整筛选条件后重新查询。" /></template>
    </el-table>
    <div class="query-mobile-list">
      <article v-for="item in items" :key="item.batch_no">
        <header>
          <code>{{ item.batch_no }}</code>
          <StatusTag :status="item.status" :label="statusLabel[item.status] || item.status" />
        </header>
        <p class="cell-content">{{ item.content }}</p>
        <div class="compose" role="img" :aria-label="composeText(item)">
          <i class="compose-p" :style="{ width: composePct(composeOf(item).pending, item) }"></i>
          <i class="compose-s" :style="{ width: composePct(composeOf(item).sent, item) }"></i>
          <i class="compose-d" :style="{ width: composePct(composeOf(item).delivered, item) }"></i>
          <i class="compose-f" :style="{ width: composePct(composeOf(item).failed, item) }"></i>
          <i class="compose-u" :style="{ width: composePct(composeOf(item).unknown, item) }"></i>
          <i class="compose-o" :style="{ width: composePct(composeOf(item).other, item) }"></i>
        </div>
        <p class="query-mobile-meta">
          {{ CATEGORY_LABELS[item.category] }} · {{ channelLabel[item.channel] || item.channel }} · {{ item.dept }} · 送达 {{ item.delivered }}/{{ item.total }}<template v-if="item.failed > 0"> · 失败 {{ item.failed }}</template>
          <span v-if="item.is_test" class="cell-flag cell-flag--test">测试</span>
        </p>
        <footer><time>{{ formatDateTime(item.created_at) }}</time><el-button link type="primary" @click="openBatch(item)">查看详情</el-button></footer>
      </article>
    </div>
    <footer class="batch-pager">
      <div class="compose-legend" aria-hidden="true">
        <span><i class="compose-p"></i>待处理</span>
        <span><i class="compose-s"></i>待回执</span>
        <span><i class="compose-d"></i>送达</span>
        <span><i class="compose-f"></i>失败</span>
        <span><i class="compose-u"></i>未知</span>
        <span><i class="compose-o"></i>其他</span>
        <em>构成 = 占受理总数的份额，不是成功率；成功率口径见统计报表</em>
      </div>
      <span>共 {{ total }} 个批次 · 每页 20</span>
      <el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </div>

  <el-drawer v-model="drawer" size="min(560px, 92vw)" :teleported="false" class="batch-drawer">
    <template #header>
      <div v-if="selected" class="batch-drawer-head">
        <StatusTag :status="selected.status" :label="statusLabel[selected.status] || selected.status" />
        <code>{{ selected.batch_no }}</code>
        <small>创建于 {{ formatDateTime(selected.created_at) }} · {{ channelLabel[selected.channel] || selected.channel }} · {{ selected.dept }}</small>
      </div>
      <span v-else>批次详情</span>
    </template>
    <template v-if="selected">
      <div v-if="canWrite" class="batch-actions">
        <el-button type="danger" plain :disabled="!canScheduleOps" data-testid="cancel-batch" @click="cancelSelected">取消批次</el-button>
        <el-button :disabled="!canScheduleOps" data-testid="reschedule-batch" @click="openReschedule">改期</el-button>
        <el-button v-if="selected.failed > 0" :disabled="!canResendFailed" data-testid="resend-failed" @click="resendFailed">重发失败（{{ selected.failed.toLocaleString() }}）</el-button>
        <p class="batch-actions-why">取消 / 改期仅「已排期」批次可用（服务端 409 为最终裁决）；重发失败将生成新批次并完整重走频控、审批与时间窗。</p>
      </div>

      <section class="batch-hero">
        <div class="compose compose--lg" role="img" :aria-label="composeText(selected)">
          <i class="compose-p" :style="{ width: composePct(composeOf(selected).pending, selected) }"></i>
          <i class="compose-s" :style="{ width: composePct(composeOf(selected).sent, selected) }"></i>
          <i class="compose-d" :style="{ width: composePct(composeOf(selected).delivered, selected) }"></i>
          <i class="compose-f" :style="{ width: composePct(composeOf(selected).failed, selected) }"></i>
          <i class="compose-u" :style="{ width: composePct(composeOf(selected).unknown, selected) }"></i>
          <i class="compose-o" :style="{ width: composePct(composeOf(selected).other, selected) }"></i>
        </div>
        <div class="batch-hero-nums">
          <div><span>待处理</span><b>{{ composeOf(selected).pending.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).pending / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
          <div><span>待回执</span><b>{{ composeOf(selected).sent.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).sent / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
          <div><span>送达</span><b>{{ composeOf(selected).delivered.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).delivered / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
          <div><span>失败</span><b :class="{ 'is-failed': composeOf(selected).failed > 0 }">{{ composeOf(selected).failed.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).failed / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
          <div><span>未知</span><b>{{ composeOf(selected).unknown.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).unknown / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
          <div><span>其他</span><b>{{ composeOf(selected).other.toLocaleString() }}</b><small>{{ selected.total > 0 ? (composeOf(selected).other / selected.total * 100).toFixed(1) + "%" : "—" }}</small></div>
        </div>
        <p class="batch-hero-quotas">受理 <b>{{ selected.total.toLocaleString() }}</b> · 计费条 <b>{{ selected.quota_cost.toLocaleString() }}</b> · 单条 <b>{{ selected.segments }}</b> 条 · 频控剔除 <b>{{ selected.removed_freq_limit.toLocaleString() }}</b></p>
      </section>

      <p
        v-if="selected.status === 'sending' && activeOf(selected) > 0"
        class="batch-note"
      >仍有 {{ activeOf(selected).toLocaleString() }} 条未终态（待处理 {{ composeOf(selected).pending.toLocaleString() }} + 待回执 {{ composeOf(selected).sent.toLocaleString() }}），批次保持发送中，直至提交完成、结果核对、回执到达或报告超时。构成非成功率。</p>
      <p v-if="selected.deferred_reason === 'market_window'" class="batch-note is-warn">营销时间窗外，已转为定时发送；到达营销窗口后自动进入队列。</p>

      <section class="batch-content-card">
        <CategoryTag :category="selected.category" />
        <p>{{ selected.content }}</p>
        <small>内容长度 {{ selected.content.length }} 字 · 单条 {{ selected.segments }} 计费条<template v-if="selected.category === 'verify'"> · 验证码已等长打码</template></small>
      </section>

      <dl class="batch-facts">
        <div><dt>渠道</dt><dd>{{ channelLabel[selected.channel] || selected.channel }}</dd></div>
        <div><dt>应用</dt><dd>{{ selected.app_name || "—" }}</dd></div>
        <div><dt>创建人</dt><dd>{{ selected.creator || "—" }}</dd></div>
        <div><dt>部门</dt><dd>{{ selected.dept }}</dd></div>
        <div><dt>创建时间</dt><dd>{{ formatDateTime(selected.created_at) }}</dd></div>
        <div><dt>定时时间</dt><dd>{{ selected.scheduled_at ? formatDateTime(selected.scheduled_at) : "—" }}</dd></div>
        <div>
          <dt>重发溯源</dt>
          <dd><button v-if="selected.resend_of" type="button" class="batch-trace" @click="traceResendOf(selected.resend_of!)">{{ selected.resend_of }} ↗</button><template v-else>—</template></dd>
        </div>
        <div><dt>标记</dt><dd>{{ selected.is_test ? "测试发送" : "正式发送" }}</dd></div>
      </dl>

      <div class="batch-detail-head"><h3>号码明细</h3><el-select v-model="detailStatus" data-testid="batch-detail-status" style="width: 128px" placeholder="全部状态" clearable @change="filterDetails"><el-option v-for="option in detailStatusOptions" :key="option.value" :label="option.label" :value="option.value" /></el-select></div>
      <el-table v-loading="detailsLoading" :data="details" row-key="id"><el-table-column label="手机号" min-width="190"><template #default="{ row }"><PhoneReveal v-if="canDecrypt" :masked="row.phone" :reveal="() => revealPhone(row.id)" :testid="`batch-phone-decrypt-${row.id}`" /><PhoneMask v-else :value="row.phone" /></template></el-table-column><el-table-column label="状态" width="100"><template #default="{ row }"><StatusTag :status="row.status" :label="statusLabel[row.status] || row.status" /></template></el-table-column><el-table-column prop="report_desc" label="回执" min-width="150" /><el-table-column label="回执时间" min-width="178"><template #default="{ row }">{{ formatDateTime(row.report_time) }}</template></el-table-column><template #empty><EmptyState title="没有符合条件的明细" description="调整状态筛选后查看。" /></template></el-table>
      <footer class="query-pagination batch-detail-pagination"><span>共 {{ detailTotal }} 条 · 每页 20</span><el-pagination v-model:current-page="detailPage" :page-size="20" :total="detailTotal" layout="prev, pager, next" @current-change="loadDetails" /></footer>
    </template>
  </el-drawer>
  <el-dialog v-model="rescheduleOpen" title="批次改期" width="min(480px, 92vw)"><el-date-picker v-model="scheduledAt" type="datetime" popper-class="qingluan-date-popper" value-format="YYYY-MM-DDTHH:mm:ss+08:00" placeholder="选择新的发送时间" /><template #footer><el-button @click="rescheduleOpen=false">取消</el-button><el-button type="primary" :disabled="!scheduledAt" @click="saveReschedule">确认改期</el-button></template></el-dialog>
</template>
