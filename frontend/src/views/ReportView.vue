<script setup lang="ts">
import { usePagedList } from "../composables/usePagedList"
import ListPagination from "../components/ListPagination.vue"
import FilterSeg from "../components/FilterSeg.vue"
import { computed, onMounted, ref } from "vue"

import {
  createDetailExport,
  getReport,
  type ExportTask,
  type ReportCategory,
  type ReportFilters,
  type ReportGranularity,
  type ReportGroupBy,
  type ReportResult,
  type ReportRow,
  type ReportTrendMetric,
} from "../api/reports"
import ReportTrendChart from "../components/ReportTrendChart.vue"
import EmptyState from "../components/EmptyState.vue"
import { useExportTask } from "../composables/useExportTask"
import { CHART_DIM_VARS } from "../lib/chartTheme"
import { CATEGORY_OPTIONS, DEFAULT_PAGE_SIZE } from "../lib/labels"
import { daysAgoDateKey, shanghaiDateKey } from "../lib/time"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()

// 默认范围：Asia/Shanghai 日历口径的近 30 天（含今天）；旧实现 setDate+toISOString 存在时区 off-by-one。
// 每次调用重新求值，保证「重置」回到当下口径的近 30 天而不是模块加载时刻。
function defaultDateRange(): [string, string] {
  return [daysAgoDateKey(29), shanghaiDateKey()]
}
const dateRange = ref<[string, string]>(defaultDateRange())

const granularity = ref<ReportGranularity>("day")
const groupBy = ref<ReportGroupBy>("app")
const category = ref<ReportCategory>("all")
const result = ref<ReportResult | null>(null)
const {
  exportTask,
  exportBusy: exportLoading,
  exportError,
  start: startExportTask,
  download: downloadExportFile,
} = useExportTask({ timeoutMessage: "导出结果等待超时（已超过 5 分钟），请稍后重新发起导出" })
const decrypted = ref(false)
const metric = ref<ReportTrendMetric>("total")
const pageSize = DEFAULT_PAGE_SIZE
/** 最后一次成功查询的条件快照；与当前表单不一致时提示「条件已变更」，不自动重查。 */
const applied = ref<ReportFilters | null>(null)

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
  if (session.canDecrypt) return `全平台 · ${session.roleLabel}`
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
  pending: "等待生成",
  running: "生成中",
  done: "已完成",
  failed: "生成失败",
}

const dimLabel = computed(() => (result.value?.group_by === "dept" ? "部门" : "应用"))

