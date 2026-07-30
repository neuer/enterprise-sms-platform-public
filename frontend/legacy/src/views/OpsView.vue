<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onBeforeUnmount, onMounted, ref, watch } from "vue"

import {
  createUnmatchedExport,
  getQueueStatus,
  listAlerts,
  listJobs,
  listRawLogs,
  listUncertain,
  listUnmatched,
  replayRaw,
  resumeQueue,
  triggerJob,
  type AlertItem,
  type JobItem,
  type QueueStatus,
  type RawLogItem,
  type UncertainItem,
  type UnmatchedItem,
} from "../api/ops"
import {
  downloadExport,
  getExportTask,
  issueExportStepUp,
  type ExportTask,
} from "../api/reports"
import CallbackView from "./CallbackView.vue"

type TabName = "alerts" | "callbacks" | "raw" | "uncertain" | "unmatched" | "jobs" | "queue"
const activeTab = ref<TabName>("alerts")
const loading = ref(false)
const errorMessage = ref("")
const alerts = ref<AlertItem[]>([])
const alertPage = ref(1)
const alertTotal = ref(0)
const alertType = ref("")
const alertLevel = ref<"" | AlertItem["level"]>("")
const alertRange = ref<[Date, Date] | null>(null)
const rawLogs = ref<RawLogItem[]>([])
const rawPage = ref(1)
const rawTotal = ref(0)
const rawSource = ref<"" | RawLogItem["source"]>("")
const rawProcessed = ref<"" | "true" | "false">("")
const uncertain = ref<UncertainItem[]>([])
const uncertainPage = ref(1)
const uncertainTotal = ref(0)
const unmatched = ref<UnmatchedItem[]>([])
const unmatchedPage = ref(1)
const unmatchedTotal = ref(0)
const jobs = ref<JobItem[]>([])
const queue = ref<QueueStatus | null>(null)
const unmatchedPhone = ref("")
const unmatchedRange = ref<[Date, Date] | null>(null)
const exportDecrypted = ref(false)
const exportTask = ref<ExportTask | null>(null)
const exportBusy = ref(false)
const exportError = ref("")
const forceResume = ref(false)
let exportPollTimer: number | undefined

function rangeValues(range: [Date, Date] | null): { start?: string; end?: string } {
  if (!range) return {}
  return { start: range[0].toISOString(), end: range[1].toISOString() }
}

function time(value: string | null): string {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

function duration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

async function load(tab: TabName = activeTab.value): Promise<void> {
  if (tab === "callbacks") return
  loading.value = true
  errorMessage.value = ""
  try {
    if (tab === "alerts") {
      const result = await listAlerts({
        page: alertPage.value,
        alertType: alertType.value,
        level: alertLevel.value || undefined,
        ...rangeValues(alertRange.value),
      })
      alerts.value = result.items
      alertPage.value = result.page
      alertTotal.value = result.total
    }
    if (tab === "raw") {
      const result = await listRawLogs({
        page: rawPage.value,
        source: rawSource.value || undefined,
        processed: rawProcessed.value === "" ? undefined : rawProcessed.value === "true",
      })
      rawLogs.value = result.items
      rawPage.value = result.page
      rawTotal.value = result.total
    }
    if (tab === "uncertain") {
      const result = await listUncertain({ page: uncertainPage.value })
      uncertain.value = result.items
      uncertainPage.value = result.page
      uncertainTotal.value = result.total
    }
    if (tab === "unmatched") {
      const result = await listUnmatched({
        page: unmatchedPage.value,
        phone: unmatchedPhone.value,
        ...rangeValues(unmatchedRange.value),
      })
      unmatched.value = result.items
      unmatchedPage.value = result.page
      unmatchedTotal.value = result.total
    }
    if (tab === "jobs") jobs.value = await listJobs()
    if (tab === "queue") queue.value = await getQueueStatus()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "运维数据加载失败"
  } finally {
    loading.value = false
  }
}

function reloadFromFirstPage(tab: "alerts" | "raw" | "unmatched"): void {
  if (tab === "alerts") alertPage.value = 1
  if (tab === "raw") rawPage.value = 1
  if (tab === "unmatched") unmatchedPage.value = 1
  void load(tab)
}

async function replay(item: RawLogItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`重放 raw #${item.id}，仅允许未处理报文。`, "确认报文重放", { type: "warning", confirmButtonText: "确认重放" })
    const result = await replayRaw(item.id)
    ElMessage.success(`重放完成，处理 ${result.processed_items} 项`)
    await load("raw")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "重放失败")
  }
}

