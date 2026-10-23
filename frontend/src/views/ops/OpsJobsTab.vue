<script setup lang="ts">
import { ElMessage } from "element-plus"

import { nextTick, onScopeDispose, ref, watch } from "vue"

import { listJobs, triggerJob, type JobItem } from "../../api/ops"

import { defaultSessionDocument, type SessionOperationOrigin } from "../../api/sessionDocument"

import { useConfirmActions } from "../../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()

import { errorText } from "../../lib/error"

import { formatPercent } from "../../lib/format"

import { useLatestRead } from "../../composables/useLatestRead"

import { jobDescription } from "../../lib/jobDescriptions"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

import LoadErrorAlert from "../../components/LoadErrorAlert.vue"

const props = defineProps<{ active: boolean }>()

const jobs = ref<JobItem[]>([])

const JOBS_EMPTY = {
  title: "暂无任务心跳记录",
  description: "心跳由 API 进程内巡检汇总，任务须以 tracked_job 声明预期间隔。",
}

/** 健康列文案：停滞优先于最近一次结果；last_status 是后端原始英文枚举，展示前必须中文化。 */
function healthLabel(row: JobItem): string {
  if (row.stalled) return "停滞"
  if (row.last_status === "success") return "成功"
  if (row.last_status === "failed") return "失败"
  return "无记录"
}

const snapshotRead = useLatestRead()

const snapshotLoading = ref(false)

const snapshotError = ref("")
const loading = snapshotLoading
const errorMessage = snapshotError
// 确认待决与写请求分开：确认阶段只防重复提交，不把触发按钮提前变成不可聚焦的 loading。
const confirmingJobName = ref<string | null>(null)
const writingJobName = ref<string | null>(null)
let pageMounted = true
onScopeDispose(() => {
  pageMounted = false
})

function triggerElement(event?: Event): HTMLElement | null {
  const current = event?.currentTarget
  if (current instanceof HTMLElement) return current.closest("button") ?? current
  const target = event?.target
  if (target instanceof Element) {
    const button = target.closest("button")
    if (button instanceof HTMLElement) return button
  }
  const active = document.activeElement
  if (active instanceof HTMLElement) {
    const button = active.closest("button")
    if (button instanceof HTMLElement) return button
  }
  return null
}

function triggerUsable(element: HTMLElement | null): element is HTMLElement {
  if (!element?.isConnected) return false
  if (element.closest("[inert]")) return false
  let node: HTMLElement | null = element
  while (node) {
    if (node.hidden || node.style.display === "none") return false
    node = node.parentElement
  }
  return !element.hasAttribute("disabled") && element.getAttribute("aria-disabled") !== "true"
}

/** 当前页的安全入口。会话或页面已经换掉时，不把焦点拉回旧的任务按钮。 */
function focusCurrentPage(): void {
  const heading = document.querySelector<HTMLElement>("main h1, .workspace h1, .page-heading h1, h1")
  const target = heading?.isConnected ? heading : document.querySelector<HTMLElement>("main, .workspace")
  if (!target?.isConnected) return
  if (target.tabIndex < 0) target.tabIndex = -1
  target.focus()
}

async function restoreTriggerFocus(
  element: HTMLElement | null,
  origin: SessionOperationOrigin,
  pageCurrent: () => boolean,
): Promise<void> {
  await nextTick()
  await new Promise<void>((resolve) => {
    window.setTimeout(resolve, 0)
  })
  const started = performance.now()
  while (performance.now() - started < 400) {
    const open = [...document.querySelectorAll<HTMLElement>(".el-overlay.is-message-box")].some(
      (overlay) => overlay.style.display !== "none" && overlay.getAttribute("aria-hidden") !== "true",
    )
    if (!open) break
    await new Promise<void>((resolve) => {
      const timer = window.setTimeout(resolve, 32)
      window.requestAnimationFrame(() => {
        window.clearTimeout(timer)
        resolve()
      })
    })
  }
  // 关闭动画结束后再落焦点，避免被确认框的焦点锁盖掉。
  await new Promise<void>((resolve) => {
    window.setTimeout(resolve, 0)
  })
  if (pageCurrent() && defaultSessionDocument.isOriginCurrent(origin) && triggerUsable(element)) {
    element.focus()
    return
  }
  focusCurrentPage()
}
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

