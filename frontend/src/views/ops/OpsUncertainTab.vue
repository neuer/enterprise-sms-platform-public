<script setup lang="ts">
import { usePagedList } from "../../composables/usePagedList"

import ListPagination from "../../components/ListPagination.vue"

import { ElMessage } from "element-plus"

import { watch } from "vue"

import {
  listUncertain,
  proposeUncertainResolution,
  confirmUncertainResolution,
  type UncertainItem,
  type UncertainResolutionAction,
} from "../../api/ops"

import { useConfirmActions } from "../../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()

import { errorText } from "../../lib/error"

import { DEFAULT_PAGE_SIZE } from "../../lib/labels"

import EmptyState from "../../components/EmptyState.vue"

import StatusTag from "../../components/StatusTag.vue"

import { useSessionStore } from "../../stores/session"

const props = defineProps<{ active: boolean }>()

const session = useSessionStore()

const RESOLUTION_ACTIONS: { action: UncertainResolutionAction; label: string }[] = [
  { action: "confirm_accepted", label: "确认已受理" },
  { action: "confirm_not_accepted", label: "确认未受理" },
  { action: "keep_unknown", label: "保持未知" },
  { action: "resend_new_batch", label: "新批次重发" },
]

const UNCERTAIN_EMPTY = {
  title: "当前没有结果未知的分片",
  description:
    "uncertain 禁止自动重发；仅 reconcile 可按厂商报文修复，到期进入保守终态后须双人确认处置。Web 重发使用源批次部门与受控 system 计费主体，不会使用 app_id=-1。",
}

function duration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

function resolutionLabel(action: string | null | undefined): string {
  return RESOLUTION_ACTIONS.find((item) => item.action === action)?.label ?? action ?? "—"
}

function resolutionStateLabel(state: string | null | undefined): string {
  const labels: Record<string, string> = {
    proposed: "待确认",
    approved: "已批准",
    effect_pending: "待生效",
    applying: "生效中",
    effect_applied: "已生效",
    closed: "已关闭",
    approval_rejected: "已驳回",
    retryable_effect_error: "生效失败可重试",
    manual_intervention_required: "需人工介入",
    cancelled_before_effect: "已取消",
  }
  return (state && labels[state]) || state || "—"
}

const uncertainList = usePagedList({
  fetcher: (page, signal) => listUncertain({ page }, signal),
  errorMessage: "运维数据加载失败",
})

const { items: uncertain, total: uncertainTotal, page: uncertainPage } = uncertainList
const loading = uncertainList.loading
const errorMessage = uncertainList.errorMessage
async function load(_tab?: string): Promise<void> {
  if (props.active) await uncertainList.load()
}

async function proposeResolution(item: UncertainItem, action: UncertainResolutionAction): Promise<void> {
  item = { ...item }
  if (
    !(await confirmAuditedAction({
      title: "确认提出处置",
      body: `对批次 ${item.batch_no} 提出「${resolutionLabel(action)}」。确认后须另一名管理员复核；重发只会创建新批次，不会把旧分片改回待发送。`,
      auditNote: "提出行为与操作人将写入审计日志。",
      confirmText: "提出处置",
    }))
  )
    return
  try {
    await proposeUncertainResolution(item.chunk_id, action)
    ElMessage.success("已提出处置 · 本次操作已记入审计")
    await load("uncertain")
  } catch (error) {
    ElMessage.error(errorText(error, "提出处置失败"))
  }
}

