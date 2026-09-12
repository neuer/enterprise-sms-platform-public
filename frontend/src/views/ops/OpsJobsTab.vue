<script setup lang="ts">
import { ElMessage } from "element-plus"

import { ref, watch } from "vue"

import { listJobs, triggerJob, type JobItem } from "../../api/ops"

import { useConfirmActions } from "../../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()

import { errorText } from "../../lib/error"

import { useLatestRead } from "../../composables/useLatestRead"

import { jobDescription } from "../../lib/jobDescriptions"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

const props = defineProps<{ active: boolean }>()

const jobs = ref<JobItem[]>([])

const JOBS_EMPTY = {
  title: "暂无任务心跳记录",
  description: "心跳由 API 进程内巡检汇总，任务须以 tracked_job 声明预期间隔。",
}

const snapshotRead = useLatestRead()

const snapshotLoading = ref(false)

const snapshotError = ref("")
const loading = snapshotLoading
const errorMessage = snapshotError
async function load(_tab?: string): Promise<void> {
  if (!props.active) return
  const signal = snapshotRead.start()
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listJobs(signal)
    if (!signal.aborted) jobs.value = result
  } catch (error) {
    if (!signal.aborted) errorMessage.value = errorText(error, "运维数据加载失败")
  } finally {
    if (!signal.aborted) loading.value = false
  }
}

async function trigger(item: JobItem): Promise<void> {
  item = { ...item }
  if (
    !(await confirmAuditedAction({
      title: "确认任务触发",
      body: `手动触发 ${item.job_name} 将立即投递一次执行，不改变 beat 既有调度。`,
      auditNote: "触发行为与操作人将写入审计日志。",
      confirmText: "手动触发",
    }))
  )
    return
  try {
    await triggerJob(item.job_name)
    ElMessage.success("任务已投递 · 本次操作已记入审计")
  } catch (error) {
    ElMessage.error(errorText(error, "任务触发失败"))
  }
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      snapshotRead.cancel()
      snapshotLoading.value = false
    }
  },
  { immediate: true },
)
</script>
<template>
  <div>
    <el-alert v-if="errorMessage" class="ops-alert" :title="errorMessage" type="error" :closable="false"
      ><template #default><el-button link type="primary" @click="load()">重新加载</el-button></template></el-alert
    >
    <section id="ops-panel-jobs" v-loading="loading" class="ops-panel" role="tabpanel" aria-labelledby="ops-tab-jobs">
      <header class="ops-panel-title"
        ><div
          ><strong>后台任务心跳</strong
          ><small>预期间隔由 beat 与 API 启动时读取，修改后需重启两个容器 · 共 {{ jobs.length }} 项</small></div
        ></header
      >
      <section class="ops-results">
        <el-table :data="jobs" row-key="job_name" class="ops-table"
          ><el-table-column prop="job_name" label="任务" min-width="180" /><el-table-column
            label="中文用途"
            min-width="270"
            ><template #default="{ row }"
              ><span class="job-description">{{ jobDescription(row.job_name) }}</span></template
            ></el-table-column
          ><el-table-column label="健康" width="100"
            ><template #default="{ row }"
              ><span class="job-health" :class="{ danger: row.stalled || row.last_status === 'failed' }"
                ><i></i>{{ row.stalled ? "stalled" : row.last_status || "无记录" }}</span
              ></template
            ></el-table-column
          ><el-table-column prop="last_duration_ms" label="耗时 ms" width="100" /><el-table-column
            prop="last_items"
            label="处理量"
            width="90" /><el-table-column label="24h 成功率" width="120"
            ><template #default="{ row }">{{
              row.last_run_at ? (row.success_rate_24h * 100).toFixed(1) + "%" : "—"
            }}</template></el-table-column
          ><el-table-column label="最近运行" width="180"
            ><template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template></el-table-column
          ><el-table-column label="操作" width="110"
            ><template #default="{ row }"
              ><el-button link type="primary" @click="trigger(row)">手动触发</el-button></template
            ></el-table-column
          ><template #empty><EmptyState :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in jobs" :key="item.job_name"
            ><header
              ><strong>{{ item.job_name }}</strong
              ><span class="job-health" :class="{ danger: item.stalled }"
                ><i></i>{{ item.stalled ? "stalled" : item.last_status || "无记录" }}</span
              ></header
            ><p class="job-description">{{ jobDescription(item.job_name) }}</p
            ><p
              >{{ item.last_items }} 项 · {{ item.last_duration_ms ?? 0 }}ms ·
              {{ item.last_run_at ? (item.success_rate_24h * 100).toFixed(1) + "%" : "—" }}</p
            ><el-button link type="primary" @click="trigger(item)">手动触发</el-button></article
          ><EmptyState v-if="!jobs.length" :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description"
        /></div>
      </section>
    </section>
  </div>
</template>