/** 周期级消息数汇总（纯加法），用于 KPI 的均值与峰值。 */
const periodTotals = computed(() => {
  const trend = result.value?.trend
  if (!trend) return []
  return trend.periods.map((period, index): [string, number] => [
    period,
    trend.series.reduce((sum, series) => sum + (series.total[index] ?? 0), 0),
  ])
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
const trendLegend = computed(() => result.value?.trend.series ?? [])

function dimColor(index: number): string {
  // 图例色块是 DOM 元素，用 var() 引用令牌即可随主题自动切换
  return `var(${CHART_DIM_VARS[index % CHART_DIM_VARS.length]})`
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
  const next = event.order
    ? { prop: event.prop, order: event.order }
    : { prop: "period_start" as const, order: "descending" as const }
  if (next.prop === sortState.value.prop && next.order === sortState.value.order) return
  sortState.value = next
  void load(applied.value ?? filters.value)
}

const pagedItems = computed(() => result.value?.items ?? [])

function changePage(next: number): void {
  void load(applied.value ?? filters.value, next)
}

function changeMetric(next: ReportTrendMetric): void {
  if (metric.value === next) return
  metric.value = next
  void load(applied.value ?? filters.value, page.value)
}

/** 明细行稳定键：周期 × 维度值，与移动端列表同一口径。 */
function reportRowKey(row: ReportRow): string {
  return `${row.period_start}-${row.dim_value}`
}

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
function shareOf(value: number, total = result.value?.summary.total ?? 0): string {
  if (total === 0) return "0.0%"
  return `${((value / total) * 100).toFixed(1)}%`
}

function composeWidth(value: number): string {
  const total = result.value?.summary.total ?? 0
  if (total === 0 || value === 0) return "0%"
  return `${Math.max((value / total) * 100, 0.4)}%`
}

function rankWidth(total: number): string {
  const metric = result.value?.metric ?? "total"
  const max = Math.max(0, ...(result.value?.dim_summary.map((item) => item[metric]) ?? []))
  if (max === 0) return "0%"
  return `${Math.max((total / max) * 100, 2)}%`
}

let requestedFilters: ReportFilters
const {
  page,
  loading,
  errorMessage,
  load: loadPage,
} = usePagedList({
  fetcher: async (page, signal) => {
    const query = { ...requestedFilters }
    const next = await getReport(
      query,
      {
        page,
        size: pageSize,
        sort: sortState.value.prop,
        order: sortState.value.order === "ascending" ? "asc" : "desc",
        metric: metric.value,
      },
      signal,
    )
    return { ...next, query }
  },
  errorMessage: "报表加载失败",
  onLoaded: (next) => {
    result.value = next
    applied.value = next.query
  },
})

async function load(query: ReportFilters = filters.value, requestedPage = 1): Promise<void> {
  requestedFilters = { ...query }
  page.value = requestedPage
  await loadPage()
}

/** 恢复默认筛选（近 30 天 · 日粒度 · 按应用 · 全类别）并立即重查；日期范围按当下重新求值。 */
function resetFilters(): void {
  granularity.value = "day"
  groupBy.value = "app"
  category.value = "all"
  dateRange.value = defaultDateRange()
  void load()
}

async function createExport(): Promise<void> {
  await startExportTask(() => createDetailExport(filters.value, decrypted.value))
}

async function download(): Promise<void> {
  await downloadExportFile("sms-report")
}

onMounted(() => void load())
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

  <form class="report-filter-bar" @submit.prevent="load()">
    <div class="report-fld">
      <span>周期</span>
      <FilterSeg v-model="granularity" :options="granularityOptions" aria-label="周期" />
    </div>
    <div class="report-fld">
      <span>维度</span>
      <FilterSeg v-model="groupBy" :options="groupByOptions" button-testid-prefix="report-group" aria-label="维度" />
    </div>
    <div class="report-fld">
      <span>类别</span>
      <el-select v-model="category" class="report-pill-select">
        <el-option label="全部类别" value="all" />
        <el-option v-for="option in CATEGORY_OPTIONS" :key="option.value" :label="option.label" :value="option.value" />
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
    <el-button data-testid="report-reset" @click="resetFilters">重置</el-button>
    <el-button :loading="exportLoading" @click="createExport">导出明细 CSV</el-button>
    <el-checkbox v-if="canDecrypt" v-model="decrypted" class="report-decrypted">含明文手机号</el-checkbox>
  </form>
  <p v-if="filtersDirty" class="report-dirty-hint">筛选条件已变更，点击「查询」刷新结果。</p>

  <div v-if="exportTask || exportError" class="export-strip" data-testid="export-strip">
    <template v-if="exportTask">
      <span class="export-tag" :class="exportTask.status">{{ statusLabel[exportTask.status] }}</span>
      <span class="export-id"
        >导出明细 <code>#{{ exportTask.id.slice(0, 8) }}</code></span
      >
      <strong v-if="exportTask.row_count !== null">{{ exportTask.row_count.toLocaleString() }} 行</strong>
      <span class="export-mode">{{ exportTask.decrypted ? "明文导出 · 已记审计" : "掩码导出 · 不含明文手机号" }}</span>
      <small v-if="exportTask.expires_at">保留至 {{ exportTask.expires_at.slice(0, 10) }}</small>
      <el-button v-if="exportTask.download_url" link type="primary" class="export-download" @click="download"
        >下载 CSV ↓</el-button
      >
    </template>
    <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" class="export-strip-error" />
  </div>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="report-error"
    ><template #default><el-button link type="primary" @click="load()">重新查询</el-button></template></el-alert
  >

  <template v-if="result">
    <section class="report-kpis" aria-label="区间关键指标">
      <el-card shadow="never" class="report-kpi">
        <span>消息数</span>
        <strong>{{ result.summary.total.toLocaleString() }}</strong>
        <small>{{ result.start }} — {{ result.end }}{{ rangeDays === null ? "" : ` · ${rangeDays} 天` }}</small>
        <p class="kpi-foot">
          {{ averageLabel }} {{ periodAverage === null ? "—" : periodAverage.toLocaleString() }} · 峰值
          {{ periodPeak === null ? "—" : `${periodPeak[0].slice(5)}（${periodPeak[1].toLocaleString()}）` }}
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
        <small>送达 /（送达 + 失败），未知不入分母</small>
        <div class="kpi-kv"
          ><span>送达</span><b>{{ result.summary.delivered.toLocaleString() }}</b></div
        >
        <div class="kpi-kv"
          ><span>失败</span><b class="neg">{{ result.summary.failed.toLocaleString() }}</b></div
        >
      </el-card>
      <el-card shadow="never" class="report-kpi">
        <span>结果构成</span>
        <strong>{{ result.summary.unknown.toLocaleString() }}<small class="strong-note">未知 · 待终态</small></strong>
        <div class="compose-strip" aria-label="结果构成">
          <i
            class="d"
            :style="{ width: composeWidth(result.summary.delivered) }"
            :title="`送达 ${result.summary.delivered.toLocaleString()}`"
          ></i
          ><i
            class="f"
            :style="{ width: composeWidth(result.summary.failed) }"
            :title="`失败 ${result.summary.failed.toLocaleString()}`"
          ></i
          ><i
            class="u"
            :style="{ width: composeWidth(result.summary.unknown) }"
            :title="`未知 ${result.summary.unknown.toLocaleString()}`"
          ></i>
        </div>
        <div class="kpi-kv compose-shares"
          ><span>送达 {{ shareOf(result.summary.delivered) }}</span
          ><span>失败 {{ shareOf(result.summary.failed) }}</span
          ><span>未知 {{ shareOf(result.summary.unknown) }}</span></div
        >
      </el-card>
    </section>

    <section class="report-main-grid">
      <el-card shadow="never" class="report-chart-card">
        <template #header>
          <div class="panel-title">
            <div
              ><strong>发送趋势 · 按{{ dimLabel }}堆叠</strong
              ><small>{{ granularityLabel[result.granularity] }}粒度 · Top 5 + 其他归并</small></div
            >
            <FilterSeg
              :model-value="metric"
              :options="metricOptions"
              class="filter-seg--pill"
              aria-label="趋势指标"
              @update:model-value="changeMetric"
            />
          </div>
        </template>
        <ReportTrendChart
          v-if="result.trend.periods.length"
          :trend="result.trend"
          :metric="result.metric"
          :start="result.start"
          :end="result.end"
          :granularity="result.granularity"
        />
        <div v-if="result.trend.periods.length" class="trend-legend">
          <span v-for="(dim, index) in trendLegend" :key="`${dim.is_other}-${dim.dim_value}`">
            <i :style="{ background: dimColor(index) }"></i>{{ dim.dim_label }}
          </span>
          <em>Top 5 + 其他归并 · 加法聚合</em>
        </div>
        <EmptyState v-else title="当前条件没有统计数据" description="调整日期、类别或分组方式后重新查询。" />
      </el-card>

      <el-card shadow="never" class="report-rank-card">
        <template #header>
          <div class="panel-title">
            <div
              ><strong>维度排行 · {{ dimLabel }}</strong
              ><small>按{{ result.metric === "total" ? "消息数" : "计费条" }} · Top 5 + 其他 · 完整筛选范围</small></div
            >
            <span>共 {{ result.dimension_total }} 个{{ dimLabel }}</span>
          </div>
        </template>
        <ul v-if="result.dim_summary.length" class="rank-list">
          <li v-for="(dim, index) in result.dim_summary" :key="`${dim.is_other}-${dim.dim_value}`">
            <span class="rank-name" :title="dim.dim_label">{{ dim.dim_label }}</span>
            <div class="rank-track"
              ><i :style="{ width: rankWidth(dim[result.metric]), background: dimColor(index) }"></i
            ></div>
            <span class="rank-num">
              <b>{{ dim[result.metric].toLocaleString() }}</b>
              <small>
                {{ shareOf(dim[result.metric], result.summary[result.metric]) }} ·
                {{ result.metric === "total" ? "计费条" : "消息数" }}
                {{ (result.metric === "total" ? dim.total_segments : dim.total).toLocaleString() }}
              </small>
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
          <div
            ><strong>明细 · 周期 × {{ dimLabel }}</strong
            ><small>点击列头排序</small></div
          >
          <span>共 {{ result.total }} 行</span>
        </div>
      </template>
      <el-table
        :data="pagedItems"
        class="report-table"
        :loading="loading"
        :row-key="reportRowKey"
        :default-sort="{ prop: 'period_start', order: 'descending' }"
        @sort-change="onSortChange"
      >
        <el-table-column prop="period_start" label="周期" width="120" sortable="custom" />
        <el-table-column prop="dim_label" :label="dimLabel" min-width="140" />
        <el-table-column prop="total" label="消息数" width="100" align="right" sortable="custom"
          ><template #default="{ row }">{{ row.total.toLocaleString() }}</template></el-table-column
        >
        <el-table-column prop="total_segments" label="计费条" width="100" align="right" sortable="custom"
          ><template #default="{ row }">{{ row.total_segments.toLocaleString() }}</template></el-table-column
        >
        <el-table-column prop="delivered" label="送达" width="90" align="right"
          ><template #default="{ row }">{{ row.delivered.toLocaleString() }}</template></el-table-column
        >
        <el-table-column prop="failed" label="失败" width="80" align="right"
          ><template #default="{ row }">{{ row.failed.toLocaleString() }}</template></el-table-column
        >
        <el-table-column prop="unknown" label="未知" width="80" align="right"
          ><template #default="{ row }">{{ row.unknown.toLocaleString() }}</template></el-table-column
        >
        <el-table-column prop="success_rate" label="成功率" width="110" align="right" sortable="custom"
          ><template #default="{ row }"
            ><span class="rate-chip" :class="rateClass(row.success_rate)">{{
              formatRate(row.success_rate)
            }}</span></template
          ></el-table-column
        >
      </el-table>
      <ListPagination
        v-if="result.total > pageSize"
        v-model:page="page"
        :total="result.total"
        :page-size="pageSize"
        :show-count="false"
        class="report-pager"
        @change="changePage(page)"
      ></ListPagination>
      <div class="report-mobile-list"
        ><article v-for="item in pagedItems" :key="`${item.period_start}-${item.dim_value}`"
          ><header
            ><time>{{ item.period_start }}</time
            ><strong>{{ item.dim_label }}</strong></header
          ><dl
            ><div
              ><dt>消息数</dt><dd>{{ item.total }}</dd></div
            ><div
              ><dt>计费条</dt><dd>{{ item.total_segments }}</dd></div
            ><div
              ><dt>成功率</dt><dd>{{ formatRate(item.success_rate) }}</dd></div
            ></dl
          ><p>送达 {{ item.delivered }} · 失败 {{ item.failed }} · 未知 {{ item.unknown }}</p></article
        ></div
      >
    </el-card>
  </template>
</template>
