<script setup lang="ts">
import { usePagedList } from "../../composables/usePagedList"

import ListPagination from "../../components/ListPagination.vue"

import FilterSeg from "../../components/FilterSeg.vue"

import { ElMessage } from "element-plus"

import { computed, h, ref, watch } from "vue"

import {
  getOutboxStatus,
  listOutboxEvents,
  retryOutboxEvent,
  type OutboxEventItem,
  type OutboxState,
  type OutboxStats,
} from "../../api/ops"

import { useConfirmActions } from "../../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()

import { errorText } from "../../lib/error"

import { DEFAULT_PAGE_SIZE } from "../../lib/labels"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

const props = defineProps<{ active: boolean }>()

const outboxStats = ref<OutboxStats | null>(null)

const outboxState = ref<"" | OutboxState>("")

const retryingOutboxId = ref<string | null>(null)

// 与服务端 Query(pattern=^1\d{10}$) 同一规则（硬性规则 8）；服务端仍为权威校验。

const OUTBOX_STATE_META: Record<OutboxState, { label: string; tag: "info" | "warning" | "success" | "danger" }> = {
  dead: { label: "死信", tag: "danger" },
  pending: { label: "待投递", tag: "warning" },
  leased: { label: "已租约", tag: "info" },
  published: { label: "已发布", tag: "info" },
  processing: { label: "处理中", tag: "info" },
  completed: { label: "已完成", tag: "success" },
}

const OUTBOX_STATE_OPTIONS = (Object.keys(OUTBOX_STATE_META) as OutboxState[]).map((value) => ({
  value,
  label: OUTBOX_STATE_META[value].label,
}))

const outboxEmpty = computed(() =>
  outboxState.value
    ? { title: "没有符合筛选条件的投递事件", description: "调整事件状态后重新查询，也可重置查看全部事件。" }
    : { title: "暂无投递事件", description: "Outbox 事件由业务事务写入，dispatcher 按租约逐步投递。" },
)

function duration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

const outboxList = usePagedList({
  fetcher: async (page, signal) => {
    const [stats, events] = await Promise.all([
      getOutboxStatus(signal),
      listOutboxEvents({ page, state: outboxState.value || undefined }, signal),
    ])
    return { ...events, stats }
  },
  onLoaded: (result) => {
    outboxStats.value = result.stats
  },
  errorMessage: "运维数据加载失败",
})

const { items: outboxEvents, total: outboxTotal, page: outboxPage } = outboxList
const loading = outboxList.loading
const errorMessage = outboxList.errorMessage
async function load(_tab?: string): Promise<void> {
  if (props.active) await outboxList.load()
}
function reloadFromFirstPage(_tab?: string): void {
  outboxList.search()
}

function setOutboxState(value: "" | OutboxState): void {
  if (outboxState.value === value) return
  outboxState.value = value
  reloadFromFirstPage("outbox")
}

function shortTaskName(taskName: string): string {
  return taskName.split(".").pop() ?? taskName
}

function outboxStateMeta(state: OutboxState): { label: string; tag: "info" | "warning" | "success" | "danger" } {
  return OUTBOX_STATE_META[state]
}

