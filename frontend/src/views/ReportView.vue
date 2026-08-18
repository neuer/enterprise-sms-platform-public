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
} from "../api/reports"
import ReportTrendChart from "../components/ReportTrendChart.vue"
import EmptyState from "../components/EmptyState.vue"
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
let pollTimer: number | undefined

const canDecrypt = computed(() => result.value?.can_export_decrypted === true)
const filters = computed<ReportFilters>(() => ({
  granularity: granularity.value,
  groupBy: groupBy.value,
  category: category.value,
  start: dateRange.value[0],
  end: dateRange.value[1],
}))

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
const statusLabel: Record<ExportTask["status"], string> = {
  pending: "等待生成", running: "生成中", done: "已完成", failed: "生成失败",
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    result.value = await getReport(filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "报表加载失败"
  } finally {
    loading.value = false
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
    <div><p class="eyebrow">ANALYTICS / 统计报表</p><h1>统计报表</h1><p>日、周、月多维聚合，消息数与计费条使用同一事实源。</p></div>
    <div class="report-export-actions">
      <el-checkbox v-if="canDecrypt" v-model="decrypted">含明文手机号</el-checkbox>
      <el-button :loading="exportLoading" @click="createExport">导出明细 CSV</el-button>
    </div>
  </section>

  <el-card shadow="never" class="report-filter-card">
    <el-form class="report-filter filter-grid" label-position="top" @submit.prevent="load">
      <el-form-item class="filter-span-3" label="统计周期"><el-segmented v-model="granularity" :options="granularityOptions" /></el-form-item>
      <el-form-item class="filter-span-2" label="分组维度"><el-segmented v-model="groupBy" :options="groupByOptions" /></el-form-item>
      <el-form-item class="filter-span-2" label="消息类别"><el-select v-model="category"><el-option label="全部类别" value="all" /><el-option label="验证码" value="verify" /><el-option label="通知" value="notice" /><el-option label="营销" value="market" /></el-select></el-form-item>
      <el-form-item class="filter-span-3" label="日期范围"><el-date-picker v-model="dateRange" type="daterange" popper-class="qingluan-date-popper" value-format="YYYY-MM-DD" range-separator="至" start-placeholder="开始日期" end-placeholder="结束日期" /></el-form-item>
      <el-form-item class="filter-actions filter-span-2"><el-button type="primary" native-type="submit" :loading="loading">查询</el-button></el-form-item>
    </el-form>
  </el-card>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="report-error"><template #default><el-button link type="primary" @click="load">重新查询</el-button></template></el-alert>

  <template v-if="result">
    <section class="report-summary">
      <el-card shadow="never"><span>消息数</span><strong>{{ result.summary.total.toLocaleString() }}</strong><small>{{ result.start }} — {{ result.end }}</small></el-card>
      <el-card shadow="never"><span>计费条</span><strong>{{ result.summary.total_segments.toLocaleString() }}</strong><small>最终内容计费口径</small></el-card>
      <el-card shadow="never"><span>送达成功率</span><strong>{{ (result.summary.success_rate * 100).toFixed(1) }}%</strong><small>unknown / other 不入分母</small></el-card>
    </section>

    <el-card shadow="never" class="report-chart-card">
      <template #header><div class="panel-title"><div><strong>双指标趋势</strong><small>{{ granularityLabel[result.granularity] }}粒度 · {{ result.group_by === 'app' ? '应用' : '部门' }}维度</small></div><span>{{ result.items.length }} 个数据点</span></div></template>
      <ReportTrendChart v-if="result.items.length" :items="result.items" /><EmptyState v-else title="当前条件没有统计数据" description="调整日期、类别或分组方式后重新查询。" />
    </el-card>

    <el-card shadow="never" class="report-table-card">
      <el-table :data="result.items" class="report-table">
        <el-table-column prop="period_start" label="周期起始" width="130" />
        <el-table-column prop="dim_label" :label="result.group_by === 'app' ? '应用' : '部门'" min-width="150" />
        <el-table-column prop="total" label="消息数" width="110" align="right" />
        <el-table-column prop="total_segments" label="计费条" width="110" align="right" />
        <el-table-column label="送达 / 失败 / 未知" min-width="170"><template #default="{ row }">{{ row.delivered }} / {{ row.failed }} / {{ row.unknown }}</template></el-table-column>
        <el-table-column label="成功率" width="100" align="right"><template #default="{ row }">{{ (row.success_rate * 100).toFixed(1) }}%</template></el-table-column>
      </el-table>
      <div class="report-mobile-list"><article v-for="item in result.items" :key="`${item.period_start}-${item.dim_value}`"><header><time>{{ item.period_start }}</time><strong>{{ item.dim_label }}</strong></header><dl><div><dt>消息数</dt><dd>{{ item.total }}</dd></div><div><dt>计费条</dt><dd>{{ item.total_segments }}</dd></div><div><dt>成功率</dt><dd>{{ (item.success_rate * 100).toFixed(1) }}%</dd></div></dl><p>送达 {{ item.delivered }} · 失败 {{ item.failed }} · 未知 {{ item.unknown }}</p></article></div>
    </el-card>
  </template>

  <el-card v-if="exportTask || exportError" shadow="never" class="export-status-card">
    <div v-if="exportTask"><span>导出任务 #{{ exportTask.id }}</span><el-tag :type="exportTask.status === 'done' ? 'success' : exportTask.status === 'failed' ? 'danger' : 'warning'">{{ statusLabel[exportTask.status] }}</el-tag><strong v-if="exportTask.row_count !== null">{{ exportTask.row_count }} 行</strong><small v-if="exportTask.expires_at">保留至 {{ exportTask.expires_at.slice(0, 10) }}</small><el-button v-if="exportTask.download_url" type="primary" @click="download">下载 CSV</el-button></div>
    <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" />
  </el-card>
</template>
