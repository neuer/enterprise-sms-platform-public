<script setup lang="ts">
import { usePagedList } from "../../composables/usePagedList"

import ListPagination from "../../components/ListPagination.vue"

import FilterSeg from "../../components/FilterSeg.vue"

import { ElMessage } from "element-plus"

import { computed, ref, watch } from "vue"

import { listRawLogs, replayRaw, type RawLogItem } from "../../api/ops"

import { useConfirmActions } from "../../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()

import { errorText } from "../../lib/error"

import { DEFAULT_PAGE_SIZE } from "../../lib/labels"

import { formatDateTime } from "../../lib/time"

import EmptyState from "../../components/EmptyState.vue"

const props = defineProps<{ active: boolean }>()

const rawSource = ref<"" | RawLogItem["source"]>("")

const rawProcessed = ref<"" | "true" | "false">("")

const RAW_SOURCE_OPTIONS: { key: string; label: string; value: "" | RawLogItem["source"] }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "report", label: "报告", value: "report" },
  { key: "reply", label: "回复", value: "reply" },
]

const RAW_PROCESSED_OPTIONS: { key: string; label: string; value: "" | "true" | "false" }[] = [
  { key: "all", label: "全部", value: "" },
  { key: "false", label: "未处理", value: "false" },
  { key: "true", label: "已处理", value: "true" },
]

const rawEmpty = computed(() =>
  rawSource.value || rawProcessed.value
    ? { title: "没有符合筛选条件的报文", description: "调整来源或处理状态后重新查询，也可重置筛选。" }
    : { title: "暂无原始报文", description: "厂商报文拉取后先以密文落入保险箱，再受控解密解析。" },
)

const rawList = usePagedList({
  fetcher: (page, signal) =>
    listRawLogs(
      {
        page,
        source: rawSource.value || undefined,
        processed: rawProcessed.value === "" ? undefined : rawProcessed.value === "true",
      },
      signal,
    ),
  errorMessage: "运维数据加载失败",
})

const { items: rawLogs, total: rawTotal, page: rawPage } = rawList
const loading = rawList.loading
const errorMessage = rawList.errorMessage
async function load(_tab?: string): Promise<void> {
  if (props.active) await rawList.load()
}
function reloadFromFirstPage(_tab?: string): void {
  rawList.search()
}

function setRawSource(value: "" | RawLogItem["source"]): void {
  if (rawSource.value === value) return
  rawSource.value = value
  reloadFromFirstPage("raw")
}

function setRawProcessed(value: "" | "true" | "false"): void {
  if (rawProcessed.value === value) return
  rawProcessed.value = value
  reloadFromFirstPage("raw")
}

function canReplay(item: RawLogItem): boolean {
  return (
    !item.processed &&
    ["complete", "complete_too_large"].includes(item.capture_state) &&
    ["unattempted", "transient_failure"].includes(item.parse_state) &&
    ["automatic", "manual"].includes(item.replay_eligibility)
  )
}

function replayStatus(item: RawLogItem): string {
  if (item.processed) return "已处理"
  if (canReplay(item)) return "可重放"
  return "不可重放"
}