async function trigger(item: JobItem, event?: Event): Promise<void> {
  item = { ...item }
  if (confirmingJobName.value || writingJobName.value) return
  const triggerEl = triggerElement(event)
  const origin = defaultSessionDocument.captureOrigin()
  confirmingJobName.value = item.job_name
  let confirmed = false
  try {
    confirmed = await confirmAuditedAction({
      title: "确认任务触发",
      body: `手动触发 ${item.job_name} 将立即投递一次执行，不改变 beat 既有调度。`,
      auditNote: "触发行为与操作人将写入审计日志。",
      confirmText: "确认触发",
    })
  } catch {
    confirmed = false
  } finally {
    confirmingJobName.value = null
  }
  if (!confirmed) {
    await restoreTriggerFocus(triggerEl, origin, () => pageMounted && props.active)
    return
  }
  if (!pageMounted || !props.active || !defaultSessionDocument.isOriginCurrent(origin)) return
  writingJobName.value = item.job_name
  try {
    await triggerJob(item.job_name)
    ElMessage.success("任务已投递 · 本次操作已记入审计")
    // 触发后重查心跳：让操作者立刻看到 last_status/耗时变化，而不是停留在旧快照。
    await load("jobs")
  } catch (error) {
    ElMessage.error(errorText(error, "任务触发失败"))
  } finally {
    writingJobName.value = null
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
    <LoadErrorAlert class="ops-alert" :message="errorMessage" @retry="load()" />
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
                ><i></i>{{ healthLabel(row) }}</span
              ></template
            ></el-table-column
          ><el-table-column prop="last_duration_ms" label="耗时 ms" width="100" /><el-table-column
            prop="last_items"
            label="处理量"
            width="90" /><el-table-column label="24h 成功率" width="120"
            ><template #default="{ row }">{{
              row.last_run_at ? formatPercent(row.success_rate_24h) : "—"
            }}</template></el-table-column
          ><el-table-column label="最近运行" width="180"
            ><template #default="{ row }">{{ formatDateTime(row.last_run_at) }}</template></el-table-column
          ><el-table-column label="操作" width="110"
            ><template #default="{ row }"
              ><el-button
                link
                type="primary"
                :loading="writingJobName === row.job_name"
                :disabled="
                  (Boolean(confirmingJobName) && confirmingJobName !== row.job_name) ||
                  (Boolean(writingJobName) && writingJobName !== row.job_name)
                "
                :data-testid="`job-trigger-${row.job_name}`"
                @click="trigger(row, $event)"
                >手动触发</el-button
              ></template
            ></el-table-column
          ><template #empty><EmptyState :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in jobs" :key="item.job_name"
            ><header
              ><strong>{{ item.job_name }}</strong
              ><span class="job-health" :class="{ danger: item.stalled }"><i></i>{{ healthLabel(item) }}</span></header
            ><p class="job-description">{{ jobDescription(item.job_name) }}</p
            ><p
              >{{ item.last_items }} 项 · {{ item.last_duration_ms ?? 0 }}ms ·
              {{ item.last_run_at ? formatPercent(item.success_rate_24h) : "—" }}</p
            ><el-button
              link
              type="primary"
              :loading="writingJobName === item.job_name"
              :disabled="
                (Boolean(confirmingJobName) && confirmingJobName !== item.job_name) ||
                (Boolean(writingJobName) && writingJobName !== item.job_name)
              "
              :data-testid="`job-trigger-mobile-${item.job_name}`"
              @click="trigger(item, $event)"
              >手动触发</el-button
            ></article
          ><EmptyState v-if="!loading && !jobs.length" :title="JOBS_EMPTY.title" :description="JOBS_EMPTY.description"
        /></div>
      </section>
    </section>
  </div>
</template>
