<script setup lang="ts">
import SecurityDailyConfigDialog from "../components/SecurityDailyConfigDialog.vue"
import LoadErrorAlert from "../components/LoadErrorAlert.vue"
import { apiErrorMessage } from "../lib/securityDaily"
import { useLatestRead } from "../composables/useLatestRead"
import { usePagedList } from "../composables/usePagedList"
import ListPagination from "../components/ListPagination.vue"
import FilterSeg from "../components/FilterSeg.vue"
import MobileFilterToggle from "../components/MobileFilterToggle.vue"
import { ElMessage } from "element-plus"
import { computed, nextTick, onMounted, reactive, ref, toRef, watch } from "vue"

import {
  generateSecurityDailyReport,
  getSecurityDailyOverview,
  getSecurityDailyReport,
  listSecurityDailyReports,
  previewSecurityDailyReport,
  retrySecurityDailyReport,
  sendSecurityDailyReport,
  type DeliveryStatus,
  type GenerationStatus,
  type SecurityDailyOverview,
  type SecurityDailyReport,
  type SecurityStatus,
} from "../api/securityDaily"
import EmptyState from "../components/EmptyState.vue"
import { vRowActivate } from "../lib/directives"
import { useConfirmActions } from "../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import SecurityDailyOverviewPanel from "../components/SecurityDailyOverviewPanel.vue"
import SecurityDailyReportDetail from "../components/SecurityDailyReportDetail.vue"
import {
  deliveryLabels,
  deliverySegOptions,
  describeSecurityDailyReport,
  displayMoment,
  generationSegOptions,
  isHistoricSupersededSent,
  securityDailyGenerationMessage,
  statusLabel,
  statusSegOptions,
  tagType,
} from "../lib/securityDailyDisplay"

const overview = ref<SecurityDailyOverview | null>(null)
const detailLoading = ref(false)
const delivering = ref(false)
const previewLoading = ref(false)
const overviewErrorMessage = ref("")
const selected = ref<SecurityDailyReport | null>(null)
const drawerOpen = ref(false)
const previewText = ref("")
const previewOpen = ref(false)
const detailRead = useLatestRead()
const previewRead = useLatestRead()
watch(
  drawerOpen,
  (open) => {
    if (open) return
    detailRead.cancel()
    previewRead.cancel()
    selected.value = null
    detailLoading.value = false
    previewLoading.value = false
    previewOpen.value = false
    previewText.value = ""
  },
  { flush: "sync" },
)
watch(
  previewOpen,
  (open) => {
    if (!open) previewRead.cancel()
  },
  { flush: "sync" },
)
const configOpen = ref(false)
const configLoading = ref(false)
const generationLoading = ref(false)
const filters = reactive({
  dateFrom: "",
  dateTo: "",
  status: "" as SecurityStatus | "",
  generationStatus: "" as GenerationStatus | "",
  deliveryStatus: "" as DeliveryStatus | "",
  page: 1,
  pageSize: DEFAULT_PAGE_SIZE,
})
const filtersCollapsed = ref(true)

const filtering = computed(() =>
  Boolean(filters.dateFrom || filters.dateTo || filters.status || filters.generationStatus || filters.deliveryStatus),
)
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

const {
  items: reports,
  total,
  loading,
  errorMessage: reportsErrorMessage,
  load: loadReports,
} = usePagedList({
  page: toRef(filters, "page"),
  fetcher: (page, signal) =>
    listSecurityDailyReports(
      {
        dateFrom: filters.dateFrom || undefined,
        dateTo: filters.dateTo || undefined,
        status: filters.status || undefined,
        generationStatus: filters.generationStatus || undefined,
        deliveryStatus: filters.deliveryStatus || undefined,
        page,
        pageSize: filters.pageSize,
      },
      signal,
    ),
  errorMessage: "安全日报列表暂不可用，请刷新重试",
  formatError: (error) => apiErrorMessage(error, "安全日报列表暂不可用，请刷新重试"),
  clearOnLoad: true,
  clearOnError: true,
  pageSize: filters.pageSize,
})

