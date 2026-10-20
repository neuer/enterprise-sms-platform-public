<script setup lang="ts">
import type { SendResult } from "../api/webMessages"
import { formatNumber } from "../lib/format"
import { STATUS_LABELS } from "../lib/labels"

/** 人工发送受理结果卡：只展示服务端受理结果并发出复制、查看批次与再发一批意图，草稿与幂等键仍在页面。 */
defineProps<{
  result: SendResult
  busy: boolean
  copied: boolean
  /** 提交时快照的类别 · 来源 · 时间摘要。 */
  summary: string
}>()

const emit = defineEmits<{
  copy: []
  viewBatches: []
  reset: []
}>()

/** 批次状态标签取 lib/labels 单点（规则：视图不再维护同名映射），未知值兜底回显原始状态码。 */
function sendStatusLabel(status: SendResult["status"]): string {
  return STATUS_LABELS[status] ?? status
}

function deferredReasonText(reason: string): string {
  if (reason === "market_window") return "超出营销发送时间窗，已转为定时发送"
  return `窗外转定时原因：${reason}`
}

function sendSuccessText(result: SendResult): string {
  const parts = [`批次 ${result.batch_no} 已受理，状态：${sendStatusLabel(result.status)}`]
  if (result.idempotent) parts.push("本次为幂等命中，返回历史批次，未重复发送")
  if (result.deferred_reason) parts.push(deferredReasonText(result.deferred_reason))
  return parts.join("。")
}

function removedTotal(result: SendResult): number {
  return result.removed_duplicate + result.removed_blacklist + result.removed_freq_limit
}
</script>

<template>
  <div class="send-result" data-testid="send-result">
    <header><i></i>已受理 · {{ sendStatusLabel(result.status) }}</header>
    <div class="batch-row">
      <code>{{ result.batch_no }}</code>
      <button type="button" :disabled="busy" @click="emit('copy')">{{ copied ? "已复制" : "复制批次号" }}</button>
    </div>
    <p class="result-line" data-testid="submitted-summary">{{ summary }}</p>
    <p class="result-line">{{ sendSuccessText(result) }}。</p>
    <div class="result-stats">
      <span
        >受理 <b>{{ formatNumber(result.accepted) }}</b></span
      >
      <span
        >剔除 <b>{{ formatNumber(removedTotal(result)) }}</b></span
      >
      <span
        >预扣 <b>{{ formatNumber(result.quota_cost) }}</b> 计费条</span
      >
    </div>
    <div class="result-acts">
      <el-button type="primary" @click="emit('viewBatches')">查看批次</el-button>
      <el-button @click="emit('reset')">再发一批</el-button>
    </div>
  </div>
</template>
