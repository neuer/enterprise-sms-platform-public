<script setup lang="ts">
import { computed } from "vue"

import type { ManagedApp } from "../api/apps"
import type { SmsSign } from "../api/signs"
import StatusTag from "./StatusTag.vue"
import { VENDOR_REVIEW_LABELS } from "../lib/labels"
import { canAdoptSign, canDeleteSign, canEditSign, canSyncSign } from "../lib/signDisplay"

interface TrailStep {
  title: string
  desc: string
  tone: "done" | "wait" | "fail"
}

/**
 * 签名详情抽屉：只负责展示与发出操作意图；详情选择、默认签名引用联查、
 * 同步/关联/删除的确认与在途守卫仍由签名管理页统一处理。
 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{
  sign: SmsSign | null
  canWrite: boolean
  isAdmin: boolean
  /** 把该签名设为默认签名的应用（含停用），由页面经 GET /admin/apps 联查。 */
  signApps: ManagedApp[]
  signAppsLoading: boolean
  signAppsError: boolean
  syncingId: number | null
  removingId: number | null
}>()
const emit = defineEmits<{
  edit: []
  sync: [sign: SmsSign]
  adopt: [sign: SmsSign]
  remove: [sign: SmsSign]
}>()

const detail = computed(() => props.sign)
const canSync = canSyncSign
const canAdopt = (item: SmsSign) => canAdoptSign(item, props.canWrite)
const canEdit = canEditSign
const canDelete = canDeleteSign

function stateLabel(state: SmsSign["vendor_state"]): string {
  return VENDOR_REVIEW_LABELS[state]
}

/** 详情抽屉标题副行：平台编号 / 已绑定厂商编号，缺项省略。 */
function detailHeadMeta(item: SmsSign): string {
  const parts = [`平台 #${item.id}`]
  if (item.vendor_sign_id) parts.push(`厂商 #${item.vendor_sign_id}`)
  return parts.join(" · ")
}

/** 详情抽屉审核轨迹：三态全部由 vendor_sign_id 与 vendor_state 前端推导。 */
const detailTrail = computed<TrailStep[]>(() => {
  const item = detail.value
  if (!item) return []
  const bound = item.vendor_sign_id !== null
  const result: TrailStep =
    item.vendor_state === "approved"
      ? { title: "厂商审核：已通过", desc: "GetSignState 同步", tone: "done" }
      : item.vendor_state === "rejected"
        ? { title: "厂商审核：已拒绝", desc: "GetSignState 同步", tone: "fail" }
        : {
            title: "厂商审核：等待结果",
            desc: bound ? "轮询同步 · 可手动同步" : "等待厂商绑定",
            tone: "wait",
          }
  return [
    { title: "已提交送审", desc: "Outbox → realtime worker", tone: "done" },
    {
      title: bound ? "已绑定厂商编号" : "等待厂商绑定",
      desc: bound ? `#${item.vendor_sign_id}` : "BindSign",
      tone: bound ? "done" : "wait",
    },
    result,
  ]
})
</script>

<template>
  <el-drawer v-model="open" class="sign-drawer" size="min(560px, 94vw)" :teleported="false">
    <template #header>
      <div v-if="detail" class="sign-drawer-head">
        <div class="sign-drawer-title">
          <StatusTag :status="detail.vendor_state" :label="stateLabel(detail.vendor_state)" />
          <b>【{{ detail.name }}】</b>
        </div>
        <code>{{ detailHeadMeta(detail) }}</code>
      </div>
    </template>
    <template v-if="detail">
      <div class="sign-trail" data-testid="sign-trail">
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
        description="修改名称后重新提交，将生成新的厂商编号并重新进入审核。"
        type="error"
        :closable="false"
        class="sign-reject-banner"
      />
      <div class="sign-content-block">
        <span>发送效果预览（签名自动拼接于内容最前）</span>
        <p class="sign-preview-text">【{{ detail.name }}】您的验证码为 123456，5 分钟内有效，请勿泄露。</p>
        <small class="sign-proof-hint"
          >计费长度含签名：本签名 +{{ detail.name.length + 2 }} 字（含方括号）· 含签名与退订语 ≤70 字计 1 条，唯一实现
          services/billing.py</small
        >
      </div>
      <dl class="sign-fact-grid">
        <div
          ><dt>平台编号</dt><dd class="mono-id">{{ detail.id }}</dd></div
        >
        <div
          ><dt>厂商编号</dt><dd class="mono-id">{{ detail.vendor_sign_id || "—" }}</dd></div
        >
        <div
          ><dt>规范签名</dt><dd>【{{ detail.name }}】</dd></div
        >
        <div
          ><dt>计入计费长度</dt><dd class="mono-id">+{{ detail.name.length + 2 }} 字</dd></div
        >
      </dl>
      <div v-if="isAdmin" class="sign-content-block" data-testid="sign-apps">
        <span>默认签名引用（仅 admin 可见）</span>
        <div class="sign-apps">
          <span v-if="signAppsLoading" class="sign-apps-note">加载中…</span>
          <span v-else-if="signAppsError" class="sign-apps-note">应用引用信息加载失败</span>
          <template v-else>
            <span v-for="app in signApps" :key="app.id" class="sign-app-chip"
              >{{ app.name }}<template v-if="app.status === 0">（已停用）</template></span
            >
            <span v-if="!signApps.length" class="sign-apps-note">暂无应用把该签名设为默认签名</span>
          </template>
        </div>
        <small class="sign-proof-hint"
          >来自 GET /admin/apps 现有 default_sign 字段前端联查，不新增接口；停用应用同样计入删除约束。</small
        >
      </div>
      <div v-if="canWrite" class="sign-detail-actions">
        <el-button v-if="canEdit(detail)" data-testid="sign-detail-edit" type="primary" @click="emit('edit')"
          >修改并重新提交</el-button
        >
        <el-button
          v-if="canSync(detail)"
          data-testid="sign-detail-sync"
          :loading="syncingId === detail.id"
          @click="emit('sync', detail)"
          >同步审核状态</el-button
        >
        <el-button v-if="canAdopt(detail)" data-testid="sign-detail-adopt" type="primary" @click="emit('adopt', detail)"
          >关联已有签名</el-button
        >
        <el-button
          v-if="canDelete(detail)"
          data-testid="sign-detail-delete"
          link
          type="danger"
          :loading="removingId === detail.id"
          :disabled="removingId !== null"
          @click="emit('remove', detail)"
          >删除签名</el-button
        >
        <p class="why">已通过审核的签名不可编辑或删除，但仍可同步厂商状态；已被应用或批次引用的驳回签名请新建。</p>
      </div>
    </template>
  </el-drawer>
</template>
