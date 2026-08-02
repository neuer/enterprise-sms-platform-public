<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import {
  generateSecurityDailyReport,
  getSecurityDailyConfiguration,
  getSecurityDailyOverview,
  getSecurityDailyReport,
  listSecurityDailyReports,
  previewSecurityDailyReport,
  retrySecurityDailyReport,
  sendSecurityDailyReport,
  type DeliveryStatus,
  type SecurityDailyConfiguration,
  type GenerationStatus,
  type SecurityDailyConfigurationState,
  type SecurityDailyOverview,
  type SecurityDailyPayload,
  type SecurityDailyReport,
  type SecurityStatus,
  updateSecurityDailyConfiguration,
} from "../api/securityDaily"
import { ApiRequestError } from "../api/webMessages"

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
const configurationLabels: Record<SecurityDailyConfigurationState, string> = {
  disabled: "日报未启用",
  dispatcher_missing: "已启用，投递器未配置",
  recipients_empty: "已启用，收件人未配置",
  ready: "日报已启用",
}
const configurationTagLabels: Record<SecurityDailyConfigurationState, string> = {
  disabled: "未启用",
  dispatcher_missing: "待配置",
  recipients_empty: "待配置",
  ready: "配置完整",
}
const configurationMessages: Record<SecurityDailyConfigurationState, string> = {
  disabled: "当前未启用，不会创建下一次运行计划。",
  dispatcher_missing: "日报已启用，但独立投递器尚未配置，当前不会正常投递。",
  recipients_empty: "日报已启用，但没有收件人配置，当前不会投递。",
  ready: "当前配置完整，按固定时间生成并交由独立投递器发送。",
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
const delivering = ref(false)
const previewLoading = ref(false)
const overviewErrorMessage = ref("")
const reportsErrorMessage = ref("")
const selected = ref<SecurityDailyReport | null>(null)
const drawerOpen = ref(false)
const previewText = ref("")
const previewOpen = ref(false)
const configOpen = ref(false)
const configLoading = ref(false)
const configSaving = ref(false)
const generationLoading = ref(false)
const configErrorMessage = ref("")
const configEnabled = ref(false)
const configRecipients = ref("")
const configApiKey = ref("")
const clearConfigApiKey = ref(false)
const currentConfiguration = ref<SecurityDailyConfiguration | null>(null)

const selectedPayload = computed<SecurityDailyPayload | null>(() => selected.value?.payload ?? null)
const coverageGaps = computed(() =>
  selectedPayload.value?.coverage.filter(
    (item) => item.tone !== "good" || item.status !== "完整",
  ) ?? [],
)
const hasActiveFilters = computed(() =>
  Boolean(dateFrom.value || dateTo.value || status.value || generationStatus.value || deliveryStatus.value),
)
const overviewDeliveryStatusLabel = computed(() => {
  if (!overview.value) return "数据不可用"
  return overview.value.delivery_status ? deliveryLabel(overview.value.delivery_status) : "尚未生成"
})
const reportsEmptyText = computed(() => {
  if (reportsErrorMessage.value) return "安全日报记录暂不可用，请刷新重试"
  if (hasActiveFilters.value) return "没有符合筛选条件的安全日报"
  if (overview.value?.configuration_state === "ready") return "暂无已生成安全日报"
  return "暂无安全日报记录"
})
const reportsEmptyHint = computed(() => {
  if (loading.value || reportsErrorMessage.value || total.value > 0 || !overview.value || hasActiveFilters.value) return ""
  if (!overview.value.enabled) return "安全日报尚未启用，启用后才会按固定时间生成日报。"
  if (overview.value.configuration_state !== "ready") return "请先完成安全日报配置，系统才会按固定时间生成日报。"
  const nextSchedule = overview.value.next_scheduled_at
    ? displayMoment(overview.value.next_scheduled_at)
    : "下一次调度时间"
  return `暂无已生成安全日报；可点击“立即生成”读取上一上海自然日的脱敏证据（不会自动发送）。${nextSchedule}后仍会按计划自动生成；生成后可打开详情进行安全预览和手动投递。`
})

function statusLabel(value: string): string {
  return statusLabels[value as SecurityStatus] ?? "未知状态"
}

function generationLabel(value: string): string {
  return generationLabels[value as GenerationStatus] ?? "数据不可用"
}

function deliveryLabel(value: string): string {
  return deliveryLabels[value as DeliveryStatus] ?? "数据不可用"
}

function configurationLabel(value: string): string {
  return configurationLabels[value as SecurityDailyConfigurationState] ?? "配置状态不可用"
}

function configurationTagLabel(value: string): string {
  return configurationTagLabels[value as SecurityDailyConfigurationState] ?? "不可用"
}

function configurationMessage(value: string): string {
  return configurationMessages[value as SecurityDailyConfigurationState] ?? "配置状态不可用，请刷新重试。"
}

function configurationTagType(value: string): "success" | "warning" | "info" {
  if (value === "ready") return "success"
  if (value === "dispatcher_missing" || value === "recipients_empty") return "warning"
  return "info"
}

function apiErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiRequestError) {
    const retry = error.status >= 500 ? "，请刷新重试" : ""
    return `${error.message}（错误码 ${error.code}）${retry}`
  }
  if (error instanceof Error && "code" in error && typeof error.code === "string") {
    const status = "status" in error && typeof error.status === "number" ? error.status : 0
    const retry = status >= 500 ? "，请刷新重试" : ""
    return `${error.message}（错误码 ${error.code}）${retry}`
  }
  return error instanceof Error ? error.message : fallback
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
  if (Number.isNaN(parsed.valueOf())) return "数据不可用"
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(parsed)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`
}

function applyFilters(): void {
  if (dateFrom.value && dateTo.value && dateFrom.value > dateTo.value) {
    ElMessage.warning("起始日期不能晚于结束日期")
    return
  }
  page.value = 1
  void loadReports()
}

async function loadReports(): Promise<void> {
  loading.value = true
  reportsErrorMessage.value = ""
  reports.value = []
  total.value = 0
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
    reports.value = []
    total.value = 0
    reportsErrorMessage.value = apiErrorMessage(error, "安全日报列表暂不可用，请刷新重试")
  } finally {
    loading.value = false
  }
}

async function loadOverview(): Promise<void> {
  overviewErrorMessage.value = ""
  overview.value = null
  try {
    overview.value = await getSecurityDailyOverview()
  } catch (error) {
    overview.value = null
    overviewErrorMessage.value = apiErrorMessage(error, "安全日报概览暂不可用，请刷新重试")
  }
}

async function openConfiguration(): Promise<void> {
  configOpen.value = true
  configLoading.value = true
  configErrorMessage.value = ""
  configApiKey.value = ""
  clearConfigApiKey.value = false
  try {
    const configuration = await getSecurityDailyConfiguration()
    currentConfiguration.value = configuration
    configEnabled.value = configuration.enabled
    configRecipients.value = configuration.recipients.join("\n")
  } catch (error) {
    configErrorMessage.value = apiErrorMessage(error, "安全日报配置暂不可用，请刷新重试")
  } finally {
    configLoading.value = false
  }
}

function parseRecipients(): string[] {
  return configRecipients.value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)
}

const EMAIL_PATTERN = /^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}$/

function validateRecipients(recipients: string[]): string {
  if (recipients.length > 3) return "收件人最多 3 个"
  const invalid = recipients.find(
    (item) => item.length > 254 || !EMAIL_PATTERN.test(item) || item.split("@")[0].length > 64,
  )
  if (invalid) return `收件人地址无效：${invalid}`
  const seen = new Set<string>()
  const duplicate = recipients.find((item) => {
    const key = item.toLowerCase()
    if (seen.has(key)) return true
    seen.add(key)
    return false
  })
  return duplicate ? `收件人不能重复：${duplicate}` : ""
}

async function saveConfiguration(): Promise<void> {
  const recipients = parseRecipients()
  const validationError = validateRecipients(recipients)
  if (validationError) {
    configErrorMessage.value = validationError
    return
  }
  configSaving.value = true
  configErrorMessage.value = ""
  try {
    currentConfiguration.value = await updateSecurityDailyConfiguration({
      enabled: configEnabled.value,
      recipients,
      resend_api_key: clearConfigApiKey.value ? "" : (configApiKey.value.trim() || null),
    })
    configOpen.value = false
    ElMessage.success("安全日报配置已保存")
    await refresh()
  } catch (error) {
    configErrorMessage.value = apiErrorMessage(error, "安全日报配置保存失败，请检查输入")
  } finally {
    configSaving.value = false
  }
}

async function refresh(): Promise<void> {
  await Promise.all([loadOverview(), loadReports()])
}

async function generateReport(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "将读取上一上海自然日的脱敏证据并生成日报，不会自动发送邮件。",
      "立即生成安全日报",
      { confirmButtonText: "立即生成", cancelButtonText: "取消", type: "info" },
    )
    generationLoading.value = true
    const report = await generateSecurityDailyReport()
    await refresh()
    await openReport(report.report_date)
    if (report.generation_status === "ready") {
      ElMessage.success("安全日报已生成，可在详情中预览或手动投递")
    } else {
      ElMessage.warning(report.last_error ?? "证据源不可用，已记录数据不可用状态")
    }
  } catch (error) {
    if (error === "cancel" || error === "close") return
    ElMessage.error(apiErrorMessage(error, "安全日报生成失败，请刷新重试"))
  } finally {
    generationLoading.value = false
  }
}

async function openReport(reportDate: string): Promise<void> {
  detailLoading.value = true
  drawerOpen.value = true
  previewOpen.value = false
  try {
    selected.value = await getSecurityDailyReport(reportDate)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, "日报详情暂不可用，请刷新重试"))
    drawerOpen.value = false
  } finally {
    detailLoading.value = false
  }
}

async function openPreview(): Promise<void> {
  if (!selected.value || previewLoading.value) return
  previewLoading.value = true
  try {
    const preview = await previewSecurityDailyReport(selected.value.report_date)
    previewText.value = preview.available ? preview.text : (preview.message ?? "数据不可用")
    previewOpen.value = true
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, "预览暂不可用，请刷新重试"))
  } finally {
    previewLoading.value = false
  }
}

async function requestDelivery(action: "send" | "retry"): Promise<void> {
  if (!selected.value || delivering.value) return
  const operation = action === "retry" ? "重试投递" : "手动投递"
  try {
    await ElMessageBox.confirm(
      `确认${operation} ${selected.value.report_date} 的安全日报？邮件正文只来自已脱敏结构化报告。`,
      "确认安全日报投递",
      { confirmButtonText: "确认", cancelButtonText: "取消", type: "warning" },
    )
    delivering.value = true
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
    ElMessage.error(apiErrorMessage(error, "投递请求失败，请刷新重试"))
  } finally {
    delivering.value = false
  }
}

function canSend(report: SecurityDailyReport): boolean {
  return Boolean(
    overview.value?.configuration_state === "ready"
    && report.generation_status === "ready"
    && report.delivery_status === "not_sent",
  )
}

function canRetry(report: SecurityDailyReport): boolean {
  return Boolean(
    overview.value?.configuration_state === "ready"
    && report.generation_status === "ready"
    && report.delivery_status === "failed",
  )
}

function openRow(row: SecurityDailyReport): void {
  void openReport(row.report_date)
}

onMounted(() => void refresh())
</script>

<template>
  <main class="workspace security-daily-page">
    <section class="page-heading security-daily-heading">
      <div>
        <p class="eyebrow">SECURITY DAILY / 安全日报</p>
        <h1>服务器安全日报</h1>
        <p>固定 08:00（Asia/Shanghai）汇总前一上海自然日；页面只展示脱敏结构化证据。</p>
      </div>
      <div class="security-daily-heading-actions">
        <el-button plain :loading="configLoading" @click="openConfiguration">配置邮件</el-button>
        <el-button plain :disabled="!overview?.enabled" :loading="generationLoading" @click="generateReport">立即生成</el-button>
        <el-button type="primary" plain :loading="loading" @click="refresh">刷新</el-button>
      </div>
    </section>

    <el-alert v-if="overviewErrorMessage" :title="overviewErrorMessage" type="error" show-icon :closable="false" />
    <el-alert v-if="reportsErrorMessage" :title="reportsErrorMessage" type="error" show-icon :closable="false" />

    <section v-if="overview" class="security-daily-overview" aria-label="安全日报概览">
      <article class="security-daily-state-card">
        <span class="section-index">安全日报运行状态</span>
        <strong>{{ configurationLabel(overview.configuration_state) }}</strong>
        <el-tag :type="configurationTagType(overview.configuration_state)" size="small">
          {{ configurationTagLabel(overview.configuration_state) }}
        </el-tag>
        <p>
          {{ overview.period_description }} · {{ configurationMessage(overview.configuration_state) }}
          <template v-if="overview.next_scheduled_at">下次 {{ displayMoment(overview.next_scheduled_at) }}</template>
        </p>
      </article>
      <dl class="security-daily-facts">
        <div><dt>调度</dt><dd>{{ overview.schedule_time }} · {{ overview.timezone }}</dd></div>
        <div><dt>收件人数</dt><dd>{{ overview.recipient_count }} 人（只展示数量）</dd></div>
        <div><dt>发件域名</dt><dd>{{ overview.sender_domain }} / {{ overview.sender_address }}</dd></div>
        <div><dt>最近日报状态</dt><dd>{{ overviewDeliveryStatusLabel }}</dd></div>
        <div><dt>最近成功生成</dt><dd>{{ displayMoment(overview.last_generated_at) }}</dd></div>
        <div><dt>最近成功投递</dt><dd>{{ displayMoment(overview.last_delivered_at) }}</dd></div>
        <div><dt>最近失败</dt><dd>{{ overview.latest_failure ?? "—" }}</dd></div>
        <div><dt>Beat 配置</dt><dd>{{ overview.beat_restart_required ? "修改后需重启 beat" : "无需重启" }}</dd></div>
      </dl>
    </section>

    <el-card shadow="never">
      <template #header>
        <div class="card-heading">
          <div><span class="section-index">01 / REPORTS</span><strong>日报记录</strong></div>
          <div class="security-daily-filters">
            <el-date-picker v-model="dateFrom" type="date" value-format="YYYY-MM-DD" clearable placeholder="起始日期" size="small" @change="applyFilters" />
            <el-date-picker v-model="dateTo" type="date" value-format="YYYY-MM-DD" clearable placeholder="结束日期" size="small" @change="applyFilters" />
            <el-select v-model="status" clearable placeholder="安全状态" size="small" @change="applyFilters">
              <el-option label="正常" value="normal" /><el-option label="关注" value="attention" /><el-option label="高风险" value="high" />
            </el-select>
            <el-select v-model="generationStatus" clearable placeholder="生成状态" size="small" @change="applyFilters">
              <el-option label="生成中" value="pending" /><el-option label="已生成" value="ready" /><el-option label="生成失败" value="failed" /><el-option label="数据不可用" value="unavailable" />
            </el-select>
            <el-select v-model="deliveryStatus" clearable placeholder="投递状态" size="small" @change="applyFilters">
              <el-option label="未投递" value="not_sent" /><el-option label="等待 mailer" value="pending" /><el-option label="投递中" value="sending" /><el-option label="已投递" value="sent" /><el-option label="投递失败" value="failed" />
            </el-select>
          </div>
        </div>
      </template>
      <el-alert v-if="reportsEmptyHint" :title="reportsEmptyHint" type="info" show-icon :closable="false" />
      <el-table v-loading="loading" :data="reports" row-key="id" :empty-text="reportsEmptyText" @row-click="openRow">
        <el-table-column prop="report_date" label="报告日期" width="120" />
        <el-table-column label="安全状态" width="105"><template #default="scope"><el-tag :type="tagType(scope.row.status)" size="small">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="生成" width="110"><template #default="scope"><el-tag :type="tagType(scope.row.generation_status)" size="small">{{ generationLabel(scope.row.generation_status) }}</el-tag></template></el-table-column>
        <el-table-column label="投递" width="125"><template #default="scope"><el-tag :type="tagType(scope.row.delivery_status)" size="small">{{ deliveryLabel(scope.row.delivery_status) }}</el-tag></template></el-table-column>
        <el-table-column prop="recipient_count" label="收件人数" width="90" />
        <el-table-column label="生成时间" width="170"><template #default="scope"><time>{{ displayMoment(scope.row.generated_at) }}</time></template></el-table-column>
        <el-table-column label="投递时间" width="170"><template #default="scope"><time>{{ displayMoment(scope.row.delivered_at) }}</time></template></el-table-column>
        <el-table-column prop="retry_count" label="重试" width="65" />
        <el-table-column label="最新错误" min-width="180"><template #default="scope">{{ scope.row.last_error ?? "—" }}</template></el-table-column>
        <el-table-column label="操作" width="170" fixed="right"><template #default="scope"><el-button link type="primary" @click.stop="openReport(scope.row.report_date)">查看详情</el-button><el-button v-if="canRetry(scope.row)" link type="warning" @click.stop="openReport(scope.row.report_date)">处理失败</el-button></template></el-table-column>
      </el-table>
      <el-pagination v-if="total > 20" v-model:current-page="page" class="security-daily-pagination" background layout="prev, pager, next" :page-size="20" :total="total" @current-change="loadReports" />
    </el-card>

    <el-drawer v-model="drawerOpen" title="安全日报详情" size="min(760px, 92vw)">
      <el-skeleton v-if="detailLoading" :rows="8" animated />
      <template v-else-if="selected">
        <div class="security-detail-heading">
          <div>
            <span class="section-index">{{ selected.report_date }}</span>
            <h2>{{ statusLabel(selected.status) }} · {{ generationLabel(selected.generation_status) }}</h2>
            <p class="security-detail-period">统计窗口 <time>{{ displayMoment(selected.period_start) }}</time> — <time>{{ displayMoment(selected.period_end) }}</time></p>
          </div>
          <div class="security-detail-actions"><el-button plain :loading="previewLoading" @click="openPreview">安全预览</el-button><el-button v-if="canSend(selected)" type="primary" :loading="delivering" @click="requestDelivery('send')">手动投递</el-button><el-button v-if="canRetry(selected)" type="warning" :loading="delivering" @click="requestDelivery('retry')">重试投递</el-button></div>
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
        <section v-if="selected.timeline.length" class="security-detail-section"><div class="section-heading"><span class="section-index">{{ selected.generation_status === "unavailable" || selected.generation_status === "failed" ? "状态时间线" : "投递时间线" }}</span></div><ol class="security-timeline"><li v-for="event in selected.timeline" :key="`${event.type}-${event.at}`"><time>{{ displayMoment(event.at) }}</time><strong>{{ event.label }}</strong><span v-if="event.detail">{{ event.detail }}</span></li></ol></section>
      </template>
    </el-drawer>

    <el-dialog v-model="previewOpen" title="安全日报纯文本预览" width="720px"><pre class="security-preview-text">{{ previewText }}</pre></el-dialog>

    <el-dialog v-model="configOpen" title="安全日报邮件配置" width="560px" destroy-on-close>
      <el-skeleton v-if="configLoading" :rows="5" animated />
      <el-form v-else label-position="top" @submit.prevent="saveConfiguration">
        <el-form-item label="启用安全日报">
          <el-switch v-model="configEnabled" active-text="启用" inactive-text="停用" />
        </el-form-item>
        <el-form-item label="Resend API Key">
          <el-input v-model="configApiKey" type="password" show-password autocomplete="off" :disabled="clearConfigApiKey" placeholder="留空保持当前 Key" />
          <div class="form-tip">当前状态：{{ currentConfiguration?.resend_api_key_configured ? "已配置" : "未配置" }}；Key 不会回显。</div>
          <el-checkbox v-if="currentConfiguration?.resend_api_key_configured" v-model="clearConfigApiKey">清空当前 Key</el-checkbox>
        </el-form-item>
        <el-form-item label="收件人（每行一个，也可用逗号分隔，最多 3 个）">
          <el-input v-model="configRecipients" type="textarea" :rows="4" placeholder="security@example.com" />
        </el-form-item>
        <el-alert v-if="configErrorMessage" :title="configErrorMessage" type="error" show-icon :closable="false" />
      </el-form>
      <template #footer>
        <el-button @click="configOpen = false">取消</el-button>
        <el-button type="primary" :loading="configSaving" @click="saveConfiguration">保存</el-button>
      </template>
    </el-dialog>
  </main>
</template>
