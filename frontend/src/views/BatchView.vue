<script setup lang="ts">
import { vRowActivate } from "../lib/directives"
import { usePagedList } from "../composables/usePagedList"
import { useBatchFilters } from "./batch/useBatchFilters"
import ListPagination from "../components/ListPagination.vue"
import FilterSeg from "../components/FilterSeg.vue"
import BatchDetailDrawer from "../components/BatchDetailDrawer.vue"
import { ElMessage } from "element-plus"
import { computed, onMounted, ref, watch } from "vue"
import { useRoute } from "vue-router"

import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"

import LoadErrorAlert from "../components/LoadErrorAlert.vue"
import MobileFilterToggle from "../components/MobileFilterToggle.vue"
import StatusTag from "../components/StatusTag.vue"
import {
  getBatch,
  getBatchMessages,
  cancelBatch,
  listBatches,
  resendFailedBatch,
  rescheduleBatch,
  type BatchItem,
} from "../api/queries"
import { CATEGORY_LABELS, DEFAULT_PAGE_SIZE } from "../lib/labels"
import { buildCompose, channelLabel, composePct, describeCompose, shortBatchNo } from "../lib/batchDisplay"
import { useConfirmActions } from "../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()
import { formatNumber } from "../lib/format"
import { formatDateTime, formatDateTimeMinute, toApiDateTime } from "../lib/time"
import { errorText } from "../lib/error"
import { useLatestRead } from "../composables/useLatestRead"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
// 测试环境未安装路由时 useRoute 返回 undefined；仅用于消费 /batches?batch_no= 深链。
const route = useRoute()

const drawer = ref(false)
const selected = ref<BatchItem | null>(null)
const detailStatus = ref("")
const rescheduleOpen = ref(false)
const scheduledAt = ref("")
const rescheduling = ref(false)
const canWrite = computed(() => session.canWrite)
const canDecrypt = computed(() => session.canDecrypt)
const isAdmin = computed(() => session.isAdmin)

const {
  statusCounts,
  category,
  statusGroup,
  channel,
  batchNo,
  range,
  isTest,
  appId,
  dept,
  moreOpen,
  appOptions,
  appliedFiltersKey,
  categoryOptions,
  channelOptions,
  isTestOptions,
  statusChipOptions,
  currentFiltersKey,
  filtersDirty,
  moreActiveCount,
  moreActive,
  filtersCollapsed,
  collapsedActiveCount,
  listQuery,
  clearFilters,
} = useBatchFilters(isAdmin)

/** 列表行构成随 items 一次预计算：渲染期每行 7+ 处引用同一构成，改查表不再重复组装。 */
const composeMap = computed(() => new Map(items.value.map((row) => [row.batch_no, buildCompose(row)])))

function composeOf(row: BatchItem): ReturnType<typeof buildCompose> {
  return composeMap.value.get(row.batch_no) ?? buildCompose(row)
}

function composeText(row: BatchItem): string {
  return describeCompose(row, composeOf(row))
}

function selectGroup(key: string): void {
  if (statusGroup.value === key) return
  statusGroup.value = key
  page.value = 1
  void load()
}

const batchRead = useLatestRead()
const deepLinkRead = useLatestRead()

let openToken = 0

const { items, total, page, loading, errorMessage, load, search } = usePagedList({
  fetcher: async (page, signal) => {
    const filtersKey = currentFiltersKey.value
    const result = await listBatches(listQuery(page), signal)
    return { ...result, filtersKey }
  },
  errorMessage: "批次列表加载失败",
  onLoaded: (result) => {
    statusCounts.value = result.status_counts ?? null
    appliedFiltersKey.value = result.filtersKey
  },
})

const {
  items: details,
  total: detailTotal,
  page: detailPage,
  loading: detailsLoading,
  load: loadDetails,
  cancel: cancelDetails,
} = usePagedList({
  fetcher: async (page, signal) => {
    const batch = selected.value?.batch_no
    if (!batch) return null
    const result = await getBatchMessages(batch, { status: detailStatus.value || undefined, page }, signal)
    return selected.value?.batch_no === batch ? result : null
  },
  errorMessage: "批次明细加载失败",
  toastError: true,
})