async function retryOutbox(item: OutboxEventItem): Promise<void> {
  item = { ...item }
  if (
    !(await confirmAuditedAction({
      title: "确认重推 Outbox 事件",
      body: h("p", [
        "将死信事件 ",
        h("strong", item.event_type),
        `（${item.aggregate_type}/${item.aggregate_id}）重置为待投递，dispatcher 将按租约重新投递。`,
      ]),
      auditNote: "重推行为与操作人将写入审计日志。",
      confirmText: "重推事件",
    }))
  )
    return
  try {
    retryingOutboxId.value = item.id
    await retryOutboxEvent(item.id)
    ElMessage.success("事件已重置为待投递 · 本次操作已记入审计")
    await load("outbox")
  } catch (error) {
    ElMessage.error(errorText(error, "事件重推失败"))
  } finally {
    retryingOutboxId.value = null
  }
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      outboxList.cancel()
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
      id="ops-panel-outbox"
      v-loading="loading"
      class="ops-panel"
      role="tabpanel"
      aria-labelledby="ops-tab-outbox"
    >
      <header class="ops-panel-title"
        ><div
          ><strong>事务性 Outbox 投递</strong><small>PostgreSQL 为唯一事实源 · 死信事件确认后可人工重推</small></div
        ></header
      >
      <form class="ops-filter-bar" @submit.prevent>
        <div class="ops-fld"
          ><span>事件状态</span>
          <FilterSeg
            :model-value="outboxState"
            :options="[
              ...[{ label: '全部', value: '' as const, testid: 'ops-outbox-state-all' }],
              ...OUTBOX_STATE_OPTIONS,
            ]"
            button-testid-prefix="ops-outbox-state"
            aria-label="Outbox 事件状态筛选"
            data-testid="ops-outbox-state"
            @update:model-value="setOutboxState"
          />
        </div>
        <p class="ops-privacy">点选即重查；事件由业务事务写入，dispatcher 按租约逐步投递，重推不二次投递已完成事件。</p>
      </form>
      <div v-if="outboxStats" class="outbox-stats" data-testid="outbox-stats">
        <article
          ><span>待投递</span><strong>{{ outboxStats.pending }}</strong></article
        >
        <article
          ><span>已发布</span><strong>{{ outboxStats.published }}</strong></article
        >
        <article
          ><span>处理中</span><strong>{{ outboxStats.processing }}</strong></article
        >
        <article class="danger"
          ><span>死信</span><strong>{{ outboxStats.dead }}</strong></article
        >
        <article
          ><span>失败尝试</span><strong>{{ outboxStats.failed_attempts }}</strong></article
        >
        <article
          ><span>最老积压</span><strong>{{ duration(outboxStats.oldest_age_seconds) }}</strong></article
        >
      </div>
      <section class="ops-results">
        <el-table :data="outboxEvents" row-key="id" class="ops-table"
          ><el-table-column label="事件" min-width="140"
            ><template #default="{ row }"
              ><strong>{{ row.event_type }}</strong></template
            ></el-table-column
          ><el-table-column label="聚合引用" min-width="180"
            ><template #default="{ row }"
              ><code class="batch-code">{{ row.aggregate_type }}/{{ row.aggregate_id }}</code></template
            ></el-table-column
          ><el-table-column label="任务" min-width="130"
            ><template #default="{ row }">{{ shortTaskName(row.task_name) }}</template></el-table-column
          ><el-table-column prop="queue" label="队列" width="90" /><el-table-column label="状态" width="90"
            ><template #default="{ row }"
              ><el-tag :type="outboxStateMeta(row.state).tag">{{ outboxStateMeta(row.state).label }}</el-tag></template
            ></el-table-column
          ><el-table-column label="尝试" width="80"
            ><template #default="{ row }">{{ row.attempts }}/{{ row.max_attempts }}</template></el-table-column
          ><el-table-column prop="failure_count" label="失败" width="70" /><el-table-column
            label="最近错误"
            min-width="130"
            ><template #default="{ row }">{{ row.last_error || "—" }}</template></el-table-column
          ><el-table-column label="更新时间" width="170"
            ><template #default="{ row }">{{ formatDateTime(row.updated_at) }}</template></el-table-column
          ><el-table-column label="操作" width="80" fixed="right"
            ><template #default="{ row }"
              ><el-button
                v-if="row.state === 'dead'"
                link
                type="danger"
                :loading="retryingOutboxId === row.id"
                :data-testid="`outbox-retry-${row.id}`"
                @click="retryOutbox(row)"
                >重推</el-button
              ></template
            ></el-table-column
          ><template #empty><EmptyState :title="outboxEmpty.title" :description="outboxEmpty.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in outboxEvents" :key="item.id"
            ><header
              ><strong>{{ item.event_type }}</strong
              ><el-tag :type="outboxStateMeta(item.state).tag">{{ outboxStateMeta(item.state).label }}</el-tag></header
            ><code>{{ item.aggregate_type }}/{{ item.aggregate_id }}</code
            ><p
              >{{ shortTaskName(item.task_name) }} · {{ item.queue }} · 尝试 {{ item.attempts }}/{{
                item.max_attempts
              }}
              · 失败 {{ item.failure_count }}</p
            ><small>{{ item.last_error || formatDateTime(item.updated_at) }}</small
            ><el-button
              v-if="item.state === 'dead'"
              link
              type="danger"
              :loading="retryingOutboxId === item.id"
              @click="retryOutbox(item)"
              >重推</el-button
            ></article
          ><EmptyState v-if="!outboxEvents.length" :title="outboxEmpty.title" :description="outboxEmpty.description"
        /></div>
        <ListPagination
          v-model:page="outboxPage"
          :total="outboxTotal"
          :page-size="DEFAULT_PAGE_SIZE"
          testid="ops-outbox-pagination"
          unit="条"
          @change="load('outbox')"
        ></ListPagination>
      </section>
    </section>
  </div>
</template>
