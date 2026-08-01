<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import {
  getSecurityDailyOverview,
  getSecurityDailyReport,
  listSecurityDailyReports,
  previewSecurityDailyReport,
  retrySecurityDailyReport,
  sendSecurityDailyReport,
  type DeliveryStatus,
  type GenerationStatus,
  type SecurityDailyOverview,
  type SecurityDailyPayload,
  type SecurityDailyReport,
  type SecurityStatus,
} from "../api/securityDaily"

const statusLabels: Record<SecurityStatus, string> = {
  normal: "正常",
  attention: "关注",
  high: "高风险",
}
const generationLabels: Record<GenerationStatus, string> = {
  pending: "生成中",
  ready: "已生成",
  failed: "生成失败",
  unavailable: "数据不可用",
}
const deliveryLabels: Record<DeliveryStatus, string> = {
  not_sent: "未投递",
  pending: "等待 mailer",
  sending: "投递中",
  sent: "已投递",
  failed: "投递失败",
}

const overview = ref<SecurityDailyOverview | null>(null)
const reports = ref<SecurityDailyReport[]>([])
const total = ref(0)
const page = ref(1)
const dateFrom = ref("")
const dateTo = ref("")
const status = ref<SecurityStatus | "">("")
const generationStatus = ref<GenerationStatus | "">("")
const deliveryStatus = ref<DeliveryStatus | "">("")
const loading = ref(false)
const detailLoading = ref(false)
const errorMessage = ref("")
const selected = ref<SecurityDailyReport | null>(null)
const drawerOpen = ref(false)
const previewText = ref("")
const previewOpen = ref(false)

const selectedPayload = computed<SecurityDailyPayload | null>(() => selected.value?.payload ?? null)
const coverageGaps = computed(() =>
  selectedPayload.value?.coverage.filter(
    (item) => item.tone !== "good" || item.status !== "完整",
  ) ?? [],
)
const overviewStatus = computed(() => overview.value?.delivery_status ?? null)

function statusLabel(value: string): string {
  return statusLabels[value as SecurityStatus] ?? "未知状态"
}

function generationLabel(value: string): string {
  return generationLabels[value as GenerationStatus] ?? "数据不可用"
}

function deliveryLabel(value: string): string {
  return deliveryLabels[value as DeliveryStatus] ?? "数据不可用"
}

function tagType(value: string): "success" | "warning" | "danger" | "info" {
  if (value === "normal" || value === "ready" || value === "sent") return "success"
  if (value === "attention" || value === "pending" || value === "sending") return "warning"
  if (value === "high" || value === "failed") return "danger"
  return "info"
}

function displayMoment(value: string | null | undefined): string {
  if (!value) return "—"
  const parsed = new Date(value)
  return Number.isNaN(parsed.valueOf()) ? "数据不可用" : parsed.toLocaleString("zh-CN", { hour12: false })
}

async function loadReports(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listSecurityDailyReports({
      dateFrom: dateFrom.value || undefined,
      dateTo: dateTo.value || undefined,
      status: status.value || undefined,
      generationStatus: generationStatus.value || undefined,
      deliveryStatus: deliveryStatus.value || undefined,
      page: page.value,
      pageSize: 20,
    })
    reports.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "安全日报列表暂不可用"
  } finally {
    loading.value = false
  }
}

async function loadOverview(): Promise<void> {
  try {
    overview.value = await getSecurityDailyOverview()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "安全日报概览暂不可用"
  }
}

async function refresh(): Promise<void> {
  await Promise.all([loadOverview(), loadReports()])
}

async function openReport(reportDate: string): Promise<void> {
  detailLoading.value = true
  drawerOpen.value = true
  previewOpen.value = false
  try {
    selected.value = await getSecurityDailyReport(reportDate)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "日报详情暂不可用")
    drawerOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

async function openPreview(): Promise<void> {
  if (!selected.value) return
  try {
    const preview = await previewSecurityDailyReport(selected.value.report_date)
    previewText.value = preview.available ? preview.text : (preview.message ?? "数据不可用")
    previewOpen.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "预览暂不可用")
  }
}

async function requestDelivery(action: "send" | "retry"): Promise<void> {
  if (!selected.value) return
  const operation = action === "retry" ? "重试投递" : "手动投递"
  try {
    await ElMessageBox.confirm(
      `确认${operation} ${selected.value.report_date} 的安全日报？邮件正文只来自已脱敏结构化报告。`,
      "确认安全日报投递",
      { confirmButtonText: "确认", cancelButtonText: "取消", type: "warning" },
    )
    if (action === "retry") {
      await retrySecurityDailyReport(selected.value.report_date)
    } else {
      await sendSecurityDailyReport(selected.value.report_date)
    }
    ElMessage.success("投递请求已受理，状态将在 mailer 回写后更新")
    await refresh()
    await openReport(selected.value.report_date)
  } catch (error) {
    if (error === "cancel" || error === "close") return
    ElMessage.error(error instanceof Error ? error.message : "投递请求失败")
  }
}

