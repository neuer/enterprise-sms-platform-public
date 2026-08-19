<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import {
  createDetailExport,
  downloadExport,
  getExportTask,
  getReport,
  issueExportStepUp,
  type ExportTask,
  type ReportCategory,
  type ReportFilters,
  type ReportGranularity,
  type ReportGroupBy,
  type ReportResult,
  type ReportTrendMetric,
} from "../api/reports"
import ReportTrendChart from "../components/ReportTrendChart.vue"
import EmptyState from "../components/EmptyState.vue"
import { CHART_DIM_PALETTE } from "../lib/chartTheme"
import { reportTrendDims } from "../lib/reportTrend"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()

const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" }).format(new Date())
const startDefault = new Date(`${today}T00:00:00+08:00`)
startDefault.setDate(startDefault.getDate() - 29)

const granularity = ref<ReportGranularity>("day")
const groupBy = ref<ReportGroupBy>("app")
const category = ref<ReportCategory>("all")
const dateRange = ref<[string, string]>([startDefault.toISOString().slice(0, 10), today])
const result = ref<ReportResult | null>(null)
const loading = ref(false)
const errorMessage = ref("")
const exportTask = ref<ExportTask | null>(null)
const exportLoading = ref(false)
const exportError = ref("")
const decrypted = ref(false)
const metric = ref<ReportTrendMetric>("total")
const page = ref(1)
const pageSize = 20
/** 最后一次成功查询的条件快照；与当前表单不一致时提示「条件已变更」，不自动重查。 */
const applied = ref<ReportFilters | null>(null)
let pollTimer: number | undefined

const canDecrypt = computed(() => result.value?.can_export_decrypted === true)
const filters = computed<ReportFilters>(() => ({
  granularity: granularity.value,
  groupBy: groupBy.value,
  category: category.value,
  start: dateRange.value[0],
  end: dateRange.value[1],
}))
const filtersDirty = computed(
  () => applied.value !== null && JSON.stringify(applied.value) !== JSON.stringify(filters.value),
)

/** 数据权限口径提示：admin/approver 全平台，operator/viewer 固定本部门。 */
const scopeLabel = computed(() => {
  if (session.role === "admin" || session.role === "approver") return `全平台 · ${session.roleLabel}`
  if (session.role) return `本部门 · ${session.dept || "—"} · ${session.roleLabel}`
  return "数据权限口径加载中"
})

const granularityLabel: Record<ReportGranularity, string> = { day: "日", week: "周", month: "月" }
const granularityOptions: Array<{ label: string; value: ReportGranularity }> = [
  { label: "日", value: "day" },
  { label: "周", value: "week" },
  { label: "月", value: "month" },
]
const groupByOptions: Array<{ label: string; value: ReportGroupBy }> = [
  { label: "应用", value: "app" },
  { label: "部门", value: "dept" },
]
const metricOptions: Array<{ label: string; value: ReportTrendMetric }> = [
  { label: "消息数", value: "total" },
  { label: "计费条", value: "total_segments" },
]
const statusLabel: Record<ExportTask["status"], string> = {
  pending: "等待生成", running: "生成中", done: "已完成", failed: "生成失败",
}

const dimLabel = computed(() => (result.value?.group_by === "dept" ? "部门" : "应用"))

/** 周期级消息数汇总（纯加法），用于 KPI 的均值与峰值。 */
const periodTotals = computed(() => {
  const totals = new Map<string, number>()
  for (const item of result.value?.items ?? []) {
    totals.set(item.period_start, (totals.get(item.period_start) ?? 0) + item.total)
  }
  return [...totals.entries()].sort((left, right) => left[0].localeCompare(right[0]))
})
const periodAverage = computed(() => {
  if (!result.value || periodTotals.value.length === 0) return null
  return Math.round(result.value.summary.total / periodTotals.value.length)
})
const periodPeak = computed(() => {
  if (periodTotals.value.length === 0) return null
  return periodTotals.value.reduce((max, entry) => (entry[1] > max[1] ? entry : max))
})
const segmentsPerMessage = computed(() => {
  const summary = result.value?.summary
  if (!summary || summary.total === 0) return "—"
  return (summary.total_segments / summary.total).toFixed(2)
})
const rangeDays = computed(() => {
  if (!result.value) return null
  const ms = Date.parse(result.value.end) - Date.parse(result.value.start)
  if (!Number.isFinite(ms) || ms < 0) return null
  return Math.round(ms / 86_400_000) + 1
})
const trendLegend = computed(() =>
  result.value ? reportTrendDims(result.value.items, metric.value) : [],
)

function dimColor(index: number): string {
  return CHART_DIM_PALETTE[index % CHART_DIM_PALETTE.length]
}

const averageLabel = computed(() => {
  const grain = result.value?.granularity
  if (grain === "week") return "周均"
  if (grain === "month") return "月均"
  return "日均"
})