const overviewRead = useLatestRead()
async function loadOverview(): Promise<void> {
  const signal = overviewRead.start()
  overviewErrorMessage.value = ""
  overview.value = null
  try {
    overview.value = await getSecurityDailyOverview(signal)
  } catch (error) {
    if (signal.aborted) return
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

function openConfiguration(): void {
  configOpen.value = true
}

async function refresh(): Promise<void> {
  await Promise.all([loadOverview(), loadReports()])
}

async function generateReport(): Promise<void> {
  if (
    !(await confirmAuditedAction({
      title: "确认立即生成",
      body: "将汇总前一自然日（北京时间）的脱敏结构化证据并新增一条日报记录，不覆盖历史记录；生成完成后立即提交邮件投递，该日已有处理中的投递请求时将被拒绝。",
      auditNote: "立即生成行为与操作人将写入审计日志。",
      confirmText: "立即生成并投递",
    }))
  )
    return
  try {
    generationLoading.value = true
    const report = await generateSecurityDailyReport()
    await refresh()
    await openReport(report.id)
    if (report.generation_status === "ready") {
      ElMessage.success(securityDailyGenerationMessage(report))
    } else {
      ElMessage.warning(securityDailyGenerationMessage(report))
    }
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, "安全日报生成失败，请刷新重试"))
  } finally {
    generationLoading.value = false
  }
}

async function openReport(reportId: number): Promise<void> {
  const signal = detailRead.start()
  previewRead.cancel()
  selected.value = null
  previewText.value = ""
  previewLoading.value = false
  detailLoading.value = true
  drawerOpen.value = true
  previewOpen.value = false
  // el-drawer 关闭后保留 DOM 与滚动位置，重新打开时先回到顶部，避免详情从中间开始显示
  void nextTick(() => {
    document.querySelector<HTMLElement>(".el-drawer__body")?.scrollTo({ top: 0 })
  })
  try {
    const report = await getSecurityDailyReport(reportId, signal)
    if (signal.aborted) return
    selected.value = report
  } catch (error) {
    if (signal.aborted) return
    ElMessage.error(apiErrorMessage(error, "日报详情暂不可用，请刷新重试"))
    drawerOpen.value = false
  } finally {
    if (!signal.aborted) detailLoading.value = false
  }
}

async function openPreview(): Promise<void> {
  if (!selected.value || previewLoading.value) return
  const reportId = selected.value.id
  const signal = previewRead.start()
  previewLoading.value = true
  try {
    const preview = await previewSecurityDailyReport(reportId, signal)
    if (signal.aborted) return
    previewText.value = preview.available ? preview.text : (preview.message ?? "数据不可用")
    previewOpen.value = true
  } catch (error) {
    if (signal.aborted) return
    ElMessage.error(apiErrorMessage(error, "预览暂不可用，请刷新重试"))
  } finally {
    if (!signal.aborted) previewLoading.value = false
  }
}

async function requestDelivery(action: "send" | "retry"): Promise<void> {
  if (!selected.value || delivering.value) return
  const report = selected.value
  const operation = action === "retry" ? "重试投递" : "手动投递"
  if (
    !(await confirmAuditedAction({
      title: `确认${operation}`,
      isCurrent: () => selected.value === report,
      body: `确认${operation} ${report.report_date} 的安全日报？邮件正文只来自已脱敏结构化报告，投递由独立投递器（mailer）执行并回写状态，同日重复投递有幂等保护。`,
      auditNote: `${operation}行为、操作人与日报 id 将写入审计日志。`,
      confirmText: `确认${operation}`,
    }))
  )
    return
  try {
    delivering.value = true
    if (action === "retry") {
      await retrySecurityDailyReport(report.id)
    } else {
      await sendSecurityDailyReport(report.id)
    }
    ElMessage.success("投递请求已受理，状态将在投递器回写后更新 · 本次操作已记入审计")
    await refresh()
    if (drawerOpen.value && selected.value?.id === report.id) await openReport(report.id)
  } catch (error) {
    ElMessage.error(apiErrorMessage(error, "投递请求失败，请刷新重试"))
  } finally {
    delivering.value = false
  }
}

function reportCopy(report: SecurityDailyReport) {
  return describeSecurityDailyReport(report)
}

