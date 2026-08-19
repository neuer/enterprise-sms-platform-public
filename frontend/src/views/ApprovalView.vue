<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage } from "element-plus"
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import {
  decideApproval,
  getApproval,
  listApprovals,
  type ApprovalAction,
  type ApprovalCounts,
  type ApprovalDetail,
  type ApprovalListItem,
  type ApprovalSort,
  type ApprovalStatus,
  type DecisionOutcome,
} from "../api/approvals"
import { ApiRequestError, type Category } from "../api/webMessages"
import ApprovalList from "../components/ApprovalList.vue"
import StatusTag from "../components/StatusTag.vue"
import { useApprovalBadgeStore } from "../stores/approvalBadge"
import { useSessionStore } from "../stores/session"

const REASON_MAX_LENGTH = 256
const PAGE_SIZE = 20
const POLL_INTERVAL_MS = 30_000
const TICK_INTERVAL_MS = 1_000

const session = useSessionStore()
const approvalBadge = useApprovalBadgeStore()

const status = ref<ApprovalStatus>("pending")
const category = ref<Category | "">("")
const dept = ref("")
const q = ref("")
const sort = ref<ApprovalSort>("expires_asc")
const page = ref(1)
const total = ref(0)
const counts = ref<ApprovalCounts>({
  pending: 0,
  approved: 0,
  rejected: 0,
  expired: 0,
  pending_urgent: 0,
})
const items = ref<ApprovalListItem[]>([])
const loading = ref(false)
const errorMessage = ref("")
const decidingId = ref<number | null>(null)
const now = ref(Date.now())

const drawerOpen = ref(false)
const selected = ref<ApprovalListItem | null>(null)
const detail = ref<ApprovalDetail | null>(null)
const detailLoading = ref(false)
const decisionReason = ref("")

const statusTabs: Array<{ value: ApprovalStatus; label: string }> = [
  { value: "pending", label: "待审批" },
  { value: "approved", label: "已通过" },
  { value: "rejected", label: "已驳回" },
  { value: "expired", label: "已过期" },
]

const sortOptions: Array<{ value: ApprovalSort; label: string }> = [
  { value: "expires_asc", label: "临期优先" },
  { value: "created_desc", label: "最新提交" },
  { value: "decided_desc", label: "最近决策" },
]

const canApprove = computed(
  () => session.role !== null && ["approver", "admin"].includes(session.role),
)

const decisionReasonTrimmed = computed(() => decisionReason.value.trim())

const canDecideSelected = computed(
  () =>
    canApprove.value &&
    selected.value !== null &&
    selected.value.status === "pending" &&
    selected.value.applicant !== session.username,
)

