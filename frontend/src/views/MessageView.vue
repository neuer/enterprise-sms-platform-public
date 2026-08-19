<script setup lang="ts">
import "../styles/workspace.css"

import { computed, ref } from "vue"

import { ElMessage } from "element-plus"

import PhoneMask from "../components/PhoneMask.vue"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"
import {
  decryptMessagePhone,
  getTimeline,
  searchMessages,
  type MessageItem,
  type PhoneBadge,
  type TimelineEvent,
  type TimelineResult,
} from "../api/queries"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
const phone = ref("")
const range = ref<[Date, Date] | null>(null)
const category = ref("")
const status = ref("")
const mode = ref<"list" | "timeline">("list")
const items = ref<MessageItem[]>([])
const timeline = ref<TimelineResult | null>(null)
const badge = ref<PhoneBadge | null>(null)
const total = ref(0)
const page = ref(1)
const searched = ref(false)
const searchedPhone = ref("")
const searchedMask = ref("")
const loading = ref(false)
const revealing = ref(false)
const errorMessage = ref("")
const revealedPhone = ref("")
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")

const viewOptions = [
  { label: "列表", value: "list" },
  { label: "时间线", value: "timeline" },
]
const categoryOptions = [
  { value: "verify", label: "验证码" },
  { value: "notice", label: "通知" },
  { value: "market", label: "营销" },
]
const statusOptions = [
  { value: "pending", label: "待发送" },
  { value: "sent", label: "已提交" },
  { value: "delivered", label: "已送达" },
  { value: "failed", label: "失败" },
  { value: "unknown", label: "未知" },
  { value: "other", label: "其他" },
]

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

const groupedEvents = computed(() => {
  const groups = new Map<string, TimelineEvent[]>()
  for (const event of timeline.value?.events || []) {
    const day = formatTime(event.ts).slice(0, 10)
    groups.set(day, [...(groups.get(day) || []), event])
  }
  return [...groups.entries()].map(([day, events]) => ({
    day,
    weekday: WEEKDAYS[new Date(`${day}T00:00:00+08:00`).getDay()],
    events,
  }))
})

const categoryLabel: Record<string, string> = { verify: "验证码", notice: "通知", market: "营销" }
const statusLabel: Record<string, string> = { delivered: "已送达", failed: "失败", unknown: "未知", sent: "已提交", pending: "待发送", other: "其他" }
const blacklistSourceLabel: Record<string, string> = { manual: "手工添加", reply_optout: "用户退订", import: "文件导入" }

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

function reportTip(item: MessageItem): string {
  return item.report_time ? `厂商回报 ${formatTime(item.report_time)}` : "厂商回报描述"
}

function showReport(item: MessageItem): boolean {
  return Boolean(item.report_desc) && (item.status === "failed" || item.status === "unknown")
}

let runToken = 0

async function run(): Promise<void> {
  const token = ++runToken
  loading.value = true
  errorMessage.value = ""
  try {
    const start = range.value?.[0].toISOString()
    const end = range.value?.[1].toISOString()
    if (mode.value === "list") {
      const result = await searchMessages(phone.value, {
        start,
        end,
        category: category.value || undefined,
        status: status.value || undefined,
        page: page.value,
      })
      if (token !== runToken) return
      items.value = result.items
      total.value = result.total
      badge.value = result.badge
      timeline.value = null
    } else {
      const next = await getTimeline(phone.value, start, end)
      if (token !== runToken) return
      timeline.value = next
      badge.value = next.badge
      items.value = []
      total.value = next.events.length
    }
  } catch (error) {
    if (token !== runToken) return
    items.value = []
    timeline.value = null
    badge.value = null
    total.value = 0
    errorMessage.value = error instanceof Error ? error.message : "号码查询失败"
  } finally {
    if (token === runToken) loading.value = false
  }
}

function search(): void {
  if (!/^1\d{10}$/.test(phone.value)) { errorMessage.value = "请输入 11 位手机号"; return }
  page.value = 1
  searched.value = true
  searchedPhone.value = phone.value
  searchedMask.value = `${phone.value.slice(0, 3)}****${phone.value.slice(-4)}`
  revealedPhone.value = ""
  void run()
}

