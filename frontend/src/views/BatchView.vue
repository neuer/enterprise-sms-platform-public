<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import PhoneMask from "../components/PhoneMask.vue"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"
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
import { useSessionStore } from "../stores/session"

const session = useSessionStore()

const items = ref<BatchItem[]>([])
const total = ref(0)
const page = ref(1)
const category = ref("")
const status = ref("")
const isTest = ref("")
const channel = ref("")
const appId = ref("")
const dept = ref("")
const range = ref<[Date, Date] | null>(null)
const loading = ref(false)
const errorMessage = ref("")
const drawer = ref(false)
const selected = ref<BatchItem | null>(null)
const details = ref<BatchMessage[]>([])
const revealed = ref<Record<number, string>>({})
const rescheduleOpen = ref(false)
const scheduledAt = ref("")
const canWrite = computed(() => session.role === "operator" || session.role === "admin")
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")
const batchDonut = computed(() => {
  if (!selected.value || selected.value.total <= 0) return "conic-gradient(var(--tx-3) 0 100%)"
  const delivered = (selected.value.delivered / selected.value.total) * 100
  const failed = (selected.value.failed / selected.value.total) * 100
  return `conic-gradient(var(--verdi-l) 0 ${delivered}%, var(--verm) ${delivered}% ${delivered + failed}%, var(--tx-3) ${delivered + failed}% 100%)`
})

const categoryLabel: Record<string, string> = { verify: "验证码", notice: "通知", market: "营销" }
const statusLabel: Record<string, string> = {
  pending_approval: "待审批", rejected: "已驳回", scheduled: "定时中", queued: "排队中",
  sending: "发送中", completed: "已完成", cancelled: "已取消", balance_blocked: "余额阻断",
  expired: "已过期", delivered: "已送达", failed: "失败", unknown: "未知", pending: "待发送",
  sent: "已提交", other: "其他",
}

function tagType(value: string): "success" | "warning" | "danger" | "info" {
  if (["completed", "delivered"].includes(value)) return "success"
  if (["pending_approval", "scheduled"].includes(value)) return "warning"
  if (["failed", "rejected", "balance_blocked", "unknown"].includes(value)) return "danger"
  return "info"
}

function formatTime(value: string | null): string {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listBatches({
      page: page.value,
      category: category.value || undefined,
      status: status.value || undefined,
      is_test: isTest.value === "" ? undefined : isTest.value === "true",
      channel: channel.value || undefined,
      app_id: appId.value ? Number(appId.value) : undefined,
      dept: session.role === "admin" && dept.value.trim() ? dept.value.trim() : undefined,
      start: range.value?.[0].toISOString(),
      end: range.value?.[1].toISOString(),
    })
    items.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "批次列表加载失败"
  } finally {
    loading.value = false
  }
}

async function openBatch(item: BatchItem): Promise<void> {
  drawer.value = true
  selected.value = item
  details.value = []
  revealed.value = {}
  try {
    const [batch, messages] = await Promise.all([getBatch(item.batch_no), getBatchMessages(item.batch_no)])
    selected.value = batch
    details.value = messages.items
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "批次详情加载失败"
  }
}

async function revealPhone(message: BatchMessage): Promise<void> {
  try {
    const result = await decryptMessagePhone(message.id)
    revealed.value = { ...revealed.value, [message.id]: result.phone }
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : "授权查看失败" }
}

async function cancelSelected(): Promise<void> {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(`取消批次 ${selected.value.batch_no}？配额将按规则回补。`, "确认取消", { type: "warning" })
    await cancelBatch(selected.value.batch_no)
    drawer.value = false
    ElMessage.success("批次已取消")
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "取消失败") }
}

function openReschedule(): void {
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
  if (!selected.value) return
  try {
    await ElMessageBox.confirm("失败号码将生成新批次并完整重走频控、审批和时间窗。", "确认重发", { type: "warning" })
    const result = await resendFailedBatch(selected.value.batch_no)
    ElMessage.success(`重发批次 ${result.batch_no} 已创建`)
    drawer.value = false
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "重发失败") }
}

function search(): void { page.value = 1; void load() }
function reset(): void { category.value = ""; status.value = ""; isTest.value = ""; channel.value = ""; appId.value = ""; dept.value = ""; range.value = null; search() }
onMounted(load)
</script>