const selectedCountdown = computed(() => {
  if (!selected.value?.expires_at || selected.value.status !== "pending") return null
  const remaining = new Date(selected.value.expires_at).getTime() - now.value
  if (Number.isNaN(remaining) || remaining <= 0) return "已临期截止"
  const totalSeconds = Math.floor(remaining / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((unit) => String(unit).padStart(2, "0")).join(":")
})

function countOf(value: unknown): number {
  return counts.value[String(value) as ApprovalStatus] ?? 0
}

function urgentOf(value: unknown): number {
  return value === "pending" ? counts.value.pending_urgent : 0
}

function categoryLabel(category: Category): string {
  return category === "market" ? "营销" : "通知"
}

function triggerRule(item: ApprovalListItem): string {
  if (item.trigger_threshold_source === "legacy_unknown" || item.trigger_threshold === null) {
    return "历史阈值不可确认"
  }
  return `${categoryLabel(item.category)} ≥ ${item.trigger_threshold} 个号码`
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .format(new Date(value))
    .replaceAll("/", "-")
}

function formatSchedule(value: string | null): string {
  return value ? formatTime(value) : "立即发送"
}

function formatSegments(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString()} 条`
}

function deciderLabel(item: ApprovalListItem): string | null {
  if (item.approver) return item.approver
  if (item.status === "expired") return "系统自动"
  return null
}

let loadToken = 0
let detailToken = 0
let pollTimer: number | undefined
let tickTimer: number | undefined

async function load(options: { silent?: boolean } = {}): Promise<void> {
  const token = ++loadToken
  if (!options.silent) loading.value = true
  errorMessage.value = ""
  try {
    const result = await listApprovals({
      status: status.value,
      page: page.value,
      size: PAGE_SIZE,
      category: category.value || undefined,
      dept: dept.value,
      q: q.value,
      sort: sort.value,
    })
    if (token !== loadToken) return
    items.value = result.items
    total.value = result.total
    counts.value = result.counts
    approvalBadge.pending = result.counts.pending
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "审批列表加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function onStatusChange(value: string | number): void {
  const next = String(value) as ApprovalStatus
  if (next === status.value) return
  status.value = next
  sort.value = next === "pending" ? "expires_asc" : "decided_desc"
  page.value = 1
  void load()
}

function applyFilters(): void {
  page.value = 1
  void load()
}

function onPageChange(): void {
  void load()
}

async function showDetail(item: ApprovalListItem): Promise<void> {
  selected.value = item
  detail.value = null
  decisionReason.value = ""
  drawerOpen.value = true
  const token = ++detailToken
  detailLoading.value = true
  try {
    const result = await getApproval(item.id)
    if (token !== detailToken) return
    detail.value = result
  } catch (error) {
    if (token !== detailToken) return
    drawerOpen.value = false
    if (error instanceof ApiRequestError && error.status === 409) {
      ElMessage.warning("该审批单已被处理或状态已变化，列表已刷新")
    } else if (error instanceof ApiRequestError && error.status === 404) {
      ElMessage.warning("该审批单已不存在，列表已刷新")
    } else {
      ElMessage.error(error instanceof Error ? error.message : "审批详情加载失败，请稍后重试")
    }
    void load({ silent: true })
  } finally {
    if (token === detailToken) detailLoading.value = false
  }
}

function closeDrawer(): void {
  drawerOpen.value = false
  selected.value = null
  detail.value = null
  decisionReason.value = ""
  detailToken += 1
}

/** 决策后去向文案：以 decision 响应回带的 batch_status / deferred_reason 为准，不靠猜。 */
function outcomeText(outcome: DecisionOutcome, lane: string): string {
  if (outcome.batch_status === "scheduled") {
    return outcome.deferred_reason === "market_window"
      ? "当前处于营销时间窗外，批次已改派为定时发送"
      : "批次已进入定时发送计划"
  }
  if (outcome.batch_status === "queued") return `批次已进入发送队列（${lane}）`
  return "批次已受理进入发送流程"
}

function laneOf(id: number): string {
  const item = items.value.find((entry) => entry.id === id) ?? detail.value ?? selected.value
  return item?.category === "market" ? "bulk" : "realtime"
}

async function submitDecision(id: number, action: ApprovalAction, reason?: string): Promise<void> {
  if (decidingId.value !== null) return
  decidingId.value = id
  try {
    const outcome = await decideApproval(id, action, reason)
    if (action === "approve") {
      ElMessage.success(`审批已通过，${outcomeText(outcome, laneOf(id))}`)
    } else {
      ElMessage.success("审批已驳回，配额将由服务端幂等回补")
    }
    closeDrawer()
    await load({ silent: true })
  } catch (error) {
    if (error instanceof ApiRequestError && error.status === 409) {
      ElMessage.warning("该审批单已被处理或状态已变化，列表已刷新")
      closeDrawer()
      await load({ silent: true })
    } else if (error instanceof ApiRequestError && error.status === 403) {
      ElMessage.error(error.message || "不能审批本人提交的审批单")
    } else {
      ElMessage.error(error instanceof Error ? error.message : "审批操作失败，请稍后重试")
    }
  } finally {
    decidingId.value = null
  }
}

function onQuick(item: ApprovalListItem, action: ApprovalAction, reason?: string): void {
  void submitDecision(item.id, action, reason)
}

function submitDrawerDecision(action: ApprovalAction): void {
  if (!selected.value) return
  const reason = decisionReasonTrimmed.value
  if (action === "reject" && !reason) return
  if (reason.length > REASON_MAX_LENGTH) return
  void submitDecision(selected.value.id, action, reason || undefined)
}

onMounted(() => {
  void load()
  pollTimer = window.setInterval(() => void load({ silent: true }), POLL_INTERVAL_MS)
  tickTimer = window.setInterval(() => {
    now.value = Date.now()
  }, TICK_INTERVAL_MS)
})

onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
  if (tickTimer !== undefined) window.clearInterval(tickTimer)
})
</script>

<template>
  <section class="page-heading approval-heading">
    <div>
      <p class="eyebrow">GOVERNANCE / 审批流转</p>
      <h1>审批中心</h1>
      <p>Web 发送达阈值进入审批；超时未决自动过期并释放配额。</p>
    </div>
    <div class="approval-head-side">
      <span class="approval-counts-pill" data-testid="approval-counts-pill"><b>{{ counts.pending }}</b> 待审 · <span :class="{ 'is-hot': counts.pending_urgent > 0 }">{{ counts.pending_urgent }} 临期</span></span>
      <span class="approval-role">当前身份 · {{ session.roleLabel }}</span>
    </div>
  </section>

  <el-card shadow="never" class="approval-card">
    <div class="approval-toolbar filter-toolbar">
      <el-segmented
        :model-value="status"
        :options="statusTabs"
        class="approval-status-seg"
        data-testid="approval-status-seg"
        @change="onStatusChange"
      >
        <template #default="{ item }">
          <span>{{ item.label }}</span>
          <span class="approval-seg-count" :class="{ 'is-hot': urgentOf(item.value) > 0 }">{{ countOf(item.value) }}</span>
          <span v-if="urgentOf(item.value) > 0" class="approval-seg-urgent">{{ urgentOf(item.value) }} 临期</span>
        </template>
      </el-segmented>
      <div class="approval-toolbar-fields">
        <label for="approval-category">类别</label>
        <el-select id="approval-category" v-model="category" data-testid="approval-category-filter" @change="applyFilters">
          <el-option label="全部类别" value="" />
          <el-option label="通知" value="notice" />
          <el-option label="营销" value="market" />
        </el-select>
        <label for="approval-dept">部门</label>
        <el-input
          id="approval-dept"
          v-model="dept"
          placeholder="模糊匹配部门"
          clearable
          maxlength="32"
          data-testid="approval-dept-filter"
          @change="applyFilters"
          @clear="applyFilters"
        />
        <label for="approval-q">申请人</label>
        <el-input
          id="approval-q"
          v-model="q"
          placeholder="登录名"
          clearable
          maxlength="32"
          data-testid="approval-q-filter"
          @change="applyFilters"
          @clear="applyFilters"
        />
        <label for="approval-sort">排序</label>
        <el-select id="approval-sort" v-model="sort" data-testid="approval-sort-filter" @change="applyFilters">
          <el-option v-for="option in sortOptions" :key="option.value" :label="option.label" :value="option.value" />
        </el-select>
      </div>
      <div class="approval-toolbar-side">
        <el-button data-testid="approval-refresh" :loading="loading" @click="void load()">刷新</el-button>
        <span class="approval-poll-hint">30s 自动</span>
      </div>
    </div>

    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

    <ApprovalList
      v-loading="loading"
      :status="status"
      :items="items"
      :now="now"
      :loading="loading"
      :deciding-id="decidingId"
      :current-username="session.username"
      @detail="showDetail"
      @quick="onQuick"
    />

    <el-pagination
      v-if="total > PAGE_SIZE"
      v-model:current-page="page"
      :page-size="PAGE_SIZE"
      :total="total"
      layout="prev, pager, next"
      @current-change="onPageChange"
    />
  </el-card>

  <el-drawer v-model="drawerOpen" title="审批详情" size="min(480px, 92vw)" @close="closeDrawer">
    <template v-if="selected">
      <div class="approval-detail-head">
        <StatusTag :status="selected.status" />
        <b>{{ selected.batch_no }}</b>
      </div>
      <dl class="approval-detail-grid">
        <div><dt>消息类别</dt><dd>{{ categoryLabel(selected.category) }}</dd></div>
        <div><dt>发送计划</dt><dd>{{ formatSchedule(selected.scheduled_at) }}</dd></div>
        <div><dt>受理号码</dt><dd>{{ selected.total.toLocaleString() }}</dd></div>
        <div><dt>预计计费</dt><dd>{{ formatSegments(selected.estimated_segments) }}</dd></div>
        <div><dt>触发规则</dt><dd>{{ triggerRule(selected) }}</dd></div>
        <div><dt>申请人</dt><dd>{{ selected.applicant }}</dd></div>
        <div><dt>所属部门</dt><dd>{{ selected.dept }}</dd></div>
        <div><dt>申请时间</dt><dd>{{ formatTime(selected.created_at) }}</dd></div>
        <div v-if="selected.expires_at" data-testid="drawer-approval-expiry">
          <dt>审批有效期</dt>
          <dd>至 {{ formatTime(selected.expires_at) }}<template v-if="selectedCountdown">（剩 {{ selectedCountdown }}）</template></dd>
        </div>
        <div v-if="deciderLabel(selected)"><dt>审批人</dt><dd>{{ deciderLabel(selected) }}</dd></div>
        <div v-if="selected.decided_at"><dt>决策时间</dt><dd>{{ formatTime(selected.decided_at) }}</dd></div>
      </dl>
      <div class="content-proof">
        <div class="approval-content-head">
          <span>待审内容（OTP 已等长打码）</span>
          <em>按需解密 · 本次查看已写敏感读审计</em>
        </div>
        <p v-loading="detailLoading" class="approval-content-body" data-testid="approval-detail-content">{{ detail?.content ?? "" }}</p>
      </div>
      <div v-if="selected.reason" class="reason-proof"><span>审批意见</span><p>{{ selected.reason }}</p></div>
      <div v-if="canDecideSelected" class="approval-decide-box" data-testid="drawer-decide-box">
        <el-input
          v-model="decisionReason"
          type="textarea"
          :rows="3"
          maxlength="256"
          show-word-limit
          placeholder="审批意见（通过选填 · 驳回必填，≤256 字）"
          data-testid="drawer-decision-reason"
        />
        <div class="approval-decide-hint">
          <span>决策写审计 · 409 冲突自动刷新队列</span>
          <span>驳回时意见必填</span>
        </div>
        <div class="approval-decide-actions">
          <el-button
            type="danger"
            plain
            :disabled="decidingId !== null || !decisionReasonTrimmed"
            data-testid="drawer-reject"
            @click="submitDrawerDecision('reject')"
          >驳回</el-button>
          <el-button
            type="primary"
            :disabled="decidingId !== null"
            data-testid="drawer-approve"
            @click="submitDrawerDecision('approve')"
          >通过</el-button>
        </div>
      </div>
      <el-alert
        v-else-if="selected.status === 'pending' && selected.applicant === session.username"
        title="本人提交 · 按规则回避，平台已隐藏决策操作"
        type="warning"
        :closable="false"
      />
    </template>
  </el-drawer>
</template>