async function confirmResolution(item: UncertainItem): Promise<void> {
  item = { ...item }
  if (item.resolution_id == null) return
  if (
    !(await confirmAuditedAction({
      title: "确认处置",
      body: `确认批次 ${item.batch_no} 的处置「${resolutionLabel(item.resolution_action)}」。提案人不能确认自己的单。`,
      auditNote: "确认行为与操作人将写入审计日志。",
      confirmText: "确认处置",
    }))
  )
    return
  try {
    await confirmUncertainResolution(item.resolution_id)
    ElMessage.success("处置已确认 · 本次操作已记入审计")
    await load("uncertain")
  } catch (error) {
    ElMessage.error(errorText(error, "确认处置失败"))
  }
}
watch(
  () => props.active,
  (active) => {
    if (active) void load()
    else {
      uncertainList.cancel()
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
      id="ops-panel-uncertain"
      v-loading="loading"
      class="ops-panel"
      role="tabpanel"
      aria-labelledby="ops-tab-uncertain"
    >
      <header class="ops-panel-title"
        ><div
          ><strong>结果未知分片</strong
          ><small
            >禁止自动重发；保守终态后双人确认处置，重发只建新批次。Web 重发继承源部门并走受控 system 主体，不使用负数
            app_id</small
          ></div
        ></header
      >
      <section class="ops-results">
        <el-table :data="uncertain" row-key="chunk_id" class="ops-table"
          ><el-table-column prop="batch_no" label="批次" min-width="160" /><el-table-column label="状态" width="110"
            ><template #default="{ row }"><StatusTag :status="row.status" /></template></el-table-column
          ><el-table-column label="customId" min-width="160"
            ><template #default="{ row }"
              ><code class="ops-hash" :title="row.custom_id">{{ row.custom_id }}</code></template
            ></el-table-column
          ><el-table-column prop="phone_count" label="号码数" width="90" /><el-table-column label="停留" width="110"
            ><template #default="{ row }"
              ><el-tag :type="row.age_seconds >= 86400 ? 'danger' : 'warning'">{{
                duration(row.age_seconds)
              }}</el-tag></template
            ></el-table-column
          ><el-table-column label="处置" min-width="220"
            ><template #default="{ row }"
              ><template v-if="row.status === 'unknown_terminal' && !row.resolution_id"
                ><el-button
                  v-for="option in RESOLUTION_ACTIONS"
                  :key="option.action"
                  link
                  type="primary"
                  :data-testid="`uncertain-propose-${option.action}`"
                  @click="proposeResolution(row, option.action)"
                  >{{ option.label }}</el-button
                ></template
              ><template v-else-if="row.resolution_state === 'proposed'"
                ><span>待确认 · {{ resolutionLabel(row.resolution_action) }}</span
                ><el-button
                  v-if="session.accountId !== row.proposer_account_id"
                  link
                  type="danger"
                  data-testid="uncertain-confirm"
                  @click="confirmResolution(row)"
                  >确认处置</el-button
                ></template
              ><span v-else-if="row.resolution_state"
                >{{ resolutionStateLabel(row.resolution_state) }} · {{ resolutionLabel(row.resolution_action) }}</span
              ><span v-else>禁止自动重发</span></template
            ></el-table-column
          ><template #empty
            ><EmptyState :title="UNCERTAIN_EMPTY.title" :description="UNCERTAIN_EMPTY.description" /></template
        ></el-table>
        <div class="ops-mobile-list"
          ><article v-for="item in uncertain" :key="item.chunk_id"
            ><header
              ><strong>{{ item.batch_no }}</strong
              ><StatusTag :status="item.status" /></header
            ><code>{{ item.custom_id }}</code
            ><p>{{ item.phone_count }} 个号码 · {{ duration(item.age_seconds) }}</p
            ><template v-if="item.status === 'unknown_terminal' && !item.resolution_id"
              ><el-button
                v-for="option in RESOLUTION_ACTIONS"
                :key="option.action"
                link
                type="primary"
                @click="proposeResolution(item, option.action)"
                >{{ option.label }}</el-button
              ></template
            ><el-button
              v-else-if="item.resolution_state === 'proposed' && session.accountId !== item.proposer_account_id"
              link
              type="danger"
              @click="confirmResolution(item)"
              >确认处置</el-button
            ></article
          ><EmptyState
            v-if="!uncertain.length"
            :title="UNCERTAIN_EMPTY.title"
            :description="UNCERTAIN_EMPTY.description"
        /></div>
        <ListPagination
          v-model:page="uncertainPage"
          :total="uncertainTotal"
          :page-size="DEFAULT_PAGE_SIZE"
          testid="ops-uncertain-pagination"
          unit="项"
          @change="load('uncertain')"
        ></ListPagination>
      </section>
    </section>
  </div>
</template>
