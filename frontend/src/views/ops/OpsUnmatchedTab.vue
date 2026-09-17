<script setup lang="ts">
import { rangeToIsoParams } from "../../lib/time"

import { usePagedList } from "../../composables/usePagedList"

import ListPagination from "../../components/ListPagination.vue"

import { ElMessage } from "element-plus"

import { computed, ref, watch } from "vue"

import { createUnmatchedExport, listUnmatched } from "../../api/ops"

import { DEFAULT_PAGE_SIZE } from "../../lib/labels"

import { phoneProblem } from "../../lib/phone"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

import PhoneMask from "../../components/PhoneMask.vue"

import { useExportTask } from "../../composables/useExportTask"

const props = defineProps<{ active: boolean }>()

const unmatchedPhone = ref("")

const unmatchedRange = ref<[Date, Date] | null>(null)

const exportDecrypted = ref(false)

const {
  exportTask,
  exportBusy,
  exportError,
  start: startExportTask,
  download: downloadExportFile,
} = useExportTask({
  timeoutMessage: "导出状态查询超时（已超过 5 分钟），请稍后重新发起导出",
})

const unmatchedEmpty = computed(() =>
  unmatchedPhone.value.trim() || unmatchedRange.value
    ? { title: "没有符合筛选条件的无主报告", description: "调整手机号或时间范围后重新查询，也可重置筛选。" }
    : { title: "暂无迁移期无主报告", description: "无法匹配平台批次的状态报告在此留存，仅供对账核查。" },
)

const unmatchedList = usePagedList({
  fetcher: (page, signal) =>
    listUnmatched({ page, phone: unmatchedPhone.value, ...rangeToIsoParams(unmatchedRange.value) }, signal),
  errorMessage: "运维数据加载失败",
})

const { items: unmatched, total: unmatchedTotal, page: unmatchedPage } = unmatchedList
const loading = unmatchedList.loading
const errorMessage = unmatchedList.errorMessage
async function load(_tab?: string): Promise<void> {
  if (props.active) await unmatchedList.load()
}
function reloadFromFirstPage(_tab?: string): void {
  unmatchedList.search()
}

function unmatchedPhoneProblem(): string | null {
  const value = unmatchedPhone.value.trim()
  return phoneProblem(value) ?? null
}

function searchUnmatched(): void {
  const issue = unmatchedPhoneProblem()
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  reloadFromFirstPage("unmatched")
}

function resetUnmatched(): void {
  unmatchedPhone.value = ""
  unmatchedRange.value = null
  reloadFromFirstPage("unmatched")
}

async function exportUnmatched(): Promise<void> {
  const issue = unmatchedPhoneProblem()
  if (issue) {
    ElMessage.warning(issue)
    return
  }
  const created = await startExportTask(() =>
    createUnmatchedExport(
      {
        phone: unmatchedPhone.value,
        ...rangeToIsoParams(unmatchedRange.value),
      },
      exportDecrypted.value,
    ),
  )
  if (created) ElMessage.success("对账导出任务已创建 · 本次操作已记入审计")
  else ElMessage.error(exportError.value)
}

async function downloadUnmatchedExport(): Promise<void> {
  await downloadExportFile("unmatched-reports")
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      unmatchedList.cancel()
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
    <section
      id="ops-panel-unmatched"
      v-loading="loading"
      class="ops-panel"
      role="tabpanel"
      aria-labelledby="ops-tab-unmatched"
    >
      <header class="ops-panel-title"
        ><div
          ><strong>迁移期无主报告</strong><small>无法匹配平台批次的状态报告在此留存，仅供对账核查</small></div
        ></header
      >
      <form class="ops-filter-bar" @submit.prevent="searchUnmatched">
        <label class="ops-fld"
          ><span>手机号</span>
          <el-input
            v-model="unmatchedPhone"
            class="ops-phone"
            maxlength="11"
            clearable
            placeholder="手机号精确查询"
            data-testid="ops-unmatched-phone"
            aria-label="无主报告手机号"
          />
        </label>
        <label class="ops-fld"
          ><span>报告时间</span>
          <el-date-picker
            v-model="unmatchedRange"
            class="ops-dates"
            type="datetimerange"
            popper-class="qingluan-date-popper"
            range-separator="至"
            start-placeholder="开始时间"
            end-placeholder="结束时间"
          />
        </label>
        <div class="ops-filter-go">
          <el-button data-testid="ops-unmatched-search" @click="searchUnmatched">查询</el-button>
          <el-button @click="resetUnmatched">重置</el-button>
          <el-checkbox v-model="exportDecrypted">授权明文</el-checkbox>
          <el-button type="primary" :loading="exportBusy" @click="exportUnmatched">导出对账</el-button>
        </div>
        <p class="ops-privacy"
          >手机号明文仅随请求体提交，服务端立即转换为 HMAC
          精确查询，不写入日志与存储；勾选「授权明文」导出的文件仍以密文落盘，下载时需重新输入当前认证源密码。</p
        >
      </form>
      <el-alert v-if="exportError" :title="exportError" type="error" :closable="false" />
      <el-alert
        v-if="exportTask"
        :title="`导出任务 #${exportTask.id} · ${exportTask.status}`"
        :type="exportTask.status === 'failed' ? 'error' : 'success'"
        :closable="false"
        ><template #default
          ><div class="export-task-detail"
            ><span v-if="exportTask.row_count !== null">{{ exportTask.row_count }} 行</span
            ><span v-if="exportTask.expires_at">有效期至 {{ formatDateTime(exportTask.expires_at) }}</span
            ><el-button
              v-if="exportTask.status === 'done' && exportTask.download_url"
              data-testid="download-unmatched-export"
              type="primary"
              link
              @click="downloadUnmatchedExport"
              >下载 CSV</el-button
            ></div
          ></template
        ></el-alert
      >
      <section class="ops-results">
        <el-table :data="unmatched" row-key="id" class="ops-table"
          ><el-table-column label="号码" width="140"
            ><template #default="{ row }"><PhoneMask :value="row.phone_mask" /></template></el-table-column
          ><el-table-column label="customId" min-width="170"
            ><template #default="{ row }"
              ><code class="ops-hash" :title="row.custom_id || ''">{{ row.custom_id || "—" }}</code></template
            ></el-table-column
          ><el-table-column label="厂商任务" min-width="150"
            ><template #default="{ row }"
              ><code class="ops-hash" :title="row.vendor_task_id || ''">{{ row.vendor_task_id || "—" }}</code></template
            ></el-table-column
          ><el-table-column prop="report_desc" label="结果" width="120" /><el-table-column label="报告时间" width="180"
            ><template #default="{ row }">{{ formatDateTime(row.report_time) }}</template></el-table-column
          ><template #empty
            ><EmptyState :title="unmatchedEmpty.title" :description="unmatchedEmpty.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in unmatched" :key="item.id"
            ><header><PhoneMask :value="item.phone_mask" /><el-tag type="warning">无主报告</el-tag></header
            ><code>{{ item.custom_id || "—" }}</code
            ><p>{{ item.report_desc || "未知结果" }} · {{ formatDateTime(item.report_time) }}</p></article
          ><EmptyState v-if="!unmatched.length" :title="unmatchedEmpty.title" :description="unmatchedEmpty.description"
        /></div>
        <ListPagination
          v-model:page="unmatchedPage"
          :total="unmatchedTotal"
          :page-size="DEFAULT_PAGE_SIZE"
          testid="ops-unmatched-pagination"
          unit="条"
          @change="load('unmatched')"
        ></ListPagination>
      </section>
    </section>
  </div>
</template>