type SortProp = "period_start" | "total" | "total_segments" | "success_rate"
const sortState = ref<{ prop: SortProp; order: "ascending" | "descending" }>({
  prop: "period_start",
  order: "descending",
})

function onSortChange(event: { prop: SortProp; order: "ascending" | "descending" | null }): void {
  sortState.value = event.order
    ? { prop: event.prop, order: event.order }
    : { prop: "period_start", order: "descending" }
}

const sortedItems = computed(() => {
  const rows = [...(result.value?.items ?? [])]
  const { prop, order } = sortState.value
  const direction = order === "ascending" ? 1 : -1
  rows.sort((left, right) => {
    const primary = prop === "period_start"
      ? left.period_start.localeCompare(right.period_start)
      : left[prop] - right[prop]
    return (primary || left.dim_label.localeCompare(right.dim_label)) * direction
  })
  return rows
})
const pagedItems = computed(() =>
  sortedItems.value.slice((page.value - 1) * pageSize, page.value * pageSize),
)

function formatRate(rate: number): string {
  return `${(rate * 100).toFixed(1)}%`
}

/** 成功率芯片着色阈值（≥98 绿 / 95–98 黄 / <95 红）：纯展示逻辑，口径仍来自服务端。 */
function rateClass(rate: number): string {
  if (rate >= 0.98) return "g"
  if (rate >= 0.95) return "y"
  return "r"
}

/** 构成占比（加法 + 除法，非成功率口径）。 */
function shareOf(value: number): string {
  const total = result.value?.summary.total ?? 0
  if (total === 0) return "0.0%"
  return `${((value / total) * 100).toFixed(1)}%`
}

function composeWidth(value: number): string {
  const total = result.value?.summary.total ?? 0
  if (total === 0 || value === 0) return "0%"
  return `${Math.max((value / total) * 100, 0.4)}%`
}

function rankWidth(total: number): string {
  const max = result.value?.dim_summary[0]?.total ?? 0
  if (max === 0) return "0%"
  return `${Math.max((total / max) * 100, 2)}%`
}

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const next = await getReport(filters.value)
    if (token !== loadToken) return
    result.value = next
    applied.value = { ...filters.value }
    page.value = 1
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "报表加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function schedulePoll(): void {
  // 先清旧定时器：重复点击“导出”会开启新任务，否则新旧两条轮询链会同时存在。
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
  if (!exportTask.value || ["done", "failed"].includes(exportTask.value.status)) return
  pollTimer = window.setTimeout(() => void refreshExport(), 2000)
}

async function refreshExport(): Promise<void> {
  if (!exportTask.value) return
  try {
    exportTask.value = await getExportTask(exportTask.value.id)
    schedulePoll()
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出状态查询失败"
  }
}

async function createExport(): Promise<void> {
  exportLoading.value = true
  exportError.value = ""
  try {
    exportTask.value = await createDetailExport(filters.value, decrypted.value)
    await refreshExport()
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出创建失败"
  } finally {
    exportLoading.value = false
  }
}

