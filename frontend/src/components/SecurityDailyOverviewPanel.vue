<script setup lang="ts">
import { computed } from "vue"

import type { SecurityDailyOverview } from "../api/securityDaily"
import {
  configurationLabel,
  configurationMessage,
  configurationTagLabel,
  configurationTagType,
  deliveryLabel,
  displayMoment,
} from "../lib/securityDailyDisplay"

/** 安全日报概览：运行状态横条与事实带，纯展示；概览加载与失败提示仍在页面。 */
const props = defineProps<{
  overview: SecurityDailyOverview
}>()

const overviewDeliveryStatusLabel = computed(() =>
  props.overview.delivery_status ? deliveryLabel(props.overview.delivery_status) : "尚未生成",
)
</script>

<template>
  <section class="security-daily-overview" aria-label="安全日报概览">
    <div class="security-daily-state">
      <span class="security-section-label">安全日报运行状态</span>
      <strong>{{ configurationLabel(overview.configuration_state) }}</strong>
      <el-tag :type="configurationTagType(overview.configuration_state)" size="small">
        {{ configurationTagLabel(overview.configuration_state) }}
      </el-tag>
      <p>{{ overview.period_description }} · {{ configurationMessage(overview.configuration_state) }}</p>
      <em v-if="overview.next_scheduled_at">下次 {{ displayMoment(overview.next_scheduled_at) }}</em>
    </div>
    <dl class="security-daily-facts">
      <div
        ><dt>调度</dt><dd>{{ overview.schedule_time }} · {{ overview.timezone }}</dd></div
      >
      <div
        ><dt>收件人数</dt><dd>{{ overview.recipient_count }} 人（只展示数量）</dd></div
      >
      <div
        ><dt>发件域名</dt><dd>{{ overview.sender_domain }} / {{ overview.sender_address }}</dd></div
      >
      <div
        ><dt>最近日报状态</dt><dd>{{ overviewDeliveryStatusLabel }}</dd></div
      >
      <div
        ><dt>最近成功生成</dt><dd>{{ displayMoment(overview.last_generated_at) }}</dd></div
      >
      <div
        ><dt>最近成功投递</dt><dd>{{ displayMoment(overview.last_delivered_at) }}</dd></div
      >
      <div
        ><dt>最近失败</dt><dd>{{ overview.latest_failure ?? "—" }}</dd></div
      >
    </dl>
  </section>
</template>