async function trigger(item: JobItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`手动触发 ${item.job_name}，本次请求将写入审计。`, "确认任务触发", { type: "warning", confirmButtonText: "手动触发" })
    await triggerJob(item.job_name)
    ElMessage.success("任务已投递")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "任务触发失败")
  }
}

async function exportUnmatched(): Promise<void> {
  exportBusy.value = true
  exportError.value = ""
  try {
    exportTask.value = await createUnmatchedExport({
      phone: unmatchedPhone.value,
      ...rangeValues(unmatchedRange.value),
    }, exportDecrypted.value)
    ElMessage.success("对账导出任务已创建")
    await refreshExport()
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出创建失败"
    ElMessage.error(exportError.value)
  } finally {
    exportBusy.value = false
  }
}

function stopExportPolling(): void {
  if (exportPollTimer !== undefined) window.clearTimeout(exportPollTimer)
  exportPollTimer = undefined
}

function scheduleExportPolling(): void {
  stopExportPolling()
  exportPollTimer = window.setTimeout(() => void refreshExport(), 2_000)
}

async function refreshExport(): Promise<void> {
  if (!exportTask.value) return
  try {
    exportTask.value = await getExportTask(exportTask.value.id)
    if (exportTask.value.status === "pending" || exportTask.value.status === "running") {
      scheduleExportPolling()
    } else {
      stopExportPolling()
    }
  } catch (error) {
    exportError.value = error instanceof Error ? error.message : "导出状态查询失败"
    stopExportPolling()
  }
}

async function downloadUnmatchedExport(): Promise<void> {
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
    anchor.download = `unmatched-reports-${exportTask.value.id}.csv`
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

async function recover(): Promise<void> {
  try {
    const warning = forceResume.value ? "force 将绕过余额和熔断原因检查。" : "仅在余额达标且原因为 999 时恢复。"
    await ElMessageBox.confirm(warning, "确认恢复双队列", { type: "warning", confirmButtonText: "恢复队列" })
    const result = await resumeQueue(forceResume.value)
    ElMessage.success(`已恢复 ${result.resumed_batches} 个批次`)
    await load("queue")
  } catch (error) {
    if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "队列恢复失败")
  }
}

watch(activeTab, (value) => void load(value))
onMounted(() => void load())
onBeforeUnmount(stopExportPolling)
</script>