async function openBatch(item: BatchItem): Promise<void> {
  const token = ++openToken
  const signal = batchRead.start()
  drawer.value = true
  selected.value = item
  details.value = []
  detailTotal.value = 0
  detailPage.value = 1
  detailStatus.value = ""
  try {
    const [batch] = await Promise.all([getBatch(item.batch_no, signal), loadDetails()])
    if (signal.aborted || token !== openToken) return
    selected.value = batch
  } catch (error) {
    if (signal.aborted || token !== openToken) return
    ElMessage.error(errorText(error, "批次详情加载失败"))
  }
}

function filterDetails(): void {
  detailPage.value = 1
  void loadDetails()
}

const canScheduleOps = computed(() => canWrite.value && selected.value?.status === "scheduled")
const canResendFailed = computed(
  () => canWrite.value && selected.value?.channel === "web" && (selected.value?.failed ?? 0) > 0,
)

const cancelling = ref(false)
async function cancelSelected(): Promise<void> {
  if (!selected.value || !canScheduleOps.value || cancelling.value) return
  const target = selected.value
  const batch = target.batch_no
  cancelling.value = true
  try {
    if (
      !(await confirmAuditedAction({
        isCurrent: () => selected.value === target && canScheduleOps.value,
        title: "确认取消",
        body: `取消批次 ${batch}？配额将按规则回补。`,
        auditNote: "取消行为与操作人将写入审计日志。",
        confirmText: "确认取消",
      }))
    )
      return
    await cancelBatch(batch)
    drawer.value = false
    ElMessage.success("批次已取消 · 本次操作已记入审计")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "取消失败"))
  } finally {
    cancelling.value = false
  }
}

function openReschedule(): void {
  if (!canScheduleOps.value) return
  scheduledAt.value = selected.value?.scheduled_at || ""
  rescheduleOpen.value = true
}

async function saveReschedule(): Promise<void> {
  if (!selected.value || !scheduledAt.value || rescheduling.value) return
  const target = selected.value
  const batch = target.batch_no
  if (
    !(await confirmAuditedAction({
      isCurrent: () => selected.value === target && canScheduleOps.value,
      title: "确认改期",
      body: `批次 ${batch} 将改到新的定时时间，并重新执行审批判定；到达新时间前批次保持「已排期」。`,
      auditNote: "改期行为与操作人将写入审计日志。",
      confirmText: "确认改期",
    }))
  )
    return
  rescheduling.value = true
  try {
    await rescheduleBatch(batch, toApiDateTime(new Date(scheduledAt.value)))
    rescheduleOpen.value = false
    drawer.value = false
    ElMessage.success("批次已改期并重新执行审批判定 · 本次操作已记入审计")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "改期失败"))
  } finally {
    rescheduling.value = false
  }
}

const resending = ref(false)
async function resendFailed(): Promise<void> {
  if (!selected.value || !canResendFailed.value || resending.value) return
  const target = selected.value
  const batch = target.batch_no
  resending.value = true
  try {
    if (
      !(await confirmAuditedAction({
        isCurrent: () => selected.value === target && canResendFailed.value,
        title: "确认重发",
        body: "失败号码将生成新批次并完整重走频控、审批和时间窗。",
        auditNote: "重发行为与操作人将写入审计日志。",
        confirmText: "确认重发",
      }))
    )
      return
    const result = await resendFailedBatch(batch)
    ElMessage.success(`重发批次 ${result.batch_no} 已创建 · 本次操作已记入审计`)
    drawer.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "重发失败"))
  } finally {
    resending.value = false
  }
}

/** 重发溯源跳转：复用批次号模糊筛选定位源批次 */
function traceResendOf(sourceBatchNo: string): void {
  drawer.value = false
  batchNo.value = sourceBatchNo
  statusGroup.value = "all"
  page.value = 1
  void load()
}

function reset(): void {
  clearFilters()
  search()
}

