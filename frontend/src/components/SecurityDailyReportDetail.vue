<script setup lang="ts">
import { computed } from "vue"

import type { SecurityDailyReport } from "../api/securityDaily"
import {
  displayMoment,
  displayPeriod,
  generationLabel,
  priorityLabels,
  priorityType,
  reportDeliveryLabel,
  reportDeliveryTagType,
  statusLabel,
  tagType,
} from "../lib/securityDailyDisplay"

/** 安全日报详情抽屉正文：只读展示脱敏结构化报告并发出预览/投递意图，加载、确认与代际守卫仍在页面。 */
const props = defineProps<{
  selected: SecurityDailyReport
  previewLoading: boolean
  delivering: boolean
  /** 页面按当前概览配置状态与日报状态判定。 */
  canSend: boolean
  canRetry: boolean
}>()

const emit = defineEmits<{
  preview: []
  deliver: [action: "send" | "retry"]
}>()

const coverageGaps = computed(
  () => props.selected.payload?.coverage.filter((item) => item.tone !== "good" || item.status !== "完整") ?? [],
)
</script>

<template>
  <div class="security-detail-heading">
    <div>
      <span class="security-section-label">{{ selected.report_date }}</span>
      <h2>{{ statusLabel(selected.status) }}</h2>
      <div class="security-detail-tags">
        <el-tag :type="selected.generation_source === 'manual' ? 'warning' : 'info'" size="small">{{
          selected.generation_source === "manual" ? "手动生成" : "自动生成"
        }}</el-tag>
        <el-tag :type="tagType(selected.generation_status)" size="small">{{
          generationLabel(selected.generation_status)
        }}</el-tag>
        <el-tag :type="reportDeliveryTagType(selected)" size="small">{{ reportDeliveryLabel(selected) }}</el-tag>
      </div>
      <p class="security-detail-period"
        >统计窗口 <time>{{ displayPeriod(selected.period_start, selected.period_end) }}</time></p
      >
    </div>
    <div class="security-detail-actions"
      ><el-button plain :loading="previewLoading" @click="emit('preview')">安全预览</el-button
      ><el-button v-if="canSend" type="primary" :loading="delivering" @click="emit('deliver', 'send')"
        >手动投递</el-button
      ><el-button v-if="canRetry" type="warning" :loading="delivering" @click="emit('deliver', 'retry')"
        >重试投递</el-button
      ></div
    >
  </div>
  <el-alert
    v-if="!selected.payload"
    title="数据不可用：当前没有可展示的脱敏结构化报告，不生成或投递邮件。"
    type="warning"
    show-icon
    :closable="false"
  />
  <section class="security-detail-section">
    <div class="section-heading"><span class="security-section-label">状态信息</span></div>
    <dl class="security-delivery-facts">
      <div
        ><dt>生成时间</dt
        ><dd
          ><time>{{ displayMoment(selected.generated_at) }}</time></dd
        ></div
      >
      <div
        ><dt>投递时间</dt
        ><dd
          ><time>{{ displayMoment(selected.delivered_at) }}</time></dd
        ></div
      >
      <div
        ><dt>收件人数</dt><dd>{{ selected.recipient_count }} 人</dd></div
      >
      <div
        ><dt>重试次数</dt><dd>{{ selected.retry_count }} 次</dd></div
      >
      <div v-if="selected.last_error" class="security-error-cell">
        <dt>最新错误</dt>
        <dd class="security-error"
          >{{ selected.last_error
          }}<time v-if="selected.last_error_at"> · {{ displayMoment(selected.last_error_at) }}</time></dd
        >
      </div>
    </dl>
  </section>
  <template v-if="selected.payload">
    <section class="security-detail-summary"
      ><span class="security-section-label">管理摘要</span><p>{{ selected.payload.summary }}</p
      ><p v-if="selected.payload.pending_confirmation" class="security-pending"
        >待确认：{{ selected.payload.pending_confirmation }}</p
      ></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"
        ><span class="security-section-label">核心指标</span
        ><strong>{{ selected.payload.metrics.length }} 项指标</strong></div
      ><div v-if="selected.payload.metrics.length" class="security-metric-grid"
        ><article
          v-for="metric in selected.payload.metrics"
          :key="metric.label"
          :class="['security-metric', metric.tone]"
          ><span>{{ metric.label }}</span
          ><strong>{{ metric.value }}</strong
          ><small>{{ metric.note }}</small></article
        ></div
      ><p v-else class="security-section-empty">该小节无记录</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">SSH 与主机安全</span></div
      ><div v-if="selected.payload.ssh.length" class="security-evidence-list"
        ><div v-for="row in selected.payload.ssh" :key="`ssh-${row.label}`"
          ><strong>{{ row.label }}</strong
          ><span>{{ row.value }}</span
          ><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div
        ></div
      ><p v-else class="security-section-empty">该证据面无记录</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">Web / API</span></div
      ><div v-if="selected.payload.web.length" class="security-evidence-list"
        ><div v-for="row in selected.payload.web" :key="`web-${row.label}`"
          ><strong>{{ row.label }}</strong
          ><span>{{ row.value }}</span
          ><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div
        ></div
      ><p v-else class="security-section-empty">该证据面无记录</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">管理审计</span></div
      ><div v-if="selected.payload.audit.length" class="security-evidence-list"
        ><div v-for="row in selected.payload.audit" :key="`audit-${row.time}-${row.action}`" class="security-audit-row"
          ><strong>{{ row.time }} · {{ row.actor }}</strong
          ><span>{{ row.source_ip }} · {{ row.action }}</span
          ><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div
        ></div
      ><p v-else class="security-section-empty">该证据面无记录</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">运行状态</span></div
      ><div v-if="selected.payload.runtime.length" class="security-evidence-list"
        ><div v-for="row in selected.payload.runtime" :key="`runtime-${row.label}`"
          ><strong>{{ row.label }}</strong
          ><span>{{ row.value }}</span
          ><el-tag :type="tagType(row.tone)" size="small">{{ row.assessment }}</el-tag></div
        ></div
      ><p v-else class="security-section-empty">该证据面无记录</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">建议处置</span></div
      ><div v-if="selected.payload.actions.length" class="security-action-list"
        ><article v-for="action in selected.payload.actions" :key="action.title"
          ><el-tag :type="priorityType(action.priority)" size="small">{{ priorityLabels[action.priority] }}</el-tag
          ><div
            ><strong>{{ action.title }}</strong
            ><p>{{ action.detail }}</p></div
          ></article
        ></div
      ><p v-else class="security-section-empty">无需处置事项</p></section
    >
    <section class="security-detail-section"
      ><div class="section-heading"><span class="security-section-label">证据范围</span></div
      ><el-alert v-if="coverageGaps.length" title="存在日志覆盖缺口" type="warning" show-icon :closable="false"
        ><template #default
          ><div v-for="item in coverageGaps" :key="`gap-${item.source}`"
            >{{ item.source }}：{{ item.note }}</div
          ></template
        ></el-alert
      ><div v-if="selected.payload.coverage.length" class="security-evidence-list"
        ><div v-for="item in selected.payload.coverage" :key="item.source"
          ><strong>{{ item.source }}</strong
          ><span>{{ item.window }} · {{ item.note }}</span
          ><el-tag :type="tagType(item.tone)" size="small">{{ item.status }}</el-tag></div
        ></div
      ><p v-else class="security-section-empty">该证据面无记录</p></section
    >
  </template>
  <section v-if="selected.timeline.length" class="security-detail-section"
    ><div class="section-heading"
      ><span class="security-section-label">{{
        selected.generation_status === "unavailable" || selected.generation_status === "failed"
          ? "状态时间线"
          : "投递时间线"
      }}</span></div
    ><ol class="security-timeline"
      ><li v-for="event in selected.timeline" :key="`${event.type}-${event.at}`"
        ><time>{{ displayMoment(event.at) }}</time
        ><strong>{{ event.label }}</strong
        ><span v-if="event.detail">{{ event.detail }}</span></li
      ></ol
    ></section
  >
</template>
