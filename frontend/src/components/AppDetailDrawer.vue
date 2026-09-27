<script setup lang="ts">
import { computed } from "vue"

import type { ManagedApp } from "../api/apps"
import {
  categoriesText,
  freqOverrideText,
  graceHoursLeft,
  quotaPercent as quotaPercentOf,
  quotaTone as quotaToneOf,
} from "../lib/appDisplay"
import { formatNumber, formatPercent } from "../lib/format"
import { formatDateTime } from "../lib/time"

/**
 * 应用详情抽屉：只负责展示与发出操作意图；密钥轮换、作废、启停用的确认、
 * 在途守卫与一次性密钥展示仍由应用管理页统一处理。
 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{
  app: ManagedApp | null
  /** 今日消耗（计费条）；用量联查不可用时为 null。 */
  consumed: number | null
  /** 服务端口径成功率（services/stats.py）；无数据为 null。 */
  rate: number | null
  usageUnavailable: boolean
  rotatingKeyId: number | null
  revokingKeyId: number | null
  rotatingCallbackId: number | null
  statusBusyId: number | null
  secretBusy: boolean
}>()
const emit = defineEmits<{
  edit: []
  demo: [app: ManagedApp]
  rotateKey: [app: ManagedApp]
  revokeKey: [app: ManagedApp]
  rotateCallback: [app: ManagedApp]
  disable: [app: ManagedApp]
  enable: [app: ManagedApp]
}>()

const detail = computed(() => props.app)
const percent = computed(() => (props.app ? quotaPercentOf(props.consumed, props.app.daily_quota) : null))
const tone = computed(() => quotaToneOf(percent.value))
const rateText = computed(() =>
  props.rate === null ? "—" : `${formatPercent(props.rate)}（送达 /（送达 + 失败），未知不入分母）`,
)
</script>