function canSend(report: SecurityDailyReport): boolean {
  return Boolean(
    overview.value?.configuration_state === "ready" &&
    report.generation_status === "ready" &&
    report.delivery_status === "not_sent",
  )
}

function canRetry(report: SecurityDailyReport): boolean {
  return Boolean(
    overview.value?.configuration_state === "ready" &&
    report.generation_status === "ready" &&
    report.delivery_status === "failed" &&
    !(report.last_error ?? "").startsWith(deliveryLabels.unknown) &&
    !isHistoricSupersededSent(report),
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
      <h1>安全日报</h1>
      <p
        >固定 08:00（北京时间）汇总前一自然日；页面只展示脱敏结构化证据，手机号与密钥永不进入界面，Resend Key
        保存后不回显。写操作全部写入审计。</p
      >
    </div>
    <div class="security-daily-heading-actions">
      <el-button :loading="configLoading" @click="openConfiguration">配置邮件</el-button>
      <el-button :disabled="!overview?.enabled" :loading="generationLoading" @click="generateReport"
        >立即生成</el-button
      >
      <el-button :loading="loading" @click="refresh">刷新</el-button>
    </div>
  </section>

  <LoadErrorAlert class="security-daily-alert" :message="overviewErrorMessage" @retry="loadOverview" />

  <SecurityDailyOverviewPanel v-if="overview" :overview="overview" />

  <div>
    <form
      class="security-daily-filter-bar filter-collapsible"
      :class="{ 'is-collapsed': filtersCollapsed }"
      @submit.prevent="search"
    >
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
      <div class="security-daily-fld filter-keep">
        <span>安全状态</span>
        <FilterSeg
          :model-value="filters.status"
          :options="statusSegOptions"
          button-testid-prefix="security-daily-status"
          class="filter-seg--compact"
          label="安全状态筛选"
          data-testid="security-daily-status-seg"
          @update:model-value="setStatus"
        />
      </div>
      <div class="security-daily-fld">
        <span>生成状态</span>
        <FilterSeg
          :model-value="filters.generationStatus"
          :options="generationSegOptions"
          button-testid-prefix="security-daily-generation"
          class="filter-seg--compact"
          label="生成状态筛选"
          data-testid="security-daily-generation-seg"
          @update:model-value="setGenerationStatus"
        />
      </div>
      <div class="security-daily-fld">
        <span>投递状态</span>
        <FilterSeg
          :model-value="filters.deliveryStatus"
          :options="deliverySegOptions"
          button-testid-prefix="security-daily-delivery"
          class="filter-seg--compact"
          label="投递状态筛选"
          data-testid="security-daily-delivery-seg"
          @update:model-value="setDeliveryStatus"
        />
      </div>
      <MobileFilterToggle
        v-model:collapsed="filtersCollapsed"
        :active-count="
          (filters.dateFrom || filters.dateTo ? 1 : 0) +
          (filters.generationStatus ? 1 : 0) +
          (filters.deliveryStatus ? 1 : 0)
        "
      />
      <div class="security-daily-filter-go filter-keep">
        <el-button data-testid="security-daily-search" type="primary" native-type="submit" :loading="loading"
          >查询</el-button
        >
        <el-button data-testid="security-daily-reset" @click="resetFilters">重置</el-button>
      </div>
    </form>
    <p class="security-daily-privacy"
      >安全 / 生成 /
      投递状态点选即重查，报告日期经「查询」生效并先校验起止先后；筛选与分页均在服务端执行，不走「接口全量返回 ·
      前端过滤」。页面只展示脱敏结构化证据，手机号与密钥不进入本页。</p
    >
  </div>

  <aside class="security-daily-rules" aria-label="脱敏边界、配置例外与投递语义">
    <div
      ><span>脱敏边界</span><p>页面只展示脱敏结构化证据，手机号与密钥永不进入界面；详情与预览同样只是只读投影。</p></div
    >
    <div
      ><span>配置例外</span
      ><p>Resend Key 明文仅存专用配置并同步独立投递器，审计只记 configured 状态与收件人数量；Key 保存后不回显。</p></div
    >
    <div
      ><span>投递语义</span
      ><p>投递由独立投递器执行并回写状态，页面查询时惰性同步；投递失败可重试，同日重复投递有幂等保护。</p></div
    >
  </aside>

  <LoadErrorAlert class="security-daily-alert" :message="reportsErrorMessage" @retry="loadReports" />

  <section class="security-daily-results">
    <template v-if="reports.length || loading">
      <el-table v-loading="loading" :data="reports" row-key="id" class="security-daily-table" @row-click="openRow">
        <el-table-column prop="id" label="记录" width="75" />
        <el-table-column prop="report_date" label="报告日期" width="120" />
        <el-table-column label="安全状态" width="105"
          ><template #default="scope"
            ><el-tag :type="tagType(scope.row.status)" size="small">{{
              statusLabel(scope.row.status)
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column label="生成方式" width="90"
          ><template #default="scope"
            ><el-tag :type="scope.row.generation_source === 'manual' ? 'warning' : 'info'" size="small">{{
              scope.row.generation_source === "manual" ? "手动" : "自动"
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column label="生成" width="110"
          ><template #default="scope"
            ><el-tag :type="tagType(scope.row.generation_status)" size="small">{{
              reportCopy(scope.row).generationLabel
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column label="投递" width="125"
          ><template #default="scope"
            ><el-tag :type="reportCopy(scope.row).deliveryTagType" size="small">{{
              reportCopy(scope.row).deliveryLabel
            }}</el-tag></template
          ></el-table-column
        >
        <el-table-column prop="recipient_count" label="收件人数" width="90" />
        <el-table-column label="生成时间" width="170"
          ><template #default="scope"
            ><time>{{ displayMoment(scope.row.generated_at) }}</time></template
          ></el-table-column
        >
        <el-table-column label="投递时间" width="170"
          ><template #default="scope"
            ><time>{{ displayMoment(scope.row.delivered_at) }}</time></template
          ></el-table-column
        >
        <el-table-column prop="retry_count" label="重试" width="65" />
        <el-table-column label="最新错误" min-width="180"
          ><template #default="scope">{{ scope.row.last_error ?? "—" }}</template></el-table-column
        >
        <el-table-column label="操作" width="170" fixed="right"
          ><template #default="scope"
            ><el-button
              v-row-activate="() => openReport(scope.row.id)"
              link
              type="primary"
              :aria-label="`查看 ${scope.row.report_date} 安全日报详情`"
              @click.stop="openReport(scope.row.id)"
              >查看详情</el-button
            ><el-button
              v-if="canRetry(scope.row)"
              link
              type="warning"
              :aria-label="`处理 ${scope.row.report_date} 安全日报的投递失败`"
              @click.stop="openReport(scope.row.id)"
              >处理失败</el-button
            ></template
          ></el-table-column
        >
      </el-table>
    </template>
    <div v-else-if="!reportsErrorMessage && filtering" class="security-daily-empty-action">
      <EmptyState title="没有符合筛选条件的安全日报" description="调整报告日期或状态筛选后重新查询。" />
      <el-button data-testid="security-daily-clear-filters" @click="resetFilters">清除筛选</el-button>
    </div>
    <div v-else-if="!reportsErrorMessage" class="security-daily-empty-action">
      <EmptyState :title="reportsEmptyTitle" :description="reportsEmptyDescription" />
    </div>

    <ListPagination
      v-model:page="filters.page"
      :total="total"
      :page-size="filters.pageSize"
      unit="条"
      @change="loadReports"
    ></ListPagination>
  </section>

  <el-drawer v-model="drawerOpen" title="安全日报详情" size="min(760px, 92vw)" :teleported="false">
    <el-skeleton v-if="detailLoading" :rows="8" animated />
    <SecurityDailyReportDetail
      v-else-if="selected"
      :selected="selected"
      :preview-loading="previewLoading"
      :delivering="delivering"
      :can-send="canSend(selected)"
      :can-retry="canRetry(selected)"
      @preview="openPreview"
      @deliver="requestDelivery"
    />
  </el-drawer>

  <el-dialog v-model="previewOpen" title="安全日报纯文本预览" width="min(720px, 92vw)">
    <pre class="security-preview-text">{{ previewText }}</pre>
  </el-dialog>

  <SecurityDailyConfigDialog v-model="configOpen" @saved="refresh" @loading="configLoading = $event" />
</template>