<template>
  <section class="page-heading query-heading">
    <div><p class="eyebrow">BATCH LEDGER / 发送账本</p><h1>批次列表</h1><p>按部门权限查看发送轨迹；号码在列表与详情中始终保持掩码。</p></div>
    <div class="query-total"><span>当前结果</span><strong>{{ total }}</strong><small>个批次</small></div>
  </section>

  <el-card shadow="never" class="query-filter-card">
    <el-form class="query-filter filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-2" label="消息类别"><el-select v-model="category" placeholder="全部类别" clearable><el-option label="验证码" value="verify" /><el-option label="通知" value="notice" /><el-option label="营销" value="market" /></el-select></el-form-item>
      <el-form-item class="filter-span-2" label="批次状态"><el-select v-model="status" placeholder="全部状态" clearable><el-option label="待审批" value="pending_approval" /><el-option label="已驳回" value="rejected" /><el-option label="定时中" value="scheduled" /><el-option label="排队中" value="queued" /><el-option label="发送中" value="sending" /><el-option label="已完成" value="completed" /><el-option label="已取消" value="cancelled" /><el-option label="余额阻断" value="balance_blocked" /><el-option label="已过期" value="expired" /></el-select></el-form-item>
      <el-form-item class="filter-span-2" label="测试发送"><el-select v-model="isTest" placeholder="全部" clearable><el-option label="正式" value="false" /><el-option label="测试" value="true" /></el-select></el-form-item>
      <el-form-item class="filter-span-2" label="渠道"><el-select v-model="channel" placeholder="全部渠道" clearable><el-option label="API" value="api" /><el-option label="Web" value="web" /></el-select></el-form-item>
      <el-form-item class="filter-span-2" label="应用 ID"><el-input v-model="appId" inputmode="numeric" placeholder="全部应用" /></el-form-item>
      <el-form-item v-if="session.role === 'admin'" class="filter-span-2" label="部门"><el-input v-model="dept" placeholder="全部部门" /></el-form-item>
      <el-form-item class="filter-span-4" label="创建时间"><el-date-picker v-model="range" type="datetimerange" popper-class="qingluan-date-popper" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /></el-form-item>
      <el-form-item class="query-filter-actions filter-actions filter-span-2"><el-button type="primary" native-type="submit" :loading="loading">查询</el-button><el-button @click="reset">重置</el-button></el-form-item>
    </el-form>
  </el-card>

  <el-card shadow="never" class="query-table-card">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="batch_no" class="query-table">
      <el-table-column label="批次 / 创建时间" min-width="210"><template #default="{ row }"><code class="batch-code">{{ row.batch_no }}</code><small class="cell-sub">{{ formatTime(row.created_at) }}</small></template></el-table-column>
      <el-table-column label="类别" width="92"><template #default="{ row }"><CategoryTag :category="row.category" /></template></el-table-column>
      <el-table-column prop="dept" label="部门" min-width="120" />
      <el-table-column label="进度" min-width="180"><template #default="{ row }"><strong>{{ row.delivered }}/{{ row.total }}</strong><span class="query-progress"><i :style="{ width: `${row.total ? row.delivered / row.total * 100 : 0}%` }"></i></span></template></el-table-column>
      <el-table-column label="状态" width="105"><template #default="{ row }"><StatusTag :status="row.status" :label="statusLabel[row.status] || row.status" /></template></el-table-column>
      <el-table-column label="计费条" width="90"><template #default="{ row }"><span class="mono-value">{{ row.quota_cost }}</span></template></el-table-column>
      <el-table-column label="操作" width="92" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="openBatch(row)">查看详情</el-button></template></el-table-column>
      <template #empty><EmptyState title="没有符合条件的批次" description="调整筛选条件后重新查询。" /></template>
    </el-table>
    <div class="query-mobile-list"><article v-for="item in items" :key="item.batch_no"><header><code>{{ item.batch_no }}</code><StatusTag :status="item.status" :label="statusLabel[item.status] || item.status" /></header><p>{{ categoryLabel[item.category] }} · {{ item.dept }} · {{ item.delivered }}/{{ item.total }}</p><footer><time>{{ formatTime(item.created_at) }}</time><el-button link type="primary" @click="openBatch(item)">查看详情</el-button></footer></article></div>
    <footer class="query-pagination"><span>第 {{ page }} 页 · 每页 20 条</span><el-pagination v-model:current-page="page" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" /></footer>
  </el-card>

  <el-drawer v-model="drawer" title="批次详情" size="min(560px, 92vw)" :teleported="false" class="batch-drawer">
    <template v-if="selected"><section class="batch-summary"><div><span>批次号</span><code>{{ selected.batch_no }}</code></div><div><span>计费条</span><strong>{{ selected.quota_cost }}</strong></div></section><section class="batch-donut-row"><div class="batch-donut" :style="{ background: batchDonut }"><span>{{ selected.total ? Math.round(selected.delivered / selected.total * 100) : 0 }}%</span></div><dl><div><dt>送达 / 失败 / 未知</dt><dd>{{ selected.delivered }} / {{ selected.failed }} / {{ selected.unknown }}</dd></div><div><dt>受理总数</dt><dd>{{ selected.total }}</dd></div></dl></section><p class="batch-content">{{ selected.content }}</p><div class="batch-actions"><el-button v-if="selected.status === 'scheduled' && canWrite" data-testid="cancel-batch" type="danger" plain @click="cancelSelected">取消批次</el-button><el-button v-if="selected.status === 'scheduled' && canWrite" data-testid="reschedule-batch" @click="openReschedule">改期</el-button><el-button v-if="selected.failed > 0 && canWrite" data-testid="resend-failed" type="primary" @click="resendFailed">失败号码重发</el-button></div></template>
    <el-table :data="details" row-key="id"><el-table-column label="手机号" min-width="190"><template #default="{ row }"><strong v-if="revealed[row.id]" class="revealed-phone">{{ revealed[row.id] }}</strong><PhoneMask v-else :value="row.phone" /><el-button v-if="canDecrypt && !revealed[row.id]" :data-testid="`batch-phone-decrypt-${row.id}`" link type="primary" @click="revealPhone(row)">授权查看</el-button></template></el-table-column><el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="tagType(row.status)">{{ statusLabel[row.status] || row.status }}</el-tag></template></el-table-column><el-table-column prop="report_desc" label="回执" min-width="150" /><el-table-column label="回执时间" min-width="178"><template #default="{ row }">{{ formatTime(row.report_time) }}</template></el-table-column></el-table>
  </el-drawer>
  <el-dialog v-model="rescheduleOpen" title="批次改期" width="min(480px, 92vw)"><el-date-picker v-model="scheduledAt" type="datetime" popper-class="qingluan-date-popper" value-format="YYYY-MM-DDTHH:mm:ss+08:00" placeholder="选择新的发送时间" /><template #footer><el-button @click="rescheduleOpen=false">取消</el-button><el-button type="primary" :disabled="!scheduledAt" @click="saveReschedule">确认改期</el-button></template></el-dialog>
</template>