<template>
  <el-drawer v-model="open" class="apps-drawer apps-detail-drawer" size="min(560px, 92vw)" :teleported="false">
    <template #header>
      <div v-if="detail" class="apps-drawer-head">
        <div class="apps-drawer-title">
          <el-tag :type="detail.status ? 'success' : 'info'">{{ detail.status ? "启用" : "停用" }}</el-tag>
          <b>{{ detail.name }}</b>
        </div>
        <code>#{{ detail.id }} · {{ detail.dept }} · 创建于 {{ formatDateTime(detail.created_at) }}</code>
      </div>
    </template>
    <template v-if="detail">
      <section class="app-sec">
        <h3>运行概览 · 今日<small>统计口径 services/stats.py</small></h3>
        <div class="apps-hero">
          <div class="apps-hero-nums">
            <template v-if="!usageUnavailable">
              <b>{{ formatNumber(consumed ?? 0) }}</b>
              <span
                >/ {{ detail.daily_quota === 0 ? "不限量" : formatNumber(detail.daily_quota) }} 计费条 · 成功率
                {{ rateText }}</span
              >
            </template>
            <template v-else>
              <b>—</b>
              <span>今日用量统计暂不可用</span>
            </template>
          </div>
          <span v-if="percent !== null" class="apps-quota-bar">
            <i :class="tone" :style="{ width: `${percent}%` }"></i>
          </span>
        </div>
        <dl class="apps-fact-grid">
          <div
            ><dt>每分钟限流</dt><dd class="apps-mono">{{ formatNumber(detail.rate_limit_per_min) }} 次</dd></div
          >
          <div
            ><dt>频控覆盖</dt><dd>{{ freqOverrideText(detail) }}</dd></div
          >
        </dl>
      </section>

      <section class="app-sec">
        <h3>密钥与回调<small>明文仅创建/轮换当次展示</small></h3>
        <div class="apps-key-line">
          <code v-if="detail.status === 1">{{ detail.api_key_prefix }}••••</code>
          <code v-else>已随停用吊销</code>
          <small>当前 API Key</small>
          <span class="apps-key-act">
            <el-button
              v-if="detail.status === 1"
              :data-testid="`rotate-key-${detail.id}`"
              link
              type="primary"
              :loading="rotatingKeyId === detail.id"
              :disabled="secretBusy"
              @click="emit('rotateKey', detail)"
              >轮换 Key</el-button
            >
          </span>
        </div>
        <div v-if="detail.old_key_prefix && detail.old_key_expires_at" class="apps-key-grace">
          <code>{{ detail.old_key_prefix }}••••</code>
          <small
            >旧 Key 宽限期至 {{ formatDateTime(detail.old_key_expires_at) }}（余
            {{ graceHoursLeft(detail) }}h），到期自动失效</small
          >
          <span class="apps-key-act">
            <el-button
              :data-testid="`revoke-old-key-${detail.id}`"
              link
              type="danger"
              :loading="revokingKeyId === detail.id"
              :disabled="revokingKeyId !== null"
              @click="emit('revokeKey', detail)"
              >立即作废</el-button
            >
          </span>
        </div>
        <dl class="apps-fact-grid">
          <div class="full">
            <dt>回调 URL（内网白名单校验 · 生产仅 HTTPS）</dt>
            <dd class="apps-mono">{{ detail.callback_url || "未配置" }}</dd>
          </div>
          <div>
            <dt>明细回调</dt>
            <dd>{{ detail.callback_url ? (detail.callback_report_enabled ? "开启" : "关闭") : "—" }}</dd>
          </div>
          <div>
            <dt>回调密钥</dt>
            <dd>
              {{ detail.callback_secret_configured ? "已配置" : "未配置" }}
              <el-button
                :data-testid="`rotate-callback-${detail.id}`"
                link
                type="primary"
                :loading="rotatingCallbackId === detail.id"
                :disabled="secretBusy"
                @click="emit('rotateCallback', detail)"
                >轮换回调密钥</el-button
              >
            </dd>
          </div>
        </dl>
      </section>

      <section class="app-sec">
        <h3>策略</h3>
        <dl class="apps-fact-grid">
          <div
            ><dt>允许类别</dt><dd>{{ categoriesText(detail) }}</dd></div
          >
          <div
            ><dt>默认签名</dt><dd>{{ detail.default_sign ? `【${detail.default_sign}】` : "未设置" }}</dd></div
          >
          <div
            ><dt>黑名单检查</dt><dd>{{ detail.blacklist_check ? "开启" : "关闭" }}</dd></div
          >
          <div
            ><dt>营销 API 大批量</dt><dd>{{ detail.allow_market_api_bulk ? "已预授权" : "未预授权" }}</dd></div
          >
          <div>
            <dt>来源 IP 白名单</dt>
            <dd class="apps-mono">{{
              detail.allowed_ips.length ? `${detail.allowed_ips.length} 条 CIDR` : "全网放行"
            }}</dd>
          </div>
        </dl>
      </section>
    </template>
    <template #footer>
      <div v-if="detail" class="apps-drawer-foot">
        <el-button :data-testid="`edit-app-${detail.id}`" @click="emit('edit')">编辑配置</el-button>
        <el-button :data-testid="`demo-script-${detail.id}`" @click="emit('demo', detail)">接入示例</el-button>
        <span class="apps-foot-sp"></span>
        <el-button
          v-if="detail.status"
          :data-testid="`disable-app-${detail.id}`"
          type="danger"
          :loading="statusBusyId === detail.id"
          :disabled="statusBusyId !== null"
          @click="emit('disable', detail)"
          >停用应用</el-button
        >
        <el-button
          v-else
          :data-testid="`enable-app-${detail.id}`"
          type="success"
          :loading="statusBusyId === detail.id"
          :disabled="statusBusyId !== null"
          @click="emit('enable', detail)"
          >启用应用</el-button
        >
      </div>
    </template>
  </el-drawer>
</template>
