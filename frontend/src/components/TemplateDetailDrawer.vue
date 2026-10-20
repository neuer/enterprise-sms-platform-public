<script setup lang="ts">
import { computed } from "vue"

import type { SmsTemplate } from "../api/templates"
import { canDelete, canEdit, canSync, canUse, stateLabel } from "../lib/templateDisplay"
import { contentParts, vendorPreviewOf } from "../lib/templatePreview"
import StatusTag from "./StatusTag.vue"

interface TrailStep {
  title: string
  desc: string
  tone: "done" | "wait" | "fail"
}

/**
 * 模板详情抽屉：只负责展示与发出操作意图；同步、删除确认与在途守卫、
 * 编辑表单回填与「用于发送」跳转仍由模板管理页统一处理。
 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{
  template: SmsTemplate | null
  canWrite: boolean
  syncingId: number | null
  removingId: number | null
}>()
const emit = defineEmits<{
  edit: []
  use: [item: SmsTemplate]
  sync: [item: SmsTemplate]
  remove: [item: SmsTemplate]
}>()

const detail = computed(() => props.template)

/** 详情抽屉标题副行：平台编号 / 已绑定厂商编号。 */
function detailHeadMeta(item: SmsTemplate): string {
  const parts = [`平台 #${item.id}`]
  if (item.vendor_template_id) parts.push(`厂商 #${item.vendor_template_id}`)
  return parts.join(" · ")
}

/** 事实格变量声明：{n} ≤max_len 字，按 pos 顺序。 */
function varSpecsLine(specs: { pos: number; max_len: number }[]): string {
  return specs.map((spec) => `{${spec.pos}} ≤${spec.max_len} 字`).join(" · ")
}

/** 详情抽屉审核轨迹：三态全部由 vendor_template_id 与 vendor_state 前端推导。 */
const detailTrail = computed<TrailStep[]>(() => {
  const item = detail.value
  if (!item) return []
  const bound = item.vendor_template_id !== null
  const submitted = item.vendor_state !== "draft"
  const result: TrailStep =
    item.vendor_state === "approved"
      ? { title: "厂商审核：已通过", desc: "GetTemplateState 同步", tone: "done" }
      : item.vendor_state === "rejected"
        ? { title: "厂商审核：已拒绝", desc: "GetTemplateState 同步", tone: "fail" }
        : {
            title: "厂商审核：等待结果",
            desc: bound ? "轮询同步 · 可手动同步" : "等待厂商绑定",
            tone: "wait",
          }
  return [
    {
      title: submitted ? "已提交送审" : "未送审",
      desc: "Outbox → realtime worker",
      tone: submitted ? "done" : "wait",
    },
    {
      title: bound ? "已绑定厂商编号" : "等待厂商绑定",
      desc: bound ? `#${item.vendor_template_id}` : "BindTemplate",
      tone: bound ? "done" : "wait",
    },
    result,
  ]
})
</script>

<template>
  <el-drawer v-model="open" class="template-drawer" size="min(560px, 94vw)" :teleported="false">
    <template #header>
      <div v-if="detail" class="template-drawer-head">
        <div class="template-drawer-title">
          <StatusTag :status="detail.vendor_state" :label="stateLabel(detail.vendor_state)" />
          <b>{{ detail.name }}</b>
        </div>
        <code>{{ detailHeadMeta(detail) }}</code>
      </div>
    </template>
    <template v-if="detail">
      <div class="template-trail" data-testid="template-trail">
        <template v-for="(step, index) in detailTrail" :key="index">
          <span v-if="index > 0" class="trail-connector"></span>
          <span class="trail-step" :class="step.tone">
            <i class="dot"></i>
            <span class="trail-txt"
              ><b>{{ step.title }}</b
              ><small>{{ step.desc }}</small></span
            >
          </span>
        </template>
      </div>
      <el-alert
        v-if="detail.vendor_state === 'rejected' && detail.vendor_reject_reason"
        :title="`厂商驳回原因：${detail.vendor_reject_reason}`"
        description="该模板已绑定厂商编号，不能原地修改；请新建模板并重新提交审核。"
        type="error"
        :closable="false"
        class="template-reject-banner"
      />
      <div class="template-content-block">
        <span>平台格式内容</span>
        <p class="template-inline-content">
          <template v-for="(part, index) in contentParts(detail.content, detail.var_specs)" :key="index">
            <span
              v-if="part.pos !== undefined"
              class="var-chip"
              :title="`变量 {${part.pos}} · 最大 ${part.maxLen ?? '—'} 字`"
              >{{ "{" + part.pos + "}" }}<i>≤{{ part.maxLen ?? "—" }}</i></span
            >
            <template v-else>{{ part.text }}</template>
          </template>
        </p>
        <small class="template-proof-hint">变量 {{ detail.var_specs.length }} 个 · 渲染时校验参数个数与长度</small>
      </div>
      <div class="template-content-block">
        <span>厂商格式（提交时转换）</span>
        <p class="template-vendor-preview">{{ vendorPreviewOf(detail.content, detail.var_specs) }}</p>
      </div>
      <dl class="template-fact-grid">
        <div
          ><dt>平台编号</dt><dd class="mono-id">{{ detail.id }}</dd></div
        >
        <div
          ><dt>厂商编号</dt><dd class="mono-id">{{ detail.vendor_template_id || "—" }}</dd></div
        >
        <div>
          <dt>变量声明</dt>
          <dd class="mono-id">{{ detail.var_specs.length ? varSpecsLine(detail.var_specs) : "无变量" }}</dd>
        </div>
      </dl>
      <div v-if="canWrite" class="template-detail-actions">
        <el-button v-if="canEdit(detail)" data-testid="template-detail-edit" type="primary" @click="emit('edit')"
          >修改并重新提交</el-button
        >
        <el-button v-if="canUse(detail)" data-testid="template-detail-use" type="primary" @click="emit('use', detail)"
          >用于发送</el-button
        >
        <el-button
          v-if="canSync(detail)"
          data-testid="template-detail-sync"
          :loading="syncingId === detail.id"
          @click="emit('sync', detail)"
          >同步审核状态</el-button
        >
        <el-button
          v-if="canDelete(detail)"
          data-testid="template-detail-delete"
          link
          type="danger"
          :loading="removingId === detail.id"
          :disabled="removingId !== null"
          @click="emit('remove', detail)"
          >删除模板</el-button
        >
        <p class="why">已绑定厂商编号的模板不可编辑或删除，但仍可同步厂商状态；被拒绝或撤销后请新建模板。</p>
      </div>
    </template>
  </el-drawer>
</template>