async function download(): Promise<void> {
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
    anchor.download = `sms-report-${exportTask.value.id}.csv`
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

onMounted(() => void load())
onBeforeUnmount(() => {
  if (pollTimer !== undefined) window.clearTimeout(pollTimer)
})
</script>

<template>
  <section class="page-heading report-heading">
    <div>
      <p class="eyebrow">ANALYTICS / 统计报表</p>
      <h1>统计报表</h1>
      <p>日、周、月多维聚合，消息数与计费条使用同一事实源。</p>
    </div>
    <span class="report-scope" data-testid="report-scope"><i></i>当前口径：{{ scopeLabel }}</span>
  </section>

  <form class="report-filter-bar" @submit.prevent="load">
    <div class="report-fld">
      <span>周期</span>
      <div class="report-seg" role="group" aria-label="周期">
        <button
          v-for="opt in granularityOptions"
          :key="opt.value"
          type="button"
          :class="{ on: granularity === opt.value }"
          @click="granularity = opt.value"
        >{{ opt.label }}</button>
      </div>
    </div>
    <div class="report-fld">
      <span>维度</span>
      <div class="report-seg" role="group" aria-label="维度">
        <button
          v-for="opt in groupByOptions"
          :key="opt.value"
          type="button"
          :class="{ on: groupBy === opt.value }"
          :data-testid="`report-group-${opt.value}`"
          @click="groupBy = opt.value"
        >{{ opt.label }}</button>
      </div>
    </div>
    <div class="report-fld">
      <span>类别</span>
      <el-select v-model="category" class="report-pill-select">
        <el-option label="全部类别" value="all" />
        <el-option label="验证码" value="verify" />
        <el-option label="通知" value="notice" />
        <el-option label="营销" value="market" />
      </el-select>
    </div>
    <div class="report-fld">
      <span>范围</span>
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        popper-class="qingluan-date-popper"
        value-format="YYYY-MM-DD"
        range-separator="→"
        start-placeholder="开始日期"
        end-placeholder="结束日期"
      />
    </div>
    <el-button type="primary" native-type="submit" class="report-filter-go" :loading="loading">查询</el-button>
    <el-button :loading="exportLoading" @click="createExport">导出明细 CSV</el-button>
    <el-checkbox v-if="canDecrypt" v-model="decrypted" class="report-decrypted">含明文手机号</el-checkbox>
  </form>
  <p v-if="filtersDirty" class="report-dirty-hint">筛选条件已变更，点击「查询」刷新结果。</p>

  <div v-if="exportTask || exportError" class="export-strip" data-testid="export-strip">
    <template v-if="exportTask">
      <span class="export-tag" :class="exportTask.status">{{ statusLabel[exportTask.status] }}</span>
      <span class="export-id">导出明细 <code>#{{ exportTask.id.slice(0, 8) }}</code></span>
      <strong v-if="exportTask.row_count !== null">{{ exportTask.row_count.toLocaleString() }} 行</strong>
      <span class="export-mode">{{ exportTask.decrypted ? "明文导出 · 已记审计" : "掩码导出 · 不含明文手机号" }}</span>
      <small v-if="exportTask.expires_at">保留至 {{ exportTask.expires_at.slice(0, 10) }}</small>
      <el-button v-if="exportTask.download_url" link type="primary" class="export-download" @click="download">下载 CSV ↓</el-button>
    </template>
    <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" class="export-strip-error" />
  </div>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="report-error"><template #default><el-button link type="primary" @click="load">重新查询</el-button></template></el-alert>

  <template v-if="result">
    <section class="report-kpis" aria-label="区间关键指标">
      <el-card shadow="never" class="report-kpi">
        <span>消息数</span>
        <strong>{{ result.summary.total.toLocaleString() }}</strong>
        <small>{{ result.start }} — {{ result.end }}{{ rangeDays === null ? "" : ` · ${rangeDays} 天` }}</small>
        <p class="kpi-foot">
          {{ averageLabel }} {{ periodAverage === null ? "—" : periodAverage.toLocaleString() }}
          · 峰值 {{ periodPeak === null ? "—" : `${periodPeak[0].slice(5)}（${periodPeak[1].toLocaleString()}）` }}
        </p>
      </el-card>
      <el-card shadow="never" class="report-kpi">
        <span>计费条</span>
        <strong>{{ result.summary.total_segments.toLocaleString() }}</strong>
        <small>最终内容计费口径 · 与厂商账单对账</small>
        <p class="kpi-foot">条 / 消息 {{ segmentsPerMessage }}</p>
      </el-card>
      <el-card shadow="never" class="report-kpi">
        <span>送达成功率</span>
        <strong>{{ formatRate(result.summary.success_rate) }}</strong>
        <small>delivered / (delivered + failed)，unknown 不入分母</small>
        <div class="kpi-kv"><span>送达 delivered</span><b>{{ result.summary.delivered.toLocaleString() }}</b></div>
        <div class="kpi-kv"><span>失败 failed</span><b class="neg">{{ result.summary.failed.toLocaleString() }}</b></div>
      </el-card>
      <el-card shadow="never" class="report-kpi">
        <span>结果构成</span>
        <strong>{{ result.summary.unknown.toLocaleString() }}<small class="strong-note">unknown 待终态</small></strong>
        <div class="compose-strip" aria-label="结果构成">
          <i class="d" :style="{ width: composeWidth(result.summary.delivered) }" :title="`送达 ${result.summary.delivered.toLocaleString()}`"></i><i class="f" :style="{ width: composeWidth(result.summary.failed) }" :title="`失败 ${result.summary.failed.toLocaleString()}`"></i><i class="u" :style="{ width: composeWidth(result.summary.unknown) }" :title="`未知 ${result.summary.unknown.toLocaleString()}`"></i>
        </div>
        <div class="kpi-kv compose"><span>送达 {{ shareOf(result.summary.delivered) }}</span><span>失败 {{ shareOf(result.summary.failed) }}</span><span>未知 {{ shareOf(result.summary.unknown) }}</span></div>
      </el-card>
    </section>

    <section class="report-main-grid">
      <el-card shadow="never" class="report-chart-card">
        <template #header>
          <div class="panel-title">
            <div><strong>发送趋势 · 按{{ dimLabel }}堆叠</strong><small>{{ granularityLabel[result.granularity] }}粒度 · Top 5 + 其他归并</small></div>
            <div class="metric-switch" role="group" aria-label="趋势指标">
              <button
                v-for="opt in metricOptions"
                :key="opt.value"
                type="button"
                :class="{ on: metric === opt.value }"
                @click="metric = opt.value"
              >{{ opt.label }}</button>
            </div>
          </div>
        </template>
        <ReportTrendChart
          v-if="result.items.length"
          :items="result.items"
          :metric="metric"
          :start="result.start"
          :end="result.end"
          :granularity="result.granularity"
        />
        <div v-if="result.items.length" class="trend-legend">
          <span v-for="(dim, index) in trendLegend" :key="dim.key">
            <i :style="{ background: dimColor(index) }"></i>{{ dim.label }}
          </span>
          <em>Top 5 + 其他归并 · 加法聚合</em>
        </div>
        <EmptyState v-else title="当前条件没有统计数据" description="调整日期、类别或分组方式后重新查询。" />
      </el-card>

      <el-card shadow="never" class="report-rank-card">
        <template #header>
          <div class="panel-title">
            <div><strong>维度排行 · {{ dimLabel }}</strong><small>按消息数 · 区间为整个筛选范围</small></div>
            <span>{{ result.dim_summary.length }} 个{{ dimLabel }}</span>
          </div>
        </template>
        <ul v-if="result.dim_summary.length" class="rank-list">
          <li v-for="(dim, index) in result.dim_summary" :key="dim.dim_value">
            <span class="rank-name" :title="dim.dim_label">{{ dim.dim_label }}</span>
            <div class="rank-track"><i :style="{ width: rankWidth(dim.total), background: dimColor(index) }"></i></div>
            <span class="rank-num">
              <b>{{ dim.total.toLocaleString() }}</b>
              <small>{{ shareOf(dim.total) }} · 计费条 {{ dim.total_segments.toLocaleString() }}</small>
            </span>
            <span class="rate-chip" :class="rateClass(dim.success_rate)">{{ formatRate(dim.success_rate) }}</span>
          </li>
        </ul>
        <EmptyState v-else title="暂无维度数据" description="调整日期、类别或分组方式后重新查询。" />
        <p class="rank-note">成功率由服务端按统一口径返回；芯片着色（≥98 绿 / 95–98 黄 / &lt;95 红）仅为展示阈值。</p>
      </el-card>
    </section>

    <el-card shadow="never" class="report-table-card">
      <template #header>
        <div class="panel-title">
          <div><strong>明细 · 周期 × {{ dimLabel }}</strong><small>点击列头排序</small></div>
          <span>共 {{ result.items.length }} 行</span>
        </div>
      </template>
      <el-table
        :data="pagedItems"
        class="report-table"
        :default-sort="{ prop: 'period_start', order: 'descending' }"
        @sort-change="onSortChange"
      >
        <el-table-column prop="period_start" label="周期" width="120" sortable="custom" />
        <el-table-column prop="dim_label" :label="dimLabel" min-width="140" />
        <el-table-column prop="total" label="消息数" width="100" align="right" sortable="custom"><template #default="{ row }">{{ row.total.toLocaleString() }}</template></el-table-column>
        <el-table-column prop="total_segments" label="计费条" width="100" align="right" sortable="custom"><template #default="{ row }">{{ row.total_segments.toLocaleString() }}</template></el-table-column>
        <el-table-column prop="delivered" label="送达" width="90" align="right"><template #default="{ row }">{{ row.delivered.toLocaleString() }}</template></el-table-column>
        <el-table-column prop="failed" label="失败" width="80" align="right"><template #default="{ row }">{{ row.failed.toLocaleString() }}</template></el-table-column>
        <el-table-column prop="unknown" label="未知" width="80" align="right"><template #default="{ row }">{{ row.unknown.toLocaleString() }}</template></el-table-column>
        <el-table-column prop="success_rate" label="成功率" width="110" align="right" sortable="custom"><template #default="{ row }"><span class="rate-chip" :class="rateClass(row.success_rate)">{{ formatRate(row.success_rate) }}</span></template></el-table-column>
      </el-table>
      <div v-if="result.items.length > pageSize" class="report-pager">
        <el-pagination
          layout="prev, pager, next, total"
          :total="result.items.length"
          :page-size="pageSize"
          :current-page="page"
          @current-change="(next: number) => (page = next)"
        />
      </div>
      <div class="report-mobile-list"><article v-for="item in pagedItems" :key="`${item.period_start}-${item.dim_value}`"><header><time>{{ item.period_start }}</time><strong>{{ item.dim_label }}</strong></header><dl><div><dt>消息数</dt><dd>{{ item.total }}</dd></div><div><dt>计费条</dt><dd>{{ item.total_segments }}</dd></div><div><dt>成功率</dt><dd>{{ formatRate(item.success_rate) }}</dd></div></dl><p>送达 {{ item.delivered }} · 失败 {{ item.failed }} · 未知 {{ item.unknown }}</p></article></div>
    </el-card>
  </template>
</template>
