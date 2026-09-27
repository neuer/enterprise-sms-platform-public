<script setup lang="ts">
import { computed } from "vue"

import type { BillingPreview } from "../api/webMessages"
import BillingSegments from "./BillingSegments.vue"
import { formatNumber } from "../lib/format"

export interface RiskLine {
  tone: "warn" | "ok" | "info"
  title: string
  desc: string
}

export interface FinalContentParts {
  sign: string
  body: string
  suffix: string
}

/** 人工发送右栏预检卡组：只展示服务端预检结果（计费/配额口径来自服务端），不参与表单状态。 */
const props = defineProps<{
  preview: BillingPreview | null
  finalParts: FinalContentParts | null
  /** 受理号码数（去重与黑名单剔除后），同时作为计费乘数。 */
  audienceCount: number
  removedDuplicate: number
  /** 粘贴号码在受理时才判定黑名单，此时为 null。 */
  removedBlacklist: number | null
  riskLines: RiskLine[]
}>()

const nextSegmentHint = computed(() => {
  const current = props.preview
  if (!current) return "下一段"
  return `再 ${current.next_segment_at} 字进入第 ${current.segment_parts.length + 1} 段`
})

const quotaUsedPct = computed(() => {
  const quota = props.preview?.quota
  if (!quota || quota.limit <= 0) return 0
  return Math.min(100, (quota.used / quota.limit) * 100)
})
const quotaThisPct = computed(() => {
  const quota = props.preview?.quota
  const cost = props.preview?.quota_cost ?? 0
  if (!quota || quota.limit <= 0) return 0
  return Math.min(Math.max(0, 100 - quotaUsedPct.value), (cost / quota.limit) * 100)
})
const quotaAfterPct = computed(() => {
  const quota = props.preview?.quota
  const cost = props.preview?.quota_cost ?? 0
  if (!quota || quota.limit <= 0) return 0
  return Math.round(((quota.used + cost) / quota.limit) * 100)
})
</script>

<template>
  <section v-if="finalParts" class="rail-card">
    <header>最终内容预览 <small>用户实收</small></header>
    <p class="final-content" data-testid="final-content"
      ><span v-if="finalParts.sign" class="fx-sign">{{ finalParts.sign }}</span
      >{{ finalParts.body }}<span v-if="finalParts.suffix" class="fx-suffix">{{ finalParts.suffix }}</span></p
    >
    <footer class="final-meta">
      <div class="legend">
        <span><i class="g"></i>签名</span>
        <span v-if="finalParts.suffix"><i class="a"></i>退订语 · 服务端自动追加</span>
      </div>
      <span class="mono">{{ preview?.final_length }} 字 · {{ preview?.est_segments }} 计费条</span>
    </footer>
  </section>

  <section class="rail-card">
    <header>受众</header>
    <div class="audience-meter">
      <strong data-testid="recipient-count">{{ formatNumber(audienceCount) }}</strong>
      <span>受理号码（去重与黑名单剔除后）</span>
    </div>
    <div class="removed" data-testid="audience-removed">
      <span
        >重复 <b>{{ formatNumber(removedDuplicate) }}</b></span
      >
      <span v-if="removedBlacklist !== null"
        >黑名单 <b>{{ formatNumber(removedBlacklist) }}</b></span
      >
      <span v-else>黑名单 <small>受理时判定</small></span>
      <span>频控 <small>受理时判定</small></span>
    </div>
  </section>

  <section v-if="preview" class="rail-card">
    <header>计费 <small>services/billing.py 单点口径</small></header>
    <BillingSegments :parts="preview.segment_parts" :next-hint="nextSegmentHint" />
    <div class="cost-line">
      <span class="fx">{{ formatNumber(audienceCount) }} 号码 × {{ preview.est_segments }} 计费条 =</span>
      <strong>{{ formatNumber(preview.quota_cost) }}<small>计费条</small></strong>
    </div>
    <p class="boundary"
      >第 {{ preview.segment_parts.length }} 段已用 {{ preview.segment_parts.at(-1)?.used }}/{{
        preview.segment_parts.at(-1)?.capacity
      }}
      字，再增加 {{ preview.next_segment_at }} 字进入第 {{ preview.segment_parts.length + 1 }} 段。</p
    >
  </section>

  <section v-if="preview && preview.quota" class="rail-card">
    <header>部门日配额</header>
    <div class="quota-row">
      <span>今日已用 / 上限</span>
      <b>{{
        preview.quota.limit > 0
          ? `${formatNumber(preview.quota.used)} / ${formatNumber(preview.quota.limit)}`
          : `${formatNumber(preview.quota.used)} / 不限`
      }}</b>
    </div>
    <template v-if="preview.quota.limit > 0">
      <div class="quota-bar"
        ><i class="used" :style="{ width: `${quotaUsedPct}%` }"></i
        ><i class="this" :style="{ width: `${quotaThisPct}%` }"></i
      ></div>
      <div class="quota-foot">
        <span>斜纹 = 本批预扣 {{ formatNumber(preview.quota_cost) }}</span>
        <span
          >提交后 {{ formatNumber(preview.quota.used + preview.quota_cost) }} /
          {{ formatNumber(preview.quota.limit) }}（{{ quotaAfterPct }}%）</span
        >
      </div>
    </template>
    <p v-else class="quota-foot">上限不限；本批预扣 {{ formatNumber(preview.quota_cost) }} 计费条</p>
  </section>
  <section v-else-if="preview" class="rail-card quota-degraded">
    <header>部门日配额</header>
    <p><b>配额投影暂不可确认。</b>用量账本重建中，预览不阻断；提交时以发送入口判定为准。</p>
  </section>

  <section v-if="riskLines.length" class="rail-card">
    <header>风险与合规</header>
    <div class="risk-lines">
      <div v-for="(line, index) in riskLines" :key="index" class="risk-line" :class="line.tone">
        <b>{{ line.title }}</b>
        <small>{{ line.desc }}</small>
      </div>
    </div>
  </section>
</template>
