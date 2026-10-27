<script setup lang="ts">
import { decryptMessagePhone, type BatchItem, type BatchMessage } from "../api/queries"
import {
  activeOf,
  buildCompose as composeOf,
  channelLabel,
  composePct,
  describeCompose,
  terminalUnsent,
} from "../lib/batchDisplay"
import { formatNumber, formatPercent } from "../lib/format"
import { DEFAULT_PAGE_SIZE, DISPLAY_STATUS_OPTIONS, STATUS_LABELS } from "../lib/labels"
import { codePointLength } from "../lib/codePoints"
import { resendBlockText } from "../lib/resendEligibility"
import { formatDateTime } from "../lib/time"
import CategoryTag from "./CategoryTag.vue"
import EmptyState from "./EmptyState.vue"
import ListPagination from "./ListPagination.vue"
import PhoneMask from "./PhoneMask.vue"
import PhoneReveal from "./PhoneReveal.vue"
import StatusTag from "./StatusTag.vue"

/**
 * 批次详情抽屉：只负责展示与发出操作意图；取消、改期、重发的确认、在途守卫
 * 与明细加载仍由批次列表页统一处理。
 */
const open = defineModel<boolean>({ default: false })
const detailStatus = defineModel<string>("detailStatus", { required: true })
const detailPage = defineModel<number>("detailPage", { required: true })
defineProps<{
  selected: BatchItem | null
  details: BatchMessage[]
  detailTotal: number
  detailsLoading: boolean
  canWrite: boolean
  canDecrypt: boolean
  canScheduleOps: boolean
  canResendFailed: boolean
  detailUnconfirmed: boolean
  cancelling: boolean
  resending: boolean
  resendChild?: { batchNo: string; status: string } | null
}>()
const emit = defineEmits<{
  cancel: []
  reschedule: []
  resend: []
  supplement: []
  trace: [sourceBatchNo: string]
  filterDetails: []
  detailPageChange: []
  retryDetail: []
}>()

const detailStatusOptions = DISPLAY_STATUS_OPTIONS

/** 行内授权查看：仅把明文返回给 PhoneReveal 内存展示，视图不落明文状态。 */
async function revealPhone(messageId: number): Promise<string> {
  const result = await decryptMessagePhone(messageId)
  return result.phone
}
</script>