function applyFilters(): void {
  if (!searched.value) return
  page.value = 1
  void run()
}

function changePage(next: number): void {
  page.value = next
  void run()
}

function switchMode(next: string | number): void {
  const value = next === "timeline" ? "timeline" : "list"
  if (value === mode.value) return
  mode.value = value
  page.value = 1
  if (searched.value) {
    void run()
  } else if (/^1\d{10}$/.test(phone.value)) {
    search()
  }
}

async function revealSearched(): Promise<void> {
  if (revealing.value) return
  revealing.value = true
  errorMessage.value = ""
  try {
    let messageId = items.value[0]?.id
    if (messageId === undefined) {
      const firstPage = await searchMessages(searchedPhone.value, { page: 1 })
      messageId = firstPage.items[0]?.id
    }
    if (messageId === undefined) {
      errorMessage.value = "该号码没有可用于授权查看的消息记录"
      return
    }
    const result = await decryptMessagePhone(messageId)
    revealedPhone.value = result.phone
    ElMessage.success("已记入审计")
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "授权查看失败"
  } finally {
    revealing.value = false
  }
}
</script>

<template>
  <section class="page-heading message-heading">
    <div><p class="eyebrow">PHONE TRACE / 号码轨迹</p><h1>号码搜索</h1><p>手机号仅用于内存 HMAC 精确匹配；默认返回掩码，授权查看逐条审计。</p></div>
  </section>

  <el-card shadow="never" class="message-search-card">
    <el-form class="message-search filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-4" label="手机号精确查询"><el-input v-model="phone" placeholder="输入 11 位手机号" maxlength="11" inputmode="numeric" clearable /></el-form-item>
      <el-form-item class="filter-span-4" label="时间范围（可选）"><el-date-picker v-model="range" type="datetimerange" popper-class="qingluan-date-popper" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /></el-form-item>
      <el-form-item class="filter-span-2" label="视图"><el-segmented :model-value="mode" :options="viewOptions" data-testid="message-view-switch" aria-label="查询视图" @change="switchMode" /></el-form-item>
      <el-form-item class="query-filter-actions filter-actions filter-span-2"><el-button type="primary" native-type="submit" :loading="loading">查询</el-button></el-form-item>
    </el-form>
    <p class="query-privacy-note">查询参数不会进入 Nginx/Uvicorn 访问日志；服务端仅向 SQL 传递 HMAC 候选。</p>
  </el-card>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

  <el-card v-if="badge" shadow="never" class="phone-badge-card">
    <div class="phone-badge">
      <div class="phone-badge-num">
        <small>当前号码{{ revealedPhone ? " · 已解密" : "" }}</small>
        <strong v-if="revealedPhone" class="revealed-phone">{{ revealedPhone }}</strong>
        <PhoneMask v-else :value="searchedMask" />
      </div>
      <el-tag :type="badge.blacklisted ? 'danger' : 'info'" :effect="badge.blacklisted ? 'dark' : 'plain'" class="phone-badge-tag">{{ badge.blacklisted ? "已在黑名单" : "未在黑名单" }}</el-tag>
      <span v-if="badge.blacklisted && badge.blacklist_source" class="phone-badge-source">来源 {{ blacklistSourceLabel[badge.blacklist_source] || badge.blacklist_source }}</span>
      <span class="phone-badge-sep" aria-hidden="true"></span>
      <div class="phone-badge-stat"><span>近30日接收</span><strong>{{ badge.recv_30d }} 条</strong></div>
      <div v-if="canDecrypt" class="phone-badge-reveal">
        <small v-if="revealedPhone">解密明文仅存页面内存，刷新即失效</small>
        <el-button v-else link type="primary" :loading="revealing" data-testid="message-phone-reveal" @click="revealSearched">授权查看</el-button>
      </div>
    </div>
  </el-card>

  <el-card v-if="mode === 'list'" shadow="never" class="query-table-card message-results">
    <el-table v-loading="loading" :data="items" row-key="id" class="query-table">
      <el-table-column label="时间 / 批次" min-width="205"><template #default="{ row }"><time>{{ formatTime(row.created_at) }}</time><code class="batch-code cell-sub">{{ row.batch_no }}</code></template></el-table-column>
      <el-table-column label="类别" width="90"><template #default="{ row }"><span :class="['category-mark', row.category]">{{ categoryLabel[row.category] || row.category }}</span></template></el-table-column>
      <el-table-column label="内容摘要" min-width="280"><template #default="{ row }"><p class="message-content">{{ row.content }}</p></template></el-table-column>
      <el-table-column label="状态" width="150"><template #default="{ row }"><StatusTag :status="row.status" :label="statusLabel[row.status] || row.status" /><p v-if="showReport(row)" class="report-desc" :title="reportTip(row)">{{ row.report_desc }}</p></template></el-table-column>
      <el-table-column label="提交方" min-width="120"><template #default="{ row }">{{ row.sender || '—' }}</template></el-table-column>
      <template #empty><EmptyState :title="searched ? '未找到符合条件的记录' : '尚未查询号码记录'" :description="searched ? '可调整类别、状态或时间范围后重试。' : '输入完整手机号后查询跨批次收发轨迹。'" /></template>
    </el-table>
    <div class="query-mobile-list"><article v-for="item in items" :key="item.id"><header><span :class="['category-mark', item.category]">{{ categoryLabel[item.category] || item.category }}</span><StatusTag :status="item.status" :label="statusLabel[item.status] || item.status" /></header><p>{{ item.content }}</p><p v-if="showReport(item)" class="report-desc">{{ item.report_desc }}</p><footer><time>{{ formatTime(item.created_at) }}</time><code>{{ item.batch_no }}</code></footer></article></div>
    <footer v-if="searched" class="query-pagination">
      <span>共 {{ total }} 条记录</span>
      <span class="result-filters">
        <el-select v-model="category" data-testid="message-category-filter" placeholder="全部类别" clearable size="small" style="width: 118px" @change="applyFilters"><el-option v-for="option in categoryOptions" :key="option.value" :value="option.value" :label="option.label" /></el-select>
        <el-select v-model="status" data-testid="message-status-filter" placeholder="全部状态" clearable size="small" style="width: 118px" @change="applyFilters"><el-option v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" /></el-select>
      </span>
      <el-pagination v-if="total > 20" v-model:current-page="page" data-testid="message-pagination" :page-size="20" :total="total" layout="prev, pager, next" @current-change="changePage" />
    </footer>
  </el-card>

  <section v-else class="timeline-panel" v-loading="loading">
    <p v-if="timeline?.truncated" class="timeline-truncated">事件过多，仅显示最近 500 条；缩小时间范围可查看完整轨迹。</p>
    <EmptyState v-if="!timeline?.events.length" :title="searched ? '该号码在所选条件下没有事件' : '尚未生成号码时间线'" :description="searched ? '可调整时间范围后重试。' : '输入完整手机号后，下行与用户回复会按日期排列。'" />
    <section v-for="group in groupedEvents" :key="group.day" class="timeline-day"><h2>{{ group.day }}<span class="timeline-day-meta">{{ group.weekday }} · {{ group.events.length }} 事件</span></h2><article v-for="event in group.events" :key="`${event.ts}-${event.direction}-${event.content}`" :class="['timeline-event', event.direction === 'in' ? 'incoming' : event.category]"><div class="timeline-dot"></div><header><span v-if="event.direction === 'out'" :class="['category-mark', event.category || '']">{{ categoryLabel[event.category || ''] || '平台下行' }}</span><strong v-else>↩ 用户回复</strong><StatusTag v-if="event.status" :status="event.status" :label="statusLabel[event.status] || event.status" /><time>{{ formatTime(event.ts).slice(11) }}</time></header><p>{{ event.content }}</p><footer><code v-if="event.batch_no">{{ event.batch_no }}</code><span>{{ event.sender }}</span></footer></article></section>
  </section>
</template>