onMounted(() => {
  void load()
  // 深链 /batches?batch_no=xxx：从回复页等入口直达批次详情抽屉
  const target = typeof route?.query.batch_no === "string" ? route.query.batch_no.trim() : ""
  if (target) {
    const signal = deepLinkRead.start()
    getBatch(target, signal)
      .then((batch) => {
        if (!signal.aborted) openBatch(batch)
      })
      .catch((error) => {
        if (!signal.aborted) ElMessage.error(errorText(error, "未找到对应批次或无权限查看"))
      })
  }
})

watch(drawer, (open) => {
  if (!open) {
    cancelDetails()
    batchRead.cancel()
  }
})
</script>

<template>
  <section class="page-heading batch-heading">
    <div>
      <p class="eyebrow">BATCH LEDGER / 发送账本</p>
      <h1>批次列表</h1>
      <p>按部门权限查看发送轨迹；号码在列表与详情中始终保持掩码。</p>
    </div>
    <span class="batch-scope"><i></i>当前口径 · {{ session.roleLabel }}</span>
  </section>

  <form
    class="batch-filter batch-filter-bar filter-collapsible"
    :class="{ 'is-collapsed': filtersCollapsed }"
    @submit.prevent="search"
  >
    <div class="batch-fld filter-keep">
      <span>批次号</span>
      <el-input v-model="batchNo" class="batch-filter-search" placeholder="模糊匹配批次号" clearable maxlength="64" />
    </div>
    <div class="batch-fld">
      <span>类别</span>
      <FilterSeg v-model="category" :options="categoryOptions" label="类别" data-testid="batch-category-filter" />
    </div>
    <div class="batch-fld">
      <span>渠道</span>
      <FilterSeg v-model="channel" :options="channelOptions" label="渠道" data-testid="batch-channel-filter" />
    </div>
    <div class="batch-fld">
      <span>创建时间</span>
      <el-date-picker
        v-model="range"
        type="datetimerange"
        format="YYYY-MM-DD HH:mm"
        popper-class="qingluan-date-popper"
        start-placeholder="创建开始"
        end-placeholder="创建结束"
        range-separator="至"
        class="batch-filter-dates"
      />
    </div>
    <div class="batch-fld">
      <el-popover v-model:visible="moreOpen" placement="bottom-end" :width="320" trigger="click">
        <template #reference>
          <button
            type="button"
            class="batch-more-trigger"
            :class="{ 'is-more-active': moreActive }"
            data-testid="batch-more-filters"
            >更多筛选 ▾<b v-if="moreActiveCount" class="batch-more-count">{{ moreActiveCount }}</b></button
          >
        </template>
        <div class="batch-more">
          <label>测试发送</label>
          <FilterSeg v-model="isTest" :options="isTestOptions" label="测试发送" data-testid="batch-is-test-filter" />
          <label>应用</label>
          <el-select
            v-if="appOptions.length"
            v-model="appId"
            placeholder="全部应用"
            clearable
            filterable
            data-testid="batch-app-filter"
          >
            <el-option
              v-for="app in appOptions"
              :key="app.id"
              :label="`${app.name}（${app.dept}）`"
              :value="String(app.id)"
            />
          </el-select>
          <el-input
            v-else
            v-model="appId"
            inputmode="numeric"
            placeholder="应用 ID（全部应用留空）"
            clearable
            data-testid="batch-app-filter"
          />
          <template v-if="isAdmin">
            <label>部门</label>
            <el-input v-model="dept" placeholder="全部部门" clearable maxlength="128" data-testid="batch-dept-filter" />
          </template>
        </div>
      </el-popover>
    </div>
    <MobileFilterToggle v-model:collapsed="filtersCollapsed" :active-count="collapsedActiveCount" />
    <div class="batch-filter-actions filter-keep">
      <span v-if="filtersDirty" class="batch-dirty" data-testid="batch-filters-dirty">● 条件已变更</span>
      <el-button type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button @click="reset">重置</el-button>
    </div>
  </form>

  <FilterSeg
    :model-value="statusGroup"
    :options="statusChipOptions"
    class="filter-seg--chips"
    data-testid="batch-status-chips"
    label="按状态分组筛选"
    button-testid-prefix="batch-chip"
    @update:model-value="selectGroup"
  >
    <template #prefix><span class="batch-chips-lbl">状态</span></template>
    <template #option="{ option }"
      >{{ option.label }}<b v-if="option.count !== null">{{ option.count }}</b></template
    >
    <template #suffix
      ><span class="batch-chips-meta"
        >分组 = 进行中(queued+sending) · 待审批 · 已排期 · 余额阻断 · 已完成 ·
        其他终态(cancelled+rejected+expired)</span
      ></template
    >
  </FilterSeg>

  <LoadErrorAlert :message="errorMessage" class="batch-error" @retry="load()" />
  <div class="batch-ledger">
    <el-table v-loading="loading" :data="items" row-key="batch_no" class="query-table" @row-click="openBatch">
      <el-table-column label="批次 / 时间" min-width="248">
        <template #default="{ row }">
          <code class="batch-code">{{ row.batch_no }}</code>
          <div
            v-if="(row.scheduled_at && row.status === 'scheduled') || row.is_test || row.resend_of"
            class="cell-subline"
          >
            <small class="mono-time">{{ formatDateTime(row.created_at) }}</small>
            <span v-if="row.scheduled_at && row.status === 'scheduled'" class="cell-flag cell-flag--sched"
              >定时 {{ formatDateTimeMinute(row.scheduled_at) }}</span
            >
            <span v-if="row.is_test" class="cell-flag cell-flag--test">测试</span>
            <button
              v-if="row.resend_of"
              type="button"
              class="cell-flag cell-flag--resend"
              :title="`重发自 ${row.resend_of}`"
              @click.stop="traceResendOf(row.resend_of)"
              >重发自 {{ shortBatchNo(row.resend_of) }} ↗</button
            >
          </div>
          <small v-else class="mono-time cell-subline">{{ formatDateTime(row.created_at) }}</small>
        </template>
      </el-table-column>
      <el-table-column label="类别 / 内容" min-width="240">
        <template #default="{ row }">
          <div class="cell-catline">
            <CategoryTag :category="row.category" />
            <p class="cell-content" :title="row.content">{{ row.content }}</p>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="来源" min-width="150">
        <template #default="{ row }">
          <div
            class="cell-src"
            :title="`${channelLabel[row.channel] || row.channel} · ${row.app_name || row.creator || '—'} · ${row.dept}`"
          >
            {{ channelLabel[row.channel] || row.channel }} · {{ row.app_name || row.creator || "—"
            }}<span class="cell-src-dept"> · {{ row.dept }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="结果构成（非成功率）" min-width="180">
        <template #default="{ row }">
          <div class="compose-nums">
            <span
              ><b>{{ formatNumber(row.delivered) }}</b> / {{ formatNumber(row.total) }}</span
            >
            <span v-if="row.failed > 0" class="is-failed">失败 {{ formatNumber(row.failed) }}</span>
            <span v-else-if="row.status === 'scheduled' || row.status === 'pending_approval'" class="compose-hint">{{
              row.status === "scheduled" ? "待进入流水线" : "待审批"
            }}</span>
          </div>
          <div class="compose" role="img" :aria-label="composeText(row)">
            <i class="compose-p" :style="{ width: composePct(composeOf(row).pending, row) }"></i>
            <i class="compose-s" :style="{ width: composePct(composeOf(row).sent, row) }"></i>
            <i class="compose-d" :style="{ width: composePct(composeOf(row).delivered, row) }"></i>
            <i class="compose-f" :style="{ width: composePct(composeOf(row).failed, row) }"></i>
            <i class="compose-u" :style="{ width: composePct(composeOf(row).unknown, row) }"></i>
            <i class="compose-o" :style="{ width: composePct(composeOf(row).other, row) }"></i>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="112" fixed="right">
        <template #default="{ row }">
          <StatusTag :status="row.status" />
          <small v-if="row.deferred_reason === 'market_window'" class="cell-deferred">窗外转定时</small>
        </template>
      </el-table-column>
      <el-table-column label="计费条" width="90" align="right">
        <template #default="{ row }"
          ><span class="mono-value">{{ formatNumber(row.quota_cost) }}</span></template
        >
      </el-table-column>
      <el-table-column label="操作" width="92" fixed="right">
        <template #default="{ row }"
          ><el-button
            v-row-activate="() => openBatch(row)"
            link
            type="primary"
            :aria-label="`查看批次 ${row.batch_no} 的详情`"
            @click.stop="openBatch(row)"
            >查看详情</el-button
          ></template
        >
      </el-table-column>
      <template #empty><EmptyState title="没有符合筛选条件的批次" description="调整筛选条件后重新查询。" /></template>
    </el-table>
    <div class="query-mobile-list">
      <article v-for="item in items" :key="item.batch_no">
        <header>
          <code>{{ item.batch_no }}</code>
          <StatusTag :status="item.status" />
        </header>
        <p class="cell-content">{{ item.content }}</p>
        <div class="compose" role="img" :aria-label="composeText(item)">
          <i class="compose-p" :style="{ width: composePct(composeOf(item).pending, item) }"></i>
          <i class="compose-s" :style="{ width: composePct(composeOf(item).sent, item) }"></i>
          <i class="compose-d" :style="{ width: composePct(composeOf(item).delivered, item) }"></i>
          <i class="compose-f" :style="{ width: composePct(composeOf(item).failed, item) }"></i>
          <i class="compose-u" :style="{ width: composePct(composeOf(item).unknown, item) }"></i>
          <i class="compose-o" :style="{ width: composePct(composeOf(item).other, item) }"></i>
        </div>
        <p class="query-mobile-meta">
          {{ CATEGORY_LABELS[item.category] }} · {{ channelLabel[item.channel] || item.channel }} · {{ item.dept }} ·
          送达 {{ item.delivered }}/{{ item.total
          }}<template v-if="item.failed > 0"> · 失败 {{ item.failed }}</template>
          <span v-if="item.is_test" class="cell-flag cell-flag--test">测试</span>
        </p>
        <footer
          ><time>{{ formatDateTime(item.created_at) }}</time
          ><el-button link type="primary" @click="openBatch(item)">查看详情</el-button></footer
        >
      </article>
    </div>
    <ListPagination
      v-model:page="page"
      :total="total"
      :page-size="DEFAULT_PAGE_SIZE"
      unit="个批次"
      class="batch-pager"
      @change="load"
      ><div class="compose-legend" aria-hidden="true">
        <span><i class="compose-p"></i>待处理</span>
        <span><i class="compose-s"></i>待回执</span>
        <span><i class="compose-d"></i>送达</span>
        <span><i class="compose-f"></i>失败</span>
        <span><i class="compose-u"></i>未知</span>
        <span><i class="compose-o"></i>其他</span>
        <em>构成 = 占受理总数的份额，不是成功率；成功率口径见统计报表</em>
      </div></ListPagination
    >
  </div>

  <BatchDetailDrawer
    v-model="drawer"
    v-model:detail-status="detailStatus"
    v-model:detail-page="detailPage"
    :selected="selected"
    :details="details"
    :detail-total="detailTotal"
    :details-loading="detailsLoading"
    :can-write="canWrite"
    :can-decrypt="canDecrypt"
    :can-schedule-ops="canScheduleOps"
    :can-resend-failed="canResendFailed"
    :cancelling="cancelling"
    :resending="resending"
    @cancel="cancelSelected"
    @reschedule="openReschedule"
    @resend="resendFailed"
    @trace="traceResendOf"
    @filter-details="filterDetails"
    @detail-page-change="loadDetails"
  />
  <el-dialog v-model="rescheduleOpen" title="批次改期" width="min(480px, 92vw)"
    ><el-date-picker
      v-model="scheduledAt"
      type="datetime"
      popper-class="qingluan-date-popper"
      value-format="YYYY-MM-DDTHH:mm:ss+08:00"
      placeholder="选择新的发送时间"
    /><template #footer
      ><el-button @click="rescheduleOpen = false">取消</el-button
      ><el-button
        type="primary"
        :loading="rescheduling"
        :disabled="!scheduledAt || rescheduling"
        @click="saveReschedule"
        >确认改期</el-button
      ></template
    ></el-dialog
  >
</template>