<template>
  <el-drawer v-model="open" size="min(560px, 92vw)" :teleported="false" class="batch-drawer">
    <template #header>
      <div v-if="selected" class="batch-drawer-head">
        <StatusTag v-if="!detailUnconfirmed" :status="selected.status" />
        <el-tag v-else type="info">状态待确认</el-tag>
        <code>{{ selected.batch_no }}</code>
        <small
          >创建于 {{ formatDateTime(selected.created_at) }} · {{ channelLabel[selected.channel] || selected.channel }} ·
          {{ selected.dept }}</small
        >
      </div>
      <span v-else>批次详情</span>
    </template>
    <template v-if="selected">
      <el-alert
        v-if="detailUnconfirmed"
        class="batch-status-unconfirmed"
        type="warning"
        :closable="false"
        title="暂时无法确认该批次的最新状态，取消和改期已暂停。"
        data-testid="batch-status-unconfirmed"
        show-icon
      >
        <el-button link type="primary" data-testid="retry-batch-detail" @click="emit('retryDetail')"
          >重新读取</el-button
        >
      </el-alert>
      <div v-if="canWrite" class="batch-actions">
        <el-button
          v-if="canScheduleOps"
          type="danger"
          plain
          :loading="cancelling"
          :disabled="cancelling"
          data-testid="cancel-batch"
          @click="emit('cancel')"
          >取消批次</el-button
        >
        <el-button
          v-if="canScheduleOps"
          :disabled="cancelling || resending"
          data-testid="reschedule-batch"
          @click="emit('reschedule')"
          >改期</el-button
        >
        <el-button
          v-if="selected.failed > 0"
          :loading="resending"
          :disabled="!canResendFailed || resending"
          data-testid="resend-failed"
          @click="emit('resend')"
          >重发失败（{{ formatNumber(selected.failed) }}）</el-button
        >
        <el-button
          v-if="selected.resend_block_reason === 'supplement_required'"
          :loading="resending"
          data-testid="resend-supplement"
          @click="emit('supplement')"
          >补发未覆盖（{{ formatNumber(selected.resend_uncovered_failed) }}）</el-button
        >
        <p v-if="resendBlockText(selected.resend_block_reason)" class="batch-actions-why" data-testid="resend-block">{{
          resendBlockText(selected.resend_block_reason)
        }}</p>
        <p class="batch-actions-why"
          >取消 / 改期仅「已排期」批次可用（服务端 409 为最终裁决）；API 批次须通过所属应用 API 重发。Web
          批次须等回执到齐后才能重发失败号码，并完整重走频控、审批与时间窗。</p
        >
      </div>

      <section class="batch-hero">
        <div class="compose compose--lg" role="img" :aria-label="describeCompose(selected, composeOf(selected))">
          <i class="compose-p" :style="{ width: composePct(composeOf(selected).pending, selected) }"></i>
          <i class="compose-s" :style="{ width: composePct(composeOf(selected).sent, selected) }"></i>
          <i class="compose-d" :style="{ width: composePct(composeOf(selected).delivered, selected) }"></i>
          <i class="compose-f" :style="{ width: composePct(composeOf(selected).failed, selected) }"></i>
          <i class="compose-u" :style="{ width: composePct(composeOf(selected).unknown, selected) }"></i>
          <i class="compose-o" :style="{ width: composePct(composeOf(selected).other, selected) }"></i>
          <i class="compose-x" :style="{ width: composePct(terminalUnsent(composeOf(selected)), selected) }"></i>
        </div>
        <div class="batch-hero-nums">
          <div
            ><span>待处理</span><b>{{ formatNumber(composeOf(selected).pending) }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).pending / selected.total) : "—"
            }}</small></div
          >
          <div
            ><span>已提交</span><b>{{ formatNumber(composeOf(selected).sent) }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).sent / selected.total) : "—"
            }}</small></div
          >
          <div
            ><span>送达</span><b>{{ formatNumber(composeOf(selected).delivered) }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).delivered / selected.total) : "—"
            }}</small></div
          >
          <div
            ><span>失败</span
            ><b :class="{ 'is-failed': composeOf(selected).failed > 0 }">{{
              formatNumber(composeOf(selected).failed)
            }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).failed / selected.total) : "—"
            }}</small></div
          >
          <div
            ><span>未知</span><b>{{ formatNumber(composeOf(selected).unknown) }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).unknown / selected.total) : "—"
            }}</small></div
          >
          <div
            ><span>其他</span><b>{{ formatNumber(composeOf(selected).other) }}</b
            ><small>{{
              selected.total > 0 ? formatPercent(composeOf(selected).other / selected.total) : "—"
            }}</small></div
          >
        </div>
        <p v-if="terminalUnsent(composeOf(selected)) > 0" class="batch-note" data-testid="unsent-terminal"
          >未下发已终止 {{ formatNumber(terminalUnsent(composeOf(selected))) }}：取消
          {{ formatNumber(composeOf(selected).cancelled) }}，驳回 {{ formatNumber(composeOf(selected).rejected) }}，过期
          {{ formatNumber(composeOf(selected).expired) }}，待核对
          {{ formatNumber(composeOf(selected).needs_review) }}。这些号码不再计入待处理。</p
        >
        <p class="batch-hero-quotas"
          >受理 <b>{{ formatNumber(selected.total) }}</b> · 计费条 <b>{{ formatNumber(selected.quota_cost) }}</b> · 单条
          <b>{{ selected.segments }}</b> 条 · 频控剔除 <b>{{ formatNumber(selected.removed_freq_limit) }}</b></p
        >
      </section>

      <p v-if="selected.status === 'sending' && activeOf(selected) > 0" class="batch-note"
        >仍有 {{ formatNumber(activeOf(selected)) }} 条未终态（待处理 {{ formatNumber(composeOf(selected).pending) }} +
        已提交
        {{
          formatNumber(composeOf(selected).sent)
        }}），批次保持发送中，直至提交完成、结果核对、回执到达或报告超时。构成非成功率。</p
      >
      <p v-if="selected.deferred_reason === 'market_window'" class="batch-note is-warn"
        >营销时间窗外，已转为定时发送；到达营销窗口后自动进入队列。</p
      >

      <section class="batch-content-card">
        <CategoryTag :category="selected.category" />
        <p>{{ selected.content }}</p>
        <small
          >内容长度 {{ codePointLength(selected.content) }} 字 · 单条 {{ selected.segments }} 计费条<template
            v-if="selected.category === 'verify'"
          >
            · 验证码已等长打码</template
          ></small
        >
      </section>

      <dl class="batch-facts">
        <div
          ><dt>渠道</dt><dd>{{ channelLabel[selected.channel] || selected.channel }}</dd></div
        >
        <div
          ><dt>应用</dt><dd>{{ selected.app_name || "—" }}</dd></div
        >
        <div
          ><dt>创建人</dt><dd>{{ selected.creator || "—" }}</dd></div
        >
        <div
          ><dt>部门</dt><dd>{{ selected.dept }}</dd></div
        >
        <div
          ><dt>创建时间</dt><dd>{{ formatDateTime(selected.created_at) }}</dd></div
        >
        <div
          ><dt>定时时间</dt><dd>{{ selected.scheduled_at ? formatDateTime(selected.scheduled_at) : "—" }}</dd></div
        >
        <div>
          <dt>重发溯源</dt>
          <dd
            ><button
              v-if="selected.resend_of"
              type="button"
              class="batch-trace"
              @click="emit('trace', selected.resend_of!)"
              >{{ selected.resend_of }} ↗</button
            ><template v-else>—</template></dd
          >
        </div>
        <div v-if="resendChild">
          <dt>重发批次</dt>
          <dd>
            <button
              type="button"
              class="batch-trace"
              data-testid="view-resend-batch"
              @click="emit('trace', resendChild.batchNo)"
              >查看重发批次 {{ resendChild.batchNo }} ·
              {{ STATUS_LABELS[resendChild.status] ?? resendChild.status }}</button
            >
          </dd>
        </div>
        <div
          ><dt>标记</dt><dd>{{ selected.is_test ? "测试发送" : "正式发送" }}</dd></div
        >
      </dl>

      <div class="batch-detail-head"
        ><h3>号码明细</h3
        ><el-select
          v-model="detailStatus"
          data-testid="batch-detail-status"
          class="batch-detail-status"
          placeholder="全部状态"
          clearable
          @change="emit('filterDetails')"
          ><el-option
            v-for="option in detailStatusOptions"
            :key="option.value"
            :label="option.label"
            :value="option.value" /></el-select
      ></div>
      <el-table v-loading="detailsLoading" :data="details" row-key="id"
        ><el-table-column label="手机号" min-width="190"
          ><template #default="{ row }"
            ><PhoneReveal
              v-if="canDecrypt"
              :masked="row.phone"
              :reveal="() => revealPhone(row.id)"
              :testid="`batch-phone-decrypt-${row.id}`" /><PhoneMask
              v-else
              :value="row.phone" /></template></el-table-column
        ><el-table-column label="状态" width="100"
          ><template #default="{ row }"
            ><StatusTag :status="row.display_status || row.status" /></template></el-table-column
        ><el-table-column prop="report_desc" label="回执" min-width="150" /><el-table-column
          label="回执时间"
          min-width="178"
          ><template #default="{ row }">{{ formatDateTime(row.report_time) }}</template></el-table-column
        ><template #empty><EmptyState title="没有符合筛选条件的明细" description="调整状态筛选后查看。" /></template
      ></el-table>
      <ListPagination
        v-model:page="detailPage"
        :total="detailTotal"
        :page-size="DEFAULT_PAGE_SIZE"
        unit="条"
        class="query-pagination batch-detail-pagination"
        @change="emit('detailPageChange')"
      ></ListPagination>
    </template>
  </el-drawer>
</template>