function canSend(report: SecurityDailyReport): boolean {
  return Boolean(overview.value?.enabled && report.generation_status === "ready" && report.payload)
}

function canRetry(report: SecurityDailyReport): boolean {
  return Boolean(overview.value?.enabled && report.generation_status === "ready" && report.payload && report.delivery_status === "failed")
}

function openRow(row: SecurityDailyReport): void {
  void openReport(row.report_date)
}

onMounted(() => void refresh())
</script>

<template>
  <main class="workspace security-daily-page">
    <header class="workspace-header">
      <div>
        <div class="breadcrumb"><span>运维</span><b>/</b><strong>安全日报</strong></div>
        <h1>服务器安全日报</h1>
        <p>固定 08:00（Asia/Shanghai）汇总前一上海自然日；页面只展示脱敏结构化证据。</p>
      </div>
      <el-button type="primary" plain :loading="loading" @click="refresh">刷新</el-button>
    </header>

    <el-alert v-if="errorMessage" :title="errorMessage" type="warning" show-icon :closable="false" />

    <section v-if="overview" class="security-daily-overview" aria-label="安全日报概览">
      <article class="security-daily-state-card">
        <span class="section-index">D030</span>
        <strong>{{ overview.enabled ? "日报已启用" : "日报未启用" }}</strong>
        <el-tag :type="overview.resend_configured ? 'success' : 'warning'" size="small">
          {{ overview.resend_configured ? "投递器已配置" : "投递器未配置" }}
        </el-tag>
        <p>{{ overview.period_description }} · 下次 {{ displayMoment(overview.next_scheduled_at) }}</p>
      </article>
      <dl class="security-daily-facts">
        <div><dt>调度</dt><dd>{{ overview.schedule_time }} · {{ overview.timezone }}</dd></div>
        <div><dt>收件人数</dt><dd>{{ overview.recipient_count }} 人（只展示数量）</dd></div>
        <div><dt>发件域名</dt><dd>{{ overview.sender_domain }} / {{ overview.sender_address }}</dd></div>
        <div><dt>最近投递</dt><dd>{{ overviewStatus ? deliveryLabel(overviewStatus) : "数据不可用" }}</dd></div>
        <div><dt>最近生成</dt><dd>{{ displayMoment(overview.last_generated_at) }}</dd></div>
        <div><dt>最近失败</dt><dd>{{ overview.latest_failure ?? "—" }}</dd></div>
        <div><dt>Beat 配置</dt><dd>{{ overview.beat_restart_required ? "修改后需重启 beat" : "无需重启" }}</dd></div>
      </dl>
    </section>

    <el-card class="workspace-card" shadow="never">
      <template #header>
        <div class="card-heading">
          <div><span class="section-index">01 / REPORTS</span><strong>日报记录</strong></div>
          <div class="security-daily-filters">
            <el-date-picker v-model="dateFrom" type="date" value-format="YYYY-MM-DD" clearable placeholder="起始日期" size="small" @change="page = 1; loadReports()" />
            <el-date-picker v-model="dateTo" type="date" value-format="YYYY-MM-DD" clearable placeholder="结束日期" size="small" @change="page = 1; loadReports()" />
            <el-select v-model="status" clearable placeholder="安全状态" size="small" @change="page = 1; loadReports()">
              <el-option label="正常" value="normal" /><el-option label="关注" value="attention" /><el-option label="高风险" value="high" />
            </el-select>
            <el-select v-model="generationStatus" clearable placeholder="生成状态" size="small" @change="page = 1; loadReports()">
              <el-option label="已生成" value="ready" /><el-option label="生成失败" value="failed" /><el-option label="数据不可用" value="unavailable" />
            </el-select>
            <el-select v-model="deliveryStatus" clearable placeholder="投递状态" size="small" @change="page = 1; loadReports()">
              <el-option label="未投递" value="not_sent" /><el-option label="等待 mailer" value="pending" /><el-option label="已投递" value="sent" /><el-option label="投递失败" value="failed" />
            </el-select>
          </div>
        </div>
      </template>
      <el-table v-loading="loading" :data="reports" row-key="id" empty-text="暂无安全日报记录" @row-click="openRow">
        <el-table-column prop="report_date" label="报告日期" width="130" />
        <el-table-column label="安全状态" width="110"><template #default="scope"><el-tag :type="tagType(scope.row.status)" size="small">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="生成" width="110"><template #default="scope"><el-tag :type="tagType(scope.row.generation_status)" size="small">{{ generationLabel(scope.row.generation_status) }}</el-tag></template></el-table-column>
        <el-table-column label="投递" width="125"><template #default="scope"><el-tag :type="tagType(scope.row.delivery_status)" size="small">{{ deliveryLabel(scope.row.delivery_status) }}</el-tag></template></el-table-column>
        <el-table-column prop="recipient_count" label="收件人数" width="95" />
        <el-table-column label="统计窗口" min-width="260"><template #default="scope">{{ displayMoment(scope.row.period_start) }} — {{ displayMoment(scope.row.period_end) }}</template></el-table-column>
        <el-table-column label="生成时间" width="180"><template #default="scope">{{ displayMoment(scope.row.generated_at) }}</template></el-table-column>
        <el-table-column label="投递时间" width="180"><template #default="scope">{{ displayMoment(scope.row.delivered_at) }}</template></el-table-column>
        <el-table-column prop="retry_count" label="重试" width="70" />
        <el-table-column label="最新错误" min-width="180"><template #default="scope">{{ scope.row.last_error ?? "—" }}</template></el-table-column>
        <el-table-column label="更新时间" width="180"><template #default="scope">{{ displayMoment(scope.row.updated_at) }}</template></el-table-column>
        <el-table-column label="操作" width="190" fixed="right"><template #default="scope"><el-button link type="primary" @click.stop="openReport(scope.row.report_date)">查看详情</el-button><el-button v-if="canRetry(scope.row)" link type="warning" @click.stop="openReport(scope.row.report_date)">处理失败</el-button></template></el-table-column>
      </el-table>
      <el-pagination v-if="total > 20" v-model:current-page="page" class="security-daily-pagination" background layout="prev, pager, next" :page-size="20" :total="total" @current-change="loadReports" />
    </el-card>

    <el-drawer v-model="drawerOpen" title="安全日报详情" size="min(760px, 92vw)">
      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <template v-else-if="selected">
        <div class="security-detail-heading">
          <div><span class="section-index">{{ selected.report_date }}</span><h2>{{ statusLabel(selected.status) }} · {{ generationLabel(selected.generation_status) }}</h2></div>
          <div class="security-detail-actions"><el-button plain @click="openPreview">安全预览</el-button><el-button v-if="canSend(selected)" type="primary" @click="requestDelivery('send')">手动投递</el-button><el-button v-if="canRetry(selected)" type="warning" @click="requestDelivery('retry')">重试投递</el-button></div>
        </div>
        <el-alert v-if="!selected.payload" title="数据不可用：当前没有可展示的脱敏结构化报告，不生成或投递邮件。" type="warning" show-icon :closable="false" />
        <template v-else>
          <section class="security-detail-summary"><span class="section-index">管理摘要</span><p>{{ selected.payload.summary }}</p><p v-if="selected.payload.pending_confirmation" class="security-pending">待确认：{{ selected.payload.pending_confirmation }}</p></section>
          <section><div class="section-heading"><span class="section-index">核心指标</span><strong>5 项指标</strong></div><div class="security-metric-grid"><article v-for="metric in selected.payload.metrics" :key="metric.label" :class="['security-metric', metric.tone]"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong><small>{{ metric.note }}</small></article></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">SSH 与主机安全</span></div><div class="security-evidence-list"><div v-for="row in selected.payload.ssh" :key="`ssh-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">Web / API</span></div><div class="security-evidence-list"><div v-for="row in selected.payload.web" :key="`web-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">管理审计</span></div><div class="security-evidence-list"><div v-for="row in selected.payload.audit" :key="`audit-${row.time}-${row.action}`"><strong>{{ row.time }} · {{ row.actor }}</strong><span>{{ row.source_ip }} · {{ row.action }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">运行状态</span></div><div class="security-evidence-list"><div v-for="row in selected.payload.runtime" :key="`runtime-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">建议处置</span></div><div class="security-action-list"><article v-for="action in selected.payload.actions" :key="action.title"><el-tag :type="tagType(action.priority === 'high' ? 'high' : action.priority === 'medium' ? 'attention' : 'normal')" size="small">{{ action.priority }}</el-tag><div><strong>{{ action.title }}</strong><p>{{ action.detail }}</p></div></article></div></section>
          <section class="security-detail-section"><div class="section-heading"><span class="section-index">证据范围</span></div><el-alert v-if="coverageGaps.length" title="存在日志覆盖缺口" type="warning" show-icon :closable="false"><template #default><div v-for="item in coverageGaps" :key="`gap-${item.source}`">{{ item.source }}：{{ item.note }}</div></template></el-alert><div class="security-evidence-list"><div v-for="item in selected.payload.coverage" :key="item.source"><strong>{{ item.source }}</strong><span>{{ item.window }} · {{ item.note }}</span><el-tag :type="tagType(item.tone)" size="small">{{ item.status }}</el-tag></div></div></section>
        </template>
        <section v-if="selected.timeline.length" class="security-detail-section"><div class="section-heading"><span class="section-index">投递时间线</span></div><ol class="security-timeline"><li v-for="event in selected.timeline" :key="`${event.type}-${event.at}`"><time>{{ displayMoment(event.at) }}</time><strong>{{ event.label }}</strong><span v-if="event.detail">{{ event.detail }}</span></li></ol></section>
      </template>
    </el-drawer>

    <el-dialog v-model="previewOpen" title="安全日报纯文本预览" width="720px"><pre class="security-preview-text">{{ previewText }}</pre></el-dialog>
  </main>
</template>
