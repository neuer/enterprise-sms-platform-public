<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, nextTick, onMounted, reactive, ref } from "vue"

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
  type SecurityActionItem,
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
import EmptyState from "../components/EmptyState.vue"

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
  unknown: "投递结果未知",
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
const priorityLabels: Record<SecurityActionItem["priority"], string> = {
  high: "高",
  medium: "中",
  low: "低",
}

const statusSegOptions = [
  { label: "全部", value: "" as SecurityStatus | "", key: "all" },
  { label: "正常", value: "normal" as SecurityStatus, key: "normal" },
  { label: "关注", value: "attention" as SecurityStatus, key: "attention" },
  { label: "高风险", value: "high" as SecurityStatus, key: "high" },
]
const generationSegOptions = [
  { label: "全部", value: "" as GenerationStatus | "", key: "all" },
  { label: "生成中", value: "pending" as GenerationStatus, key: "pending" },
  { label: "已生成", value: "ready" as GenerationStatus, key: "ready" },
  { label: "生成失败", value: "failed" as GenerationStatus, key: "failed" },
  { label: "数据不可用", value: "unavailable" as GenerationStatus, key: "unavailable" },
]
const deliverySegOptions = [
  { label: "全部", value: "" as DeliveryStatus | "", key: "all" },
  { label: "未投递", value: "not_sent" as DeliveryStatus, key: "not-sent" },
  { label: "等待 mailer", value: "pending" as DeliveryStatus, key: "pending" },
  { label: "投递中", value: "sending" as DeliveryStatus, key: "sending" },
  { label: "已投递", value: "sent" as DeliveryStatus, key: "sent" },
  { label: "投递失败", value: "failed" as DeliveryStatus, key: "failed" },
  { label: "结果未知", value: "unknown" as DeliveryStatus, key: "unknown" },
]

const overview = ref<SecurityDailyOverview | null>(null)
const reports = ref<SecurityDailyReport[]>([])
const total = ref(0)
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
const filters = reactive({
  dateFrom: "",
  dateTo: "",
  status: "" as SecurityStatus | "",
  generationStatus: "" as GenerationStatus | "",
  deliveryStatus: "" as DeliveryStatus | "",
  page: 1,
  pageSize: 20,
})