async function replay(item: RawLogItem): Promise<void> {
  item = { ...item }
  if (!canReplay(item)) return
  if (
    !(await confirmAuditedAction({
      title: "确认报文重放",
      body: `重放 raw #${item.id}：仅允许未处理且载荷完整的报文；重放重新走受控解密解析，不会产生重复下发。`,
      auditNote: "重放行为与操作人将写入审计日志。",
      confirmText: "确认重放",
    }))
  )
    return
  try {
    const result = await replayRaw(item.id)
    ElMessage.success(`重放完成，处理 ${result.processed_items} 项 · 本次操作已记入审计`)
    await load("raw")
  } catch (error) {
    ElMessage.error(errorText(error, "重放失败"))
  }
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      rawList.cancel()
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
    <section id="ops-panel-raw" v-loading="loading" class="ops-panel" role="tabpanel" aria-labelledby="ops-tab-raw">
      <header class="ops-panel-title"
        ><div><strong>原始报文保险箱</strong><small>密文载荷与完整性摘要不对外返回</small></div></header
      >
      <form class="ops-filter-bar" @submit.prevent>
        <div class="ops-fld"
          ><span>来源</span>
          <FilterSeg
            :model-value="rawSource"
            :options="RAW_SOURCE_OPTIONS"
            button-testid-prefix="ops-raw-source"
            aria-label="报文来源筛选"
            data-testid="ops-raw-source-seg"
            @update:model-value="setRawSource"
          />
        </div>
        <div class="ops-fld"
          ><span>处理状态</span>
          <FilterSeg
            :model-value="rawProcessed"
            :options="RAW_PROCESSED_OPTIONS"
            button-testid-prefix="ops-raw-processed"
            aria-label="处理状态筛选"
            data-testid="ops-raw-processed-seg"
            @update:model-value="setRawProcessed"
          />
        </div>
        <p class="ops-privacy">点选即重查；拉走即消费，完整响应先以 AES-GCM 密文落库，页面只展示无 PII 元数据。</p>
      </form>
      <section class="ops-results">
        <el-table :data="rawLogs" row-key="id" class="ops-table"
          ><el-table-column prop="id" label="RAW" width="80" /><el-table-column
            prop="source"
            label="来源"
            width="100" /><el-table-column label="记录 / customId" min-width="150"
            ><template #default="{ row }">{{ row.item_count }} / {{ row.custom_id_count }}</template></el-table-column
          ><el-table-column label="状态" width="110"
            ><template #default="{ row }"
              ><el-tag :type="row.processed ? 'success' : 'danger'">{{ replayStatus(row) }}</el-tag></template
            ></el-table-column
          ><el-table-column label="完整性" width="120"
            ><template #default="{ row }"
              ><el-tag
                :type="
                  row.capture_state === 'complete'
                    ? 'success'
                    : row.capture_state === 'complete_too_large'
                      ? 'warning'
                      : 'danger'
                "
                >{{
                  row.capture_state === "complete"
                    ? "完整"
                    : row.capture_state === "complete_too_large"
                      ? "超限完整"
                      : "截断"
                }}</el-tag
              ></template
            ></el-table-column
          ><el-table-column prop="error" label="错误摘要" min-width="180" /><el-table-column label="时间" width="180"
            ><template #default="{ row }">{{ formatDateTime(row.fetched_at) }}</template></el-table-column
          ><el-table-column label="操作" width="90"
            ><template #default="{ row }"
              ><el-button v-if="canReplay(row)" link type="danger" @click="replay(row)">重放</el-button></template
            ></el-table-column
          ><template #empty><EmptyState :title="rawEmpty.title" :description="rawEmpty.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in rawLogs" :key="item.id"
            ><header
              ><strong>RAW-{{ item.id }} · {{ item.source }}</strong
              ><el-tag :type="item.processed ? 'success' : 'danger'">{{ replayStatus(item) }}</el-tag></header
            ><p
              >{{ item.item_count }} 项 · {{ item.custom_id_count }} customId ·
              {{
                item.capture_state === "complete"
                  ? "完整"
                  : item.capture_state === "complete_too_large"
                    ? "超限完整"
                    : "截断"
              }}</p
            ><small>{{ item.error || formatDateTime(item.fetched_at) }}</small
            ><el-button v-if="canReplay(item)" link type="danger" @click="replay(item)">重放</el-button></article
          ><EmptyState v-if="!rawLogs.length" :title="rawEmpty.title" :description="rawEmpty.description"
        /></div>
        <ListPagination
          v-model:page="rawPage"
          :total="rawTotal"
          :page-size="DEFAULT_PAGE_SIZE"
          testid="ops-raw-pagination"
          unit="条"
          @change="load('raw')"
        ></ListPagination>
      </section>
    </section>
  </div>
</template>