<template>
  <section class="page-heading ops-heading">
    <div><p class="eyebrow">CONTROL ROOM / 运维控制室</p><h1>运维中心</h1><p>所有恢复动作均有守卫与审计；原始报文只展示无 PII 元数据。</p></div>
    <span class="ops-mode"><i></i> 审计在线 · 安全元数据</span>
  </section>

  <el-card shadow="never" class="ops-workbench">
    <el-tabs v-model="activeTab" class="ops-tabs">
      <el-tab-pane label="告警记录" name="alerts" />
      <el-tab-pane label="回调任务" name="callbacks" lazy />
      <el-tab-pane label="原始报文" name="raw" lazy />
      <el-tab-pane label="uncertain" name="uncertain" lazy />
      <el-tab-pane label="unmatched" name="unmatched" lazy />
      <el-tab-pane label="任务健康" name="jobs" lazy />
      <el-tab-pane label="队列恢复" name="queue" lazy />
    </el-tabs>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false"><template #default><el-button link type="primary" @click="load()">重新加载</el-button></template></el-alert>

    <section v-if="activeTab === 'alerts'" v-loading="loading" class="ops-panel">
      <header class="ops-panel-title ops-filter-title"><div><strong>告警事实流</strong><small>渠道为空时仅写 alert_log + 日志 · 共 {{ alertTotal }} 条</small></div><div class="filter-toolbar ops-query-filters"><el-input v-model="alertType" clearable placeholder="告警类型" /><el-select v-model="alertLevel" clearable placeholder="等级"><el-option label="info" value="info" /><el-option label="warn" value="warn" /><el-option label="crit" value="crit" /></el-select><el-date-picker v-model="alertRange" type="datetimerange" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间" /><el-button type="primary" @click="reloadFromFirstPage('alerts')">查询</el-button><el-button @click="alertType = ''; alertLevel = ''; alertRange = null; reloadFromFirstPage('alerts')">重置</el-button></div></header>
      <el-table :data="alerts" class="ops-table"><el-table-column label="等级" width="88"><template #default="{ row }"><el-tag :type="row.level === 'crit' ? 'danger' : row.level === 'warn' ? 'warning' : 'info'" :effect="row.level === 'crit' ? 'dark' : 'plain'">{{ row.level }}</el-tag></template></el-table-column><el-table-column prop="title" label="告警" min-width="220" /><el-table-column prop="alert_type" label="类型" min-width="150" /><el-table-column prop="channels" label="渠道" width="110" /><el-table-column label="时间" width="180"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column></el-table>
      <div class="ops-mobile-list"><article v-for="item in alerts" :key="item.id"><header><el-tag :type="item.level === 'crit' ? 'danger' : 'warning'">{{ item.level }}</el-tag><time>{{ time(item.created_at) }}</time></header><strong>{{ item.title }}</strong><p>{{ item.alert_type }} · {{ item.channels }}</p></article></div>
      <el-pagination v-if="alertTotal > 20" v-model:current-page="alertPage" data-testid="ops-alert-pagination" class="ops-pagination" :page-size="20" :total="alertTotal" layout="prev, pager, next, total" background @current-change="load('alerts')" />
    </section>

    <CallbackView v-else-if="activeTab === 'callbacks'" embedded />

    <section v-else-if="activeTab === 'raw'" v-loading="loading" class="ops-panel">
      <header class="ops-panel-title ops-filter-title"><div><strong>原始报文保险箱</strong><small>密文载荷与完整性摘要不对外返回 · 共 {{ rawTotal }} 条</small></div><div class="filter-toolbar ops-query-filters"><el-select v-model="rawSource" clearable placeholder="来源"><el-option label="报告" value="report" /><el-option label="回复" value="reply" /></el-select><el-select v-model="rawProcessed" clearable placeholder="处理状态"><el-option label="待重放" value="false" /><el-option label="已处理" value="true" /></el-select><el-button type="primary" @click="reloadFromFirstPage('raw')">查询</el-button><el-button @click="rawSource = ''; rawProcessed = ''; reloadFromFirstPage('raw')">重置</el-button></div></header>
      <el-table :data="rawLogs" class="ops-table"><el-table-column prop="id" label="RAW" width="80" /><el-table-column prop="source" label="来源" width="100" /><el-table-column label="记录 / customId" min-width="150"><template #default="{ row }">{{ row.item_count }} / {{ row.custom_id_count }}</template></el-table-column><el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="row.processed ? 'success' : 'danger'">{{ row.processed ? '已处理' : '待重放' }}</el-tag></template></el-table-column><el-table-column prop="error" label="错误摘要" min-width="180" /><el-table-column label="时间" width="180"><template #default="{ row }">{{ time(row.fetched_at) }}</template></el-table-column><el-table-column label="操作" width="90"><template #default="{ row }"><el-button v-if="!row.processed" link type="danger" @click="replay(row)">重放</el-button></template></el-table-column></el-table>
      <div class="ops-mobile-list"><article v-for="item in rawLogs" :key="item.id"><header><strong>RAW-{{ item.id }} · {{ item.source }}</strong><el-tag :type="item.processed ? 'success' : 'danger'">{{ item.processed ? '已处理' : '待重放' }}</el-tag></header><p>{{ item.item_count }} 项 · {{ item.custom_id_count }} customId</p><small>{{ item.error || time(item.fetched_at) }}</small><el-button v-if="!item.processed" link type="danger" @click="replay(item)">重放</el-button></article></div>
      <el-pagination v-if="rawTotal > 20" v-model:current-page="rawPage" data-testid="ops-raw-pagination" class="ops-pagination" :page-size="20" :total="rawTotal" layout="prev, pager, next, total" background @current-change="load('raw')" />
    </section>

    <section v-else-if="activeTab === 'uncertain'" v-loading="loading" class="ops-panel">
      <header class="ops-panel-title"><div><strong>结果未知分片</strong><small>只读核查；仅 reconcile 可迁移状态</small></div><span>{{ uncertainTotal }} 项</span></header>
      <el-table :data="uncertain" class="ops-table"><el-table-column prop="batch_no" label="批次" min-width="180" /><el-table-column prop="custom_id" label="customId" min-width="180" /><el-table-column prop="phone_count" label="号码数" width="90" /><el-table-column label="停留" width="120"><template #default="{ row }"><el-tag :type="row.age_seconds >= 86400 ? 'danger' : 'warning'">{{ duration(row.age_seconds) }}</el-tag></template></el-table-column><el-table-column label="进入时间" width="180"><template #default="{ row }">{{ time(row.uncertain_since) }}</template></el-table-column></el-table>
      <div class="ops-mobile-list"><article v-for="item in uncertain" :key="item.chunk_id"><header><strong>{{ item.batch_no }}</strong><el-tag type="danger">{{ duration(item.age_seconds) }}</el-tag></header><code>{{ item.custom_id }}</code><p>{{ item.phone_count }} 个号码 · 禁止自动重发</p></article></div>
      <el-pagination v-if="uncertainTotal > 20" v-model:current-page="uncertainPage" data-testid="ops-uncertain-pagination" class="ops-pagination" :page-size="20" :total="uncertainTotal" layout="prev, pager, next, total" background @current-change="load('uncertain')" />
    </section>

    <section v-else-if="activeTab === 'unmatched'" v-loading="loading" class="ops-panel">
      <header class="ops-panel-title ops-filter-title"><div><strong>迁移期无主报告</strong><small>手机号精确查询只在内存转换 HMAC · 共 {{ unmatchedTotal }} 条</small></div><div class="filter-toolbar ops-query-filters"><el-input v-model="unmatchedPhone" maxlength="11" clearable placeholder="手机号精确查询" /><el-date-picker v-model="unmatchedRange" type="datetimerange" range-separator="至" start-placeholder="开始时间" end-placeholder="结束时间" /><el-button @click="reloadFromFirstPage('unmatched')">查询</el-button><el-checkbox v-model="exportDecrypted">授权明文</el-checkbox><el-button type="primary" :loading="exportBusy" @click="exportUnmatched">导出对账</el-button></div></header>
      <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" />
      <el-alert v-if="exportTask" :title="`导出任务 #${exportTask.id} · ${exportTask.status}`" :type="exportTask.status === 'failed' ? 'error' : 'success'" :closable="false"><template #default><div class="export-task-detail"><span v-if="exportTask.row_count !== null">{{ exportTask.row_count }} 行</span><span v-if="exportTask.expires_at">有效期至 {{ time(exportTask.expires_at) }}</span><el-button v-if="exportTask.status === 'done' && exportTask.download_url" data-testid="download-unmatched-export" type="primary" link @click="downloadUnmatchedExport">下载 CSV</el-button></div></template></el-alert>
      <el-table :data="unmatched" class="ops-table"><el-table-column prop="phone_mask" label="号码" width="140" /><el-table-column prop="custom_id" label="customId" min-width="170" /><el-table-column prop="vendor_task_id" label="厂商任务" min-width="150" /><el-table-column prop="report_desc" label="结果" width="120" /><el-table-column label="报告时间" width="180"><template #default="{ row }">{{ time(row.report_time) }}</template></el-table-column></el-table>
      <div class="ops-mobile-list"><article v-for="item in unmatched" :key="item.id"><header><strong>{{ item.phone_mask }}</strong><el-tag type="warning">unmatched</el-tag></header><code>{{ item.custom_id || '—' }}</code><p>{{ item.report_desc || '未知结果' }} · {{ time(item.report_time) }}</p></article></div>
      <el-pagination v-if="unmatchedTotal > 20" v-model:current-page="unmatchedPage" data-testid="ops-unmatched-pagination" class="ops-pagination" :page-size="20" :total="unmatchedTotal" layout="prev, pager, next, total" background @current-change="load('unmatched')" />
    </section>

    <section v-else-if="activeTab === 'jobs'" v-loading="loading" class="ops-panel">
      <header class="ops-panel-title"><div><strong>后台任务心跳</strong><small>预期间隔由 beat 与 API 启动时读取，修改后需重启两个容器</small></div><span>{{ jobs.length }} 项</span></header>
      <el-table :data="jobs" class="ops-table"><el-table-column prop="job_name" label="任务" min-width="180" /><el-table-column label="健康" width="100"><template #default="{ row }"><span class="job-health" :class="{ danger: row.stalled || row.last_status === 'failed' }"><i></i>{{ row.stalled ? 'stalled' : row.last_status || '无记录' }}</span></template></el-table-column><el-table-column prop="last_duration_ms" label="耗时 ms" width="100" /><el-table-column prop="last_items" label="处理量" width="90" /><el-table-column label="24h 成功率" width="120"><template #default="{ row }">{{ (row.success_rate_24h * 100).toFixed(1) }}%</template></el-table-column><el-table-column label="最近运行" width="180"><template #default="{ row }">{{ time(row.last_run_at) }}</template></el-table-column><el-table-column label="操作" width="110"><template #default="{ row }"><el-button link type="primary" @click="trigger(row)">手动触发</el-button></template></el-table-column></el-table>
      <div class="ops-mobile-list"><article v-for="item in jobs" :key="item.job_name"><header><strong>{{ item.job_name }}</strong><span class="job-health" :class="{ danger: item.stalled }"><i></i>{{ item.stalled ? 'stalled' : item.last_status || '无记录' }}</span></header><p>{{ item.last_items }} 项 · {{ item.last_duration_ms ?? 0 }}ms · {{ (item.success_rate_24h * 100).toFixed(1) }}%</p><el-button link type="primary" @click="trigger(item)">手动触发</el-button></article></div>
    </section>

    <section v-else-if="activeTab === 'queue'" v-loading="loading" class="ops-panel queue-recovery">
      <header class="ops-panel-title"><div><strong>双队列恢复</strong><small>PostgreSQL 状态先恢复，Redis 仅作为投递通道</small></div></header>
      <template v-if="queue"><div class="queue-status-grid"><article><span>REALTIME</span><strong>{{ queue.realtime_code ? `暂停 · ${queue.realtime_code}` : '运行中' }}</strong></article><article><span>BULK</span><strong>{{ queue.bulk_code ? `暂停 · ${queue.bulk_code}` : '运行中' }}</strong></article><article><span>余额</span><strong>{{ queue.balance === null ? '无快照' : `余额 ${queue.balance.toLocaleString()}` }}</strong><small>阈值 {{ queue.threshold.toLocaleString() }}</small></article></div><div class="break-glass"><el-switch v-model="forceResume" data-testid="force-resume" inline-prompt active-text="FORCE" inactive-text="SAFE" /><p>{{ forceResume ? '将绕过余额与暂停原因守卫，操作会写审计。' : '仅余额达到阈值且暂停码为 999 时允许恢复。' }}</p><el-button type="danger" @click="recover">恢复队列</el-button></div></template>
    </section>
  </el-card>
</template>
