<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import {
  decideApproval,
  listApprovals,
  type ApprovalItem,
  type ApprovalStatus,
} from "../api/approvals"
import { useSessionStore } from "../stores/session"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"

const session = useSessionStore()
const status = ref<ApprovalStatus>("pending")
const page = ref(1)
const total = ref(0)
const items = ref<ApprovalItem[]>([])
const selected = ref<ApprovalItem | null>(null)
const drawerOpen = ref(false)
const loading = ref(false)
const errorMessage = ref("")

const tabs: Array<{ value: ApprovalStatus; label: string }> = [
  { value: "pending", label: "待我审批" },
  { value: "approved", label: "已通过" },
  { value: "rejected", label: "已驳回" },
  { value: "expired", label: "已过期" },
]

const canApprove = computed(
  () => session.role !== null && ["approver", "admin"].includes(session.role),
)

function canDecide(item: ApprovalItem): boolean {
  return canApprove.value && item.status === "pending" && item.applicant !== session.username
}

function categoryLabel(category: ApprovalItem["category"]): string {
  return category === "market" ? "营销" : "通知"
}

function triggerRule(item: ApprovalItem): string {
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

function formatSchedule(value: string | null | undefined): string {
  if (value === undefined) return "计划暂不可用"
  return value ? formatTime(value) : "立即发送"
}

function formatSegments(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString()} 条`
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listApprovals(status.value, page.value)
    items.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "审批列表加载失败"
  } finally {
    loading.value = false
  }
}

async function changeStatus(value: string | number): Promise<void> {
  status.value = String(value) as ApprovalStatus
  page.value = 1
  await load()
}

function showDetail(item: ApprovalItem): void {
  selected.value = item
  drawerOpen.value = true
}

async function approve(item: ApprovalItem): Promise<void> {
  await ElMessageBox.confirm(`确认通过批次 ${item.batch_no}？`, "审批确认", {
    confirmButtonText: "确认通过",
    cancelButtonText: "取消",
    type: "warning",
  })
  await decideApproval(item.id, "approve")
  ElMessage.success("审批已通过，批次将按类别进入发送队列")
  await load()
}

async function reject(item: ApprovalItem): Promise<void> {
  const result = await ElMessageBox.prompt("请填写明确的驳回原因", "驳回审批", {
    confirmButtonText: "确认驳回",
    cancelButtonText: "取消",
    inputPattern: /\S+/,
    inputErrorMessage: "驳回原因不能为空",
  })
  await decideApproval(item.id, "reject", result.value)
  ElMessage.success("审批已驳回，配额将由服务端幂等回补")
  await load()
}

onMounted(load)
</script>

<template>
  <section class="page-heading approval-heading">
    <div>
      <p class="eyebrow">GOVERNANCE / 审批流转</p>
      <h1>审批中心</h1>
      <p>查看短信内容、受众规模与申请人，决策动作全量留痕且禁止自审。</p>
    </div>
    <span class="approval-role">当前身份 · {{ session.roleLabel }}</span>
  </section>

  <el-card shadow="never" class="approval-card">
    <div class="approval-toolbar filter-toolbar">
      <el-segmented
        :model-value="status"
        :options="tabs"
        value-key="value"
        label-key="label"
        @change="changeStatus"
      />
      <span><b>{{ total }}</b> 条记录</span>
    </div>

    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

    <section v-if="status === 'pending'" v-loading="loading" class="approval-queue">
      <article v-for="item in items" :key="item.id" class="approval-item">
        <header>
          <CategoryTag :category="item.category" />
          <button
            :data-testid="`approval-detail-${item.id}`"
            class="approval-batch"
            type="button"
            :aria-label="`查看批次 ${item.batch_no} 的审批详情`"
            @click="showDetail(item)"
          >{{ item.batch_no }}</button>
          <span>提交人 {{ item.applicant }}</span>
          <span>部门 {{ item.dept }}</span>
          <time>{{ formatTime(item.created_at) }}</time>
        </header>
        <dl class="approval-facts">
          <div><dt>受理号码</dt><dd>{{ item.total.toLocaleString() }}</dd></div>
          <div><dt>预计计费</dt><dd>{{ formatSegments(item.estimated_segments) }}</dd></div>
          <div><dt>发送计划</dt><dd>{{ formatSchedule(item.scheduled_at) }}</dd></div>
          <div><dt>触发规则</dt><dd>{{ triggerRule(item) }}</dd></div>
        </dl>
        <blockquote class="approval-quote">{{ item.content }}</blockquote>
        <footer>
          <div class="approval-compliance"><el-tag effect="plain">服务端预检</el-tag><el-tag effect="plain">决策写审计</el-tag><el-tag effect="plain">禁止本人审批</el-tag></div>
          <div v-if="canDecide(item)" :data-testid="`approval-actions-${item.id}`" class="approval-actions">
            <el-button plain type="danger" @click="reject(item)">驳回</el-button>
            <el-button type="primary" @click="approve(item)">通过</el-button>
          </div>
          <span v-else class="self-note approval-avoid">本人提交 · 按规则回避，待其他管理员审批</span>
        </footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="当前没有待审批记录" description="新的审批申请会出现在这里。" />
    </section>

    <el-table v-else v-loading="loading" :data="items" row-key="id" @row-click="showDetail">
      <el-table-column label="批次 / 申请时间" min-width="190">
        <template #default="{ row }">
          <button
            :data-testid="`approval-detail-${row.id}`"
            class="table-row-detail batch-cell"
            type="button"
            :aria-label="`查看批次 ${row.batch_no} 的审批详情`"
            @click.stop="showDetail(row)"
            @keydown.enter.stop.prevent="showDetail(row)"
            @keydown.space.stop.prevent="showDetail(row)"
          >
            <b>{{ row.batch_no }}</b><small>{{ formatTime(row.created_at) }}</small>
          </button>
        </template>
      </el-table-column>
      <el-table-column label="类别" width="86">
        <template #default="{ row }"><CategoryTag :category="row.category" /></template>
      </el-table-column>
      <el-table-column prop="applicant" label="申请人" min-width="120" />
      <el-table-column prop="dept" label="部门" min-width="130" />
      <el-table-column prop="total" label="受众" width="100" align="right" />
      <el-table-column label="状态" width="105">
        <template #default="{ row }"><StatusTag :status="row.status" /></template>
      </el-table-column>
      <el-table-column label="操作" width="168" fixed="right">
        <template #default="{ row }">
          <div v-if="canDecide(row)" :data-testid="`approval-actions-${row.id}`" @click.stop>
            <el-button link type="primary" @click="approve(row)">通过</el-button>
            <el-button link type="danger" @click="reject(row)">驳回</el-button>
          </div>
          <span v-else-if="row.status === 'pending' && row.applicant === session.username" class="self-note">本人提交</span>
          <el-button v-else link @click.stop="showDetail(row)">查看</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="当前分类没有审批记录" description="新的审批申请会出现在这里。" /></template>
    </el-table>

    <div v-if="status !== 'pending'" v-loading="loading" class="approval-mobile-list">
      <article v-for="item in items" :key="item.id">
        <header>
          <CategoryTag :category="item.category" />
          <StatusTag :status="item.status" />
        </header>
        <strong>{{ item.batch_no }}</strong>
        <time>{{ formatTime(item.created_at) }}</time>
        <dl>
          <div><dt>申请人</dt><dd>{{ item.applicant }}</dd></div>
          <div><dt>所属部门</dt><dd>{{ item.dept }}</dd></div>
          <div><dt>接收数量</dt><dd>{{ item.total.toLocaleString() }}</dd></div>
        </dl>
        <footer>
          <el-button
            :data-testid="`mobile-approval-detail-${item.id}`"
            link
            type="primary"
            @click="showDetail(item)"
          >查看详情</el-button>
          <div
            v-if="canDecide(item)"
            :data-testid="`mobile-approval-actions-${item.id}`"
            class="approval-mobile-actions"
            @click.stop
          >
            <el-button :data-testid="`mobile-approval-approve-${item.id}`" link type="primary" @click="approve(item)">通过</el-button>
            <el-button :data-testid="`mobile-approval-reject-${item.id}`" link type="danger" @click="reject(item)">驳回</el-button>
          </div>
          <span v-else-if="item.status === 'pending' && item.applicant === session.username" class="self-note">本人提交</span>
        </footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="当前分类没有审批记录" description="新的审批申请会出现在这里。" />
    </div>

    <el-pagination
      v-if="total > 20"
      v-model:current-page="page"
      :page-size="20"
      :total="total"
      layout="prev, pager, next"
      @current-change="load"
    />
  </el-card>

  <el-drawer v-model="drawerOpen" title="审批详情" size="min(480px, 92vw)">
    <template v-if="selected">
      <div class="approval-detail-head">
        <StatusTag :status="selected.status" />
        <b>{{ selected.batch_no }}</b>
      </div>
      <dl class="approval-detail-grid">
        <div><dt>消息类别</dt><dd>{{ categoryLabel(selected.category) }}</dd></div>
        <div><dt>接收数量</dt><dd>{{ selected.total.toLocaleString() }}</dd></div>
        <div><dt>单号计费</dt><dd>{{ formatSegments(selected.segments) }}</dd></div>
        <div><dt>预计计费</dt><dd>{{ formatSegments(selected.estimated_segments) }}</dd></div>
        <div><dt>发送计划</dt><dd>{{ formatSchedule(selected.scheduled_at) }}</dd></div>
        <div><dt>触发规则</dt><dd>{{ triggerRule(selected) }}</dd></div>
        <div><dt>申请人</dt><dd>{{ selected.applicant }}</dd></div>
        <div><dt>所属部门</dt><dd>{{ selected.dept }}</dd></div>
        <div><dt>申请时间</dt><dd>{{ formatTime(selected.created_at) }}</dd></div>
        <div v-if="selected.approver"><dt>审批人</dt><dd>{{ selected.approver }}</dd></div>
      </dl>
      <div class="content-proof"><span>待审内容</span><p>{{ selected.content }}</p></div>
      <div v-if="selected.reason" class="reason-proof"><span>审批意见</span><p>{{ selected.reason }}</p></div>
      <div v-if="canDecide(selected)" class="drawer-actions">
        <el-button type="danger" plain @click="reject(selected)">驳回</el-button>
        <el-button type="primary" @click="approve(selected)">通过并入队</el-button>
      </div>
      <el-alert
        v-else-if="selected.status === 'pending' && selected.applicant === session.username"
        title="这是本人提交的审批单，平台已隐藏决策操作"
        type="warning"
        :closable="false"
      />
    </template>
  </el-drawer>
</template>