const selectedPayload = computed<SecurityDailyPayload | null>(() => selected.value?.payload ?? null)
const coverageGaps = computed(() =>
  selectedPayload.value?.coverage.filter(
    (item) => item.tone !== "good" || item.status !== "完整",
  ) ?? [],
)
const filtering = computed(() =>
  Boolean(filters.dateFrom || filters.dateTo || filters.status || filters.generationStatus || filters.deliveryStatus),
)
const overviewDeliveryStatusLabel = computed(() => {
  if (!overview.value) return "数据不可用"
  return overview.value.delivery_status ? deliveryLabel(overview.value.delivery_status) : "尚未生成"
})
const reportsEmptyTitle = computed(() => {
  if (!overview.value) return "暂无安全日报记录"
  if (!overview.value.enabled) return "安全日报尚未启用"
  if (overview.value.configuration_state !== "ready") return "安全日报配置不完整"
  return "暂无已生成安全日报"
})
const reportsEmptyDescription = computed(() => {
  if (!overview.value) return "概览暂不可用；刷新重试后，已生成的日报会出现在这里。"
  if (!overview.value.enabled) return "启用后才会按固定时间生成日报；可在「配置邮件」中完成启用与收件人配置。"
  if (overview.value.configuration_state !== "ready") return "请先完成安全日报配置，系统才会按固定时间生成日报。"
  const nextSchedule = overview.value.next_scheduled_at
    ? displayMoment(overview.value.next_scheduled_at)
    : "下一次调度时间"
  return `可点击「立即生成」新增一条前一自然日（北京时间）的记录并立即投递。${nextSchedule} 后自动任务也会按计划生成并发送一封；生成后可打开详情进行安全预览和手动投递。`
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
  if (value === "normal" || value === "ready" || value === "sent" || value === "good") return "success"
  if (value === "attention" || value === "pending" || value === "sending" || value === "unknown" || value === "warn") return "warning"
  if (value === "high" || value === "failed" || value === "danger") return "danger"
  return "info"
}

function priorityType(priority: SecurityActionItem["priority"]): "danger" | "warning" | "info" {
  if (priority === "high") return "danger"
  if (priority === "medium") return "warning"
  return "info"
}

function displayPeriod(start: string, end: string): string {
  const startText = displayMoment(start)
  const endText = displayMoment(end)
  const fullLength = "YYYY-MM-DD HH:mm:ss".length
  const sameDay = startText.length === fullLength
    && endText.length === fullLength
    && startText.slice(0, 10) === endText.slice(0, 10)
  return sameDay ? `${startText} — ${endText.slice(11)}` : `${startText} — ${endText}`
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

let loadToken = 0

async function loadReports(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  reportsErrorMessage.value = ""
  reports.value = []
  total.value = 0
  try {
    const result = await listSecurityDailyReports({
      dateFrom: filters.dateFrom || undefined,
      dateTo: filters.dateTo || undefined,
      status: filters.status || undefined,
      generationStatus: filters.generationStatus || undefined,
      deliveryStatus: filters.deliveryStatus || undefined,
      page: filters.page,
      pageSize: filters.pageSize,
    })
    if (token !== loadToken) return
    reports.value = result.items
    total.value = result.total
  } catch (error) {
    if (token !== loadToken) return
    reports.value = []
    total.value = 0
    reportsErrorMessage.value = apiErrorMessage(error, "安全日报列表暂不可用，请刷新重试")
  } finally {
    if (token === loadToken) loading.value = false
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

/** 报告日期先校验起止先后再重查；seg 点选与「查询」按钮共用同一入口。 */
function search(): void {
  if (filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo) {
    ElMessage.warning("起始日期不能晚于结束日期")
    return
  }
  filters.page = 1
  void loadReports()
}

function resetFilters(): void {
  filters.dateFrom = ""
  filters.dateTo = ""
  filters.status = ""
  filters.generationStatus = ""
  filters.deliveryStatus = ""
  filters.page = 1
  void loadReports()
}

/** 状态 seg 点选即重查，与回调任务、用户与角色页同一语言。 */
function setStatus(value: SecurityStatus | ""): void {
  if (value === filters.status) return
  filters.status = value
  search()
}

function setGenerationStatus(value: GenerationStatus | ""): void {
  if (value === filters.generationStatus) return
  filters.generationStatus = value
  search()
}

function setDeliveryStatus(value: DeliveryStatus | ""): void {
  if (value === filters.deliveryStatus) return
  filters.deliveryStatus = value
  search()
}

function clearConfigurationSecrets(): void {
  configApiKey.value = ""
  clearConfigApiKey.value = false
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
    ElMessage.success("安全日报配置已保存 · 本次操作已记入审计")
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
      h("div", { class: "security-daily-confirm-dialog" }, [
        h(
          "p",
          "将汇总前一自然日（北京时间）的脱敏结构化证据并新增一条日报记录，不覆盖历史记录；生成完成后立即提交邮件投递，该日已有处理中的投递请求时将被拒绝。",
        ),
        h(
          "p",
          { class: "security-daily-confirm-audit" },
          "立即生成行为与操作人将写入审计日志。",
        ),
      ]),
      "确认立即生成",
      {
        confirmButtonText: "立即生成并投递",
        cancelButtonText: "取消",
        type: "warning",
        customClass: "security-daily-confirm-box",
      },
    )
    generationLoading.value = true
    const report = await generateSecurityDailyReport()
    await refresh()
    await openReport(report.id)
    if (report.generation_status === "ready") {
      ElMessage.success("安全日报已重新生成并提交邮件投递 · 本次操作已记入审计")
    } else {
      ElMessage.warning(`${report.last_error ?? "证据源不可用，已新增记录并发送问题通报"} · 本次操作已记入审计`)
    }
  } catch (error) {
    if (error === "cancel" || error === "close") return
    ElMessage.error(apiErrorMessage(error, "安全日报生成失败，请刷新重试"))
  } finally {
    generationLoading.value = false
  }
}

async function openReport(reportId: number): Promise<void> {
  detailLoading.value = true
  drawerOpen.value = true
  previewOpen.value = false
  // el-drawer 关闭后保留 DOM 与滚动位置，重新打开时先回到顶部，避免详情从中间开始显示
  void nextTick(() => {
    document.querySelector<HTMLElement>(".el-drawer__body")?.scrollTo({ top: 0 })
  })
  try {
    selected.value = await getSecurityDailyReport(reportId)
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
    const preview = await previewSecurityDailyReport(selected.value.id)
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
      h("div", { class: "security-daily-confirm-dialog" }, [
        h(
          "p",
          `确认${operation} ${selected.value.report_date} 的安全日报？邮件正文只来自已脱敏结构化报告，投递由独立 mailer 执行并回写状态，同日重复投递有幂等保护。`,
        ),
        h(
          "p",
          { class: "security-daily-confirm-audit" },
          `${operation}行为、操作人与日报 id 将写入审计日志。`,
        ),
      ]),
      `确认${operation}`,
      {
        confirmButtonText: `确认${operation}`,
        cancelButtonText: "取消",
        type: "warning",
        customClass: "security-daily-confirm-box",
      },
    )
    delivering.value = true
    if (action === "retry") {
      await retrySecurityDailyReport(selected.value.id)
    } else {
      await sendSecurityDailyReport(selected.value.id)
    }
    ElMessage.success("投递请求已受理，状态将在 mailer 回写后更新 · 本次操作已记入审计")
    await refresh()
    await openReport(selected.value.id)
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
  void openReport(row.id)
}

onMounted(() => void refresh())
</script>

<template>
  <section class="page-heading security-daily-heading">
    <div>
      <p class="eyebrow">SECURITY DAILY / 安全日报</p>
      <h1>服务器安全日报</h1>
      <p>固定 08:00（北京时间）汇总前一自然日；页面只展示脱敏结构化证据，手机号与密钥永不进入界面，Resend Key 保存后不回显。写操作全部写入审计。</p>
    </div>
    <div class="security-daily-heading-actions">
      <el-button plain :loading="configLoading" @click="openConfiguration">配置邮件</el-button>
      <el-button plain :disabled="!overview?.enabled" :loading="generationLoading" @click="generateReport">立即生成</el-button>
      <el-button type="primary" plain :loading="loading" @click="refresh">刷新</el-button>
    </div>
  </section>

  <el-alert v-if="overviewErrorMessage" class="security-daily-alert" :title="overviewErrorMessage" type="error" show-icon :closable="false" />

  <section v-if="overview" class="security-daily-overview" aria-label="安全日报概览">
    <div class="security-daily-state">
      <span class="security-section-label">安全日报运行状态</span>
      <strong>{{ configurationLabel(overview.configuration_state) }}</strong>
      <el-tag :type="configurationTagType(overview.configuration_state)" size="small">
        {{ configurationTagLabel(overview.configuration_state) }}
      </el-tag>
      <p>{{ overview.period_description }} · {{ configurationMessage(overview.configuration_state) }}</p>
      <em v-if="overview.next_scheduled_at">下次 {{ displayMoment(overview.next_scheduled_at) }}</em>
    </div>
    <dl class="security-daily-facts">
      <div><dt>调度</dt><dd>{{ overview.schedule_time }} · {{ overview.timezone }}</dd></div>
      <div><dt>收件人数</dt><dd>{{ overview.recipient_count }} 人（只展示数量）</dd></div>
      <div><dt>发件域名</dt><dd>{{ overview.sender_domain }} / {{ overview.sender_address }}</dd></div>
      <div><dt>最近日报状态</dt><dd>{{ overviewDeliveryStatusLabel }}</dd></div>
      <div><dt>最近成功生成</dt><dd>{{ displayMoment(overview.last_generated_at) }}</dd></div>
      <div><dt>最近成功投递</dt><dd>{{ displayMoment(overview.last_delivered_at) }}</dd></div>
      <div><dt>最近失败</dt><dd>{{ overview.latest_failure ?? "—" }}</dd></div>
    </dl>
  </section>

  <div>
    <form class="security-daily-filter-bar" @submit.prevent="search">
      <div class="security-daily-fld">
        <span>报告日期</span>
        <div class="security-daily-dates">
          <el-date-picker
            v-model="filters.dateFrom"
            class="security-daily-date"
            type="date"
            value-format="YYYY-MM-DD"
            popper-class="qingluan-date-popper"
            clearable
            placeholder="起始日期"
            data-testid="security-daily-date-from"
          />
          <el-date-picker
            v-model="filters.dateTo"
            class="security-daily-date"
            type="date"
            value-format="YYYY-MM-DD"
            popper-class="qingluan-date-popper"
            clearable
            placeholder="结束日期"
            data-testid="security-daily-date-to"
          />
        </div>
      </div>
      <div class="security-daily-fld">
        <span>安全状态</span>
        <div class="security-daily-seg" role="group" aria-label="安全状态筛选" data-testid="security-daily-status-seg">
          <button
            v-for="option in statusSegOptions"
            :key="option.key"
            type="button"
            :class="{ on: filters.status === option.value }"
            :data-testid="`security-daily-status-${option.key}`"
            @click="setStatus(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <div class="security-daily-fld">
        <span>生成状态</span>
        <div class="security-daily-seg" role="group" aria-label="生成状态筛选" data-testid="security-daily-generation-seg">
          <button
            v-for="option in generationSegOptions"
            :key="option.key"
            type="button"
            :class="{ on: filters.generationStatus === option.value }"
            :data-testid="`security-daily-generation-${option.key}`"
            @click="setGenerationStatus(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <div class="security-daily-fld">
        <span>投递状态</span>
        <div class="security-daily-seg" role="group" aria-label="投递状态筛选" data-testid="security-daily-delivery-seg">
          <button
            v-for="option in deliverySegOptions"
            :key="option.key"
            type="button"
            :class="{ on: filters.deliveryStatus === option.value }"
            :data-testid="`security-daily-delivery-${option.key}`"
            @click="setDeliveryStatus(option.value)"
          >{{ option.label }}</button>
        </div>
      </div>
      <div class="security-daily-filter-go">
        <el-button data-testid="security-daily-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
        <el-button data-testid="security-daily-reset" @click="resetFilters">重置</el-button>
      </div>
    </form>
    <p class="security-daily-privacy">安全 / 生成 / 投递状态点选即重查，报告日期经「查询」生效并先校验起止先后；筛选与分页均在服务端执行，不走「接口全量返回 · 前端过滤」。页面只展示脱敏结构化证据，手机号与密钥不进入本页。</p>
  </div>

  <aside class="security-daily-rules" aria-label="脱敏边界、配置例外与投递语义">
    <div><span>脱敏边界</span><p>页面只展示脱敏结构化证据，手机号与密钥永不进入界面；详情与预览同样只是只读投影。</p></div>
    <div><span>配置例外</span><p>Resend Key 明文仅存专用配置并同步独立 mailer，审计只记 configured 状态与收件人数量；Key 保存后不回显。</p></div>
    <div><span>投递语义</span><p>投递由独立 mailer 执行并回写状态，页面查询时惰性同步；投递失败可重试，同日重复投递有幂等保护。</p></div>
  </aside>

  <el-alert v-if="reportsErrorMessage" class="security-daily-alert" :title="reportsErrorMessage" type="error" show-icon :closable="false">
    <template #default><el-button link type="primary" @click="loadReports">重新加载</el-button></template>
  </el-alert>

  <section class="security-daily-results">
    <template v-if="reports.length || loading">
      <el-table v-loading="loading" :data="reports" row-key="id" class="security-daily-table" @row-click="openRow">
        <el-table-column prop="id" label="记录" width="75" />
        <el-table-column prop="report_date" label="报告日期" width="120" />
        <el-table-column label="安全状态" width="105"><template #default="scope"><el-tag :type="tagType(scope.row.status)" size="small">{{ statusLabel(scope.row.status) }}</el-tag></template></el-table-column>
        <el-table-column label="生成方式" width="90"><template #default="scope"><el-tag :type="scope.row.generation_source === 'manual' ? 'warning' : 'info'" size="small">{{ scope.row.generation_source === 'manual' ? '手动' : '自动' }}</el-tag></template></el-table-column>
        <el-table-column label="生成" width="110"><template #default="scope"><el-tag :type="tagType(scope.row.generation_status)" size="small">{{ generationLabel(scope.row.generation_status) }}</el-tag></template></el-table-column>
        <el-table-column label="投递" width="125"><template #default="scope"><el-tag :type="tagType(scope.row.delivery_status)" size="small">{{ deliveryLabel(scope.row.delivery_status) }}</el-tag></template></el-table-column>
        <el-table-column prop="recipient_count" label="收件人数" width="90" />
        <el-table-column label="生成时间" width="170"><template #default="scope"><time>{{ displayMoment(scope.row.generated_at) }}</time></template></el-table-column>
        <el-table-column label="投递时间" width="170"><template #default="scope"><time>{{ displayMoment(scope.row.delivered_at) }}</time></template></el-table-column>
        <el-table-column prop="retry_count" label="重试" width="65" />
        <el-table-column label="最新错误" min-width="180"><template #default="scope">{{ scope.row.last_error ?? "—" }}</template></el-table-column>
        <el-table-column label="操作" width="170" fixed="right"><template #default="scope"><el-button link type="primary" @click.stop="openReport(scope.row.id)">查看详情</el-button><el-button v-if="canRetry(scope.row)" link type="warning" @click.stop="openReport(scope.row.id)">处理失败</el-button></template></el-table-column>
      </el-table>
    </template>
    <div v-else-if="reportsErrorMessage" class="security-daily-empty-action">
      <EmptyState title="安全日报记录暂不可用" description="请刷新重试；若持续失败，请检查独立投递控制面状态。" />
    </div>
    <div v-else-if="filtering" class="security-daily-empty-action">
      <EmptyState title="没有符合筛选条件的安全日报" description="调整报告日期或状态筛选后重新查询。" />
      <el-button data-testid="security-daily-clear-filters" @click="resetFilters">清除筛选</el-button>
    </div>
    <div v-else class="security-daily-empty-action">
      <EmptyState :title="reportsEmptyTitle" :description="reportsEmptyDescription" />
    </div>

    <footer class="security-daily-pagination">
      <span>共 {{ total }} 条 · 每页 20</span>
      <el-pagination v-model:current-page="filters.page" :page-size="filters.pageSize" :total="total" layout="prev, pager, next" @current-change="loadReports" />
    </footer>
  </section>

  <el-drawer v-model="drawerOpen" title="安全日报详情" size="min(760px, 92vw)">
    <el-skeleton v-if="detailLoading" :rows="8" animated />
    <template v-else-if="selected">
      <div class="security-detail-heading">
        <div>
          <span class="security-section-label">{{ selected.report_date }}</span>
          <h2>{{ statusLabel(selected.status) }}</h2>
          <div class="security-detail-tags">
            <el-tag :type="selected.generation_source === 'manual' ? 'warning' : 'info'" size="small">{{ selected.generation_source === 'manual' ? '手动生成' : '自动生成' }}</el-tag>
            <el-tag :type="tagType(selected.generation_status)" size="small">{{ generationLabel(selected.generation_status) }}</el-tag>
            <el-tag :type="tagType(selected.delivery_status)" size="small">{{ deliveryLabel(selected.delivery_status) }}</el-tag>
          </div>
          <p class="security-detail-period">统计窗口 <time>{{ displayPeriod(selected.period_start, selected.period_end) }}</time></p>
        </div>
        <div class="security-detail-actions"><el-button plain :loading="previewLoading" @click="openPreview">安全预览</el-button><el-button v-if="canSend(selected)" type="primary" :loading="delivering" @click="requestDelivery('send')">手动投递</el-button><el-button v-if="canRetry(selected)" type="warning" :loading="delivering" @click="requestDelivery('retry')">重试投递</el-button></div>
      </div>
      <el-alert v-if="!selected.payload" title="数据不可用：当前没有可展示的脱敏结构化报告，不生成或投递邮件。" type="warning" show-icon :closable="false" />
      <section class="security-detail-section">
        <div class="section-heading"><span class="security-section-label">状态信息</span></div>
        <dl class="security-delivery-facts">
          <div><dt>生成时间</dt><dd><time>{{ displayMoment(selected.generated_at) }}</time></dd></div>
          <div><dt>投递时间</dt><dd><time>{{ displayMoment(selected.delivered_at) }}</time></dd></div>
          <div><dt>收件人数</dt><dd>{{ selected.recipient_count }} 人</dd></div>
          <div><dt>重试次数</dt><dd>{{ selected.retry_count }} 次</dd></div>
          <div v-if="selected.last_error" class="security-error-cell">
            <dt>最新错误</dt>
            <dd class="security-error">{{ selected.last_error }}<time v-if="selected.last_error_at"> · {{ displayMoment(selected.last_error_at) }}</time></dd>
          </div>
        </dl>
      </section>
      <template v-if="selected.payload">
        <section class="security-detail-summary"><span class="security-section-label">管理摘要</span><p>{{ selected.payload.summary }}</p><p v-if="selected.payload.pending_confirmation" class="security-pending">待确认：{{ selected.payload.pending_confirmation }}</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">核心指标</span><strong>{{ selected.payload.metrics.length }} 项指标</strong></div><div v-if="selected.payload.metrics.length" class="security-metric-grid"><article v-for="metric in selected.payload.metrics" :key="metric.label" :class="['security-metric', metric.tone]"><span>{{ metric.label }}</span><strong>{{ metric.value }}</strong><small>{{ metric.note }}</small></article></div><p v-else class="security-section-empty">该小节无记录</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">SSH 与主机安全</span></div><div v-if="selected.payload.ssh.length" class="security-evidence-list"><div v-for="row in selected.payload.ssh" :key="`ssh-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div><p v-else class="security-section-empty">该证据面无记录</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">Web / API</span></div><div v-if="selected.payload.web.length" class="security-evidence-list"><div v-for="row in selected.payload.web" :key="`web-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div><p v-else class="security-section-empty">该证据面无记录</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">管理审计</span></div><div v-if="selected.payload.audit.length" class="security-evidence-list"><div v-for="row in selected.payload.audit" :key="`audit-${row.time}-${row.action}`" class="security-audit-row"><strong>{{ row.time }} · {{ row.actor }}</strong><span>{{ row.source_ip }} · {{ row.action }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div><p v-else class="security-section-empty">该证据面无记录</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">运行状态</span></div><div v-if="selected.payload.runtime.length" class="security-evidence-list"><div v-for="row in selected.payload.runtime" :key="`runtime-${row.label}`"><strong>{{ row.label }}</strong><span>{{ row.value }}</span><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div></div><p v-else class="security-section-empty">该证据面无记录</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">建议处置</span></div><div v-if="selected.payload.actions.length" class="security-action-list"><article v-for="action in selected.payload.actions" :key="action.title"><el-tag :type="priorityType(action.priority)" size="small">{{ priorityLabels[action.priority] }}</el-tag><div><strong>{{ action.title }}</strong><p>{{ action.detail }}</p></div></article></div><p v-else class="security-section-empty">无需处置事项</p></section>
        <section class="security-detail-section"><div class="section-heading"><span class="security-section-label">证据范围</span></div><el-alert v-if="coverageGaps.length" title="存在日志覆盖缺口" type="warning" show-icon :closable="false"><template #default><div v-for="item in coverageGaps" :key="`gap-${item.source}`">{{ item.source }}：{{ item.note }}</div></template></el-alert><div v-if="selected.payload.coverage.length" class="security-evidence-list"><div v-for="item in selected.payload.coverage" :key="item.source"><strong>{{ item.source }}</strong><span>{{ item.window }} · {{ item.note }}</span><el-tag :type="tagType(item.tone)" size="small">{{ item.status }}</el-tag></div></div><p v-else class="security-section-empty">该证据面无记录</p></section>
      </template>
      <section v-if="selected.timeline.length" class="security-detail-section"><div class="section-heading"><span class="security-section-label">{{ selected.generation_status === "unavailable" || selected.generation_status === "failed" ? "状态时间线" : "投递时间线" }}</span></div><ol class="security-timeline"><li v-for="event in selected.timeline" :key="`${event.type}-${event.at}`"><time>{{ displayMoment(event.at) }}</time><strong>{{ event.label }}</strong><span v-if="event.detail">{{ event.detail }}</span></li></ol></section>
    </template>
  </el-drawer>

  <el-dialog v-model="previewOpen" title="安全日报纯文本预览" width="720px"><pre class="security-preview-text">{{ previewText }}</pre></el-dialog>

  <el-dialog v-model="configOpen" title="安全日报邮件配置" width="560px" destroy-on-close @closed="clearConfigurationSecrets">
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
</template>
