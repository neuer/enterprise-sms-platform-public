<script setup lang="ts">
import "../styles/workspace.css"

import { computed, ref } from "vue"

import { ElMessage } from "element-plus"

import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import PhoneMask from "../components/PhoneMask.vue"
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
const decryptId = ref<number>()
const loading = ref(false)
const revealing = ref(false)
const errorMessage = ref("")
const revealedPhone = ref("")
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")
const displayMask = computed(() => items.value[0]?.phone || searchedMask.value)

/** 手机号即时校验提示：空或合法为 undefined，非法时表单内联展示（与上行回复同规则同文案）。 */
const phoneError = computed<string | undefined>(() => {
  const value = phone.value.trim()
  return value === "" || /^1\d{10}$/.test(value) ? undefined : "手机号须为 11 位以 1 开头的数字"
})

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
    weekday: WEEKDAYS[new Date(`${day}T12:00:00+08:00`).getDay()],
    events,
  }))
})

const categoryLabel: Record<string, string> = { verify: "验证码", notice: "通知", market: "营销" }
const statusLabel: Record<string, string> = {
  delivered: "已送达",
  failed: "失败",
  unknown: "未知",
  sent: "已提交",
  pending: "待发送",
  other: "其他",
}
const blacklistSourceLabel: Record<string, string> = {
  manual: "人工加入",
  reply_optout: "回复退订",
  import: "导入",
}

function isCategory(value: string): value is "verify" | "notice" | "market" {
  return value === "verify" || value === "notice" || value === "market"
}

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

function reportTip(item: MessageItem): string {
  return item.report_time ? `厂商回报 ${formatTime(item.report_time)}` : "厂商回报描述"
}

function showReport(item: MessageItem): boolean {
  return Boolean(item.report_desc) && (item.status === "failed" || item.status === "unknown")
}

function maskFromInput(value: string): string {
  return `${value.slice(0, 3)}****${value.slice(-4)}`
}

let runToken = 0

async function run(): Promise<void> {
  const token = ++runToken
  loading.value = true
  errorMessage.value = ""
  const queryPhone = searchedPhone.value || phone.value
  try {
    const start = range.value?.[0].toISOString()
    const end = range.value?.[1].toISOString()
    if (mode.value === "list") {
      const result = await searchMessages(queryPhone, {
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
      decryptId.value = result.items[0]?.id
      if (result.items[0]?.phone) searchedMask.value = result.items[0].phone
    } else {
      const next = await getTimeline(queryPhone, start, end)
      if (token !== runToken) return
      timeline.value = next
      badge.value = next.badge
      items.value = []
      total.value = next.events.length
      decryptId.value = undefined
      if (canDecrypt.value) {
        try {
          const firstPage = await searchMessages(queryPhone, { page: 1 })
          if (token !== runToken) return
          decryptId.value = firstPage.items[0]?.id
          if (firstPage.items[0]?.phone) searchedMask.value = firstPage.items[0].phone
        } catch {
          if (token !== runToken) return
          decryptId.value = undefined
        }
      }
    }
  } catch (error) {
    if (token !== runToken) return
    items.value = []
    timeline.value = null
    badge.value = null
    total.value = 0
    decryptId.value = undefined
    errorMessage.value = error instanceof Error ? error.message : "号码查询失败"
  } finally {
    if (token === runToken) loading.value = false
  }
}

function search(): void {
  const value = phone.value.trim()
  if (value === "") {
    ElMessage.warning("请输入 11 位手机号")
    return
  }
  if (phoneError.value) {
    ElMessage.warning(phoneError.value)
    return
  }
  page.value = 1
  searched.value = true
  searchedPhone.value = value
  searchedMask.value = maskFromInput(value)
  revealedPhone.value = ""
  decryptId.value = undefined
  void run()
}

/** 重置查询栏条件（手机号/时间范围/视图）并回到未查询态；本页手机号必填，重置不自动查询。 */
function reset(): void {
  phone.value = ""
  range.value = null
  mode.value = "list"
  page.value = 1
  searched.value = false
  searchedPhone.value = ""
  searchedMask.value = ""
  revealedPhone.value = ""
  decryptId.value = undefined
  items.value = []
  timeline.value = null
  badge.value = null
  total.value = 0
  errorMessage.value = ""
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

function switchMode(next: "list" | "timeline"): void {
  if (next === mode.value) return
  mode.value = next
  page.value = 1
  if (searched.value) void run()
  else if (/^1\d{10}$/.test(phone.value)) search()
}

async function revealSearched(): Promise<void> {
  if (revealing.value || decryptId.value === undefined) return
  revealing.value = true
  errorMessage.value = ""
  try {
    const result = await decryptMessagePhone(decryptId.value)
    revealedPhone.value = result.phone
    ElMessage.success("已解密 · 本次授权查看已记入审计")
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "授权查看失败"
  } finally {
    revealing.value = false
  }
}
</script>

<template>
  <section class="page-heading message-heading">
    <div>
      <p class="eyebrow">PHONE TRACE / 号码轨迹</p>
      <h1>号码搜索</h1>
      <p>跨批次检索单个号码的下行与回复轨迹。手机号只在内存计算 HMAC 精确匹配，不明文持久化。</p>
    </div>
  </section>

  <form class="message-search message-filter-bar" @submit.prevent="search">
    <label class="message-fld">
      <span>手机号精确查询</span>
      <el-input
        v-model="phone"
        class="message-filter-phone"
        data-testid="message-filter-phone"
        placeholder="输入 11 位手机号"
        maxlength="11"
        inputmode="numeric"
        clearable
      />
      <small v-if="phoneError" class="message-phone-error">{{ phoneError }}</small>
    </label>
    <label class="message-fld">
      <span>时间范围（可选）</span>
      <el-date-picker
        v-model="range"
        type="datetimerange"
        format="YYYY-MM-DD HH:mm"
        popper-class="qingluan-date-popper"
        start-placeholder="开始时间"
        end-placeholder="结束时间"
        range-separator="至"
        class="message-filter-dates"
      />
    </label>
    <div class="message-fld">
      <span>视图</span>
      <div class="message-seg" role="group" aria-label="查询视图" data-testid="message-mode-seg">
        <button type="button" :class="{ on: mode === 'list' }" data-testid="message-view-list" @click="switchMode('list')">列表</button>
        <button type="button" :class="{ on: mode === 'timeline' }" data-testid="message-view-timeline" @click="switchMode('timeline')">时间线</button>
      </div>
    </div>
    <div class="message-filter-go">
      <el-button type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="message-reset" @click="reset">重置</el-button>
    </div>
    <p class="message-privacy">查询参数不进入 Nginx/Uvicorn 访问日志；服务端仅向 SQL 传递 <code>phone_hmac</code> 候选。类别与状态筛选在列表视图结果区提供。</p>
  </form>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

  <div v-if="badge" class="message-badge">
    <div class="message-badge-num">
      <small>当前号码{{ revealedPhone ? " · 已解密" : "" }}</small>
      <strong v-if="revealedPhone" class="revealed-phone">{{ revealedPhone }}</strong>
      <PhoneMask v-else :value="displayMask" />
    </div>
    <span :class="['message-badge-tag', badge.blacklisted ? 'is-listed' : 'is-clear']">
      {{ badge.blacklisted ? "已在黑名单" : "未在黑名单" }}
    </span>
    <span v-if="badge.blacklisted && badge.blacklist_source" class="message-badge-source">
      来源 {{ blacklistSourceLabel[badge.blacklist_source] || badge.blacklist_source }}
    </span>
    <span class="message-badge-sep" aria-hidden="true"></span>
    <div class="message-badge-stat">
      <span>近30日接收</span>
      <strong>{{ badge.recv_30d }} 条</strong>
    </div>
    <div v-if="canDecrypt && (decryptId !== undefined || revealedPhone)" class="message-badge-reveal">
      <small v-if="revealedPhone">解密明文仅存页面内存，刷新即失效</small>
      <el-button
        v-if="!revealedPhone"
        link
        type="primary"
        :loading="revealing"
        data-testid="message-phone-decrypt"
        @click="revealSearched"
      >授权查看</el-button>
      <el-button v-else disabled>已授权查看</el-button>
    </div>
  </div>

  <section v-if="mode === 'list'" class="message-results">
    <el-table v-loading="loading" :data="items" row-key="id" class="query-table">
      <el-table-column label="时间 / 批次" min-width="205">
        <template #default="{ row }">
          <time class="message-time">{{ formatTime(row.created_at) }}</time>
          <code class="batch-code cell-sub">{{ row.batch_no }}</code>
        </template>
      </el-table-column>
      <el-table-column label="类别" width="90">
        <template #default="{ row }">
          <CategoryTag v-if="isCategory(row.category)" :category="row.category" />
          <span v-else>{{ categoryLabel[row.category] || row.category }}</span>
        </template>
      </el-table-column>
      <el-table-column label="内容摘要" min-width="280">
        <template #default="{ row }"><p class="message-content">{{ row.content }}</p></template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <StatusTag :status="row.status" :label="statusLabel[row.status] || row.status" />
          <p v-if="showReport(row)" class="report-desc" :title="reportTip(row)">{{ row.report_desc }}</p>
        </template>
      </el-table-column>
      <el-table-column label="提交方" min-width="120">
        <template #default="{ row }">{{ row.sender || "—" }}</template>
      </el-table-column>
      <template #empty>
        <EmptyState
          :title="searched ? '未找到符合条件的记录' : '尚未查询号码记录'"
          :description="searched
            ? '该号码在所选时间范围内没有收发记录；可扩大时间范围，或核对号码后重试。'
            : '输入完整手机号后，这里会出现该号码跨批次的收发轨迹；切换到时间线可一屏还原「我们发了什么、用户回了什么」。'"
        />
      </template>
    </el-table>
    <div class="query-mobile-list">
      <article v-for="item in items" :key="item.id">
        <header>
          <CategoryTag v-if="isCategory(item.category)" :category="item.category" />
          <span v-else>{{ categoryLabel[item.category] || item.category }}</span>
          <StatusTag :status="item.status" :label="statusLabel[item.status] || item.status" />
        </header>
        <p>{{ item.content }}</p>
        <p v-if="showReport(item)" class="report-desc">{{ item.report_desc }}</p>
        <footer>
          <time>{{ formatTime(item.created_at) }}</time>
          <code>{{ item.batch_no }}</code>
        </footer>
      </article>
    </div>
    <footer v-if="searched" class="query-pagination message-pager">
      <span>共 {{ total }} 条 · 每页 20</span>
      <span class="result-filters">
        <el-select v-model="category" data-testid="message-category-filter" placeholder="全部类别" clearable size="small" style="width: 118px" @change="applyFilters">
          <el-option v-for="option in categoryOptions" :key="option.value" :value="option.value" :label="option.label" />
        </el-select>
        <el-select v-model="status" data-testid="message-status-filter" placeholder="全部状态" clearable size="small" style="width: 118px" @change="applyFilters">
          <el-option v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" />
        </el-select>
      </span>
      <el-pagination v-model:current-page="page" data-testid="message-pagination" :page-size="20" :total="total" layout="prev, pager, next" @current-change="changePage" />
    </footer>
  </section>

  <section v-else class="timeline-panel" v-loading="loading">
    <p v-if="timeline?.truncated" class="timeline-truncated">事件过多，仅显示最近 500 条；缩小时间范围可查看完整轨迹。</p>
    <EmptyState
      v-if="!timeline?.events.length"
      :title="searched ? '该号码在所选条件下没有事件' : '尚未生成号码时间线'"
      :description="searched ? '可调整时间范围后重试。' : '输入完整手机号后，下行与用户回复会按日期排列。'"
    />
    <section v-for="group in groupedEvents" :key="group.day" class="timeline-day">
      <h2>{{ group.day }}<span class="timeline-day-meta">{{ group.weekday }} · {{ group.events.length }} 事件</span></h2>
      <article
        v-for="event in group.events"
        :key="`${event.ts}-${event.direction}-${event.content}`"
        :class="['timeline-event', event.direction === 'in' ? 'incoming' : event.category]"
      >
        <div class="timeline-dot"></div>
        <header>
          <CategoryTag v-if="event.direction === 'out' && event.category && isCategory(event.category)" :category="event.category" />
          <span v-else-if="event.direction === 'out'" class="category-mark">{{ categoryLabel[event.category || ''] || '平台下行' }}</span>
          <strong v-else>↩ 用户回复</strong>
          <StatusTag v-if="event.status" :status="event.status" :label="statusLabel[event.status] || event.status" />
          <time>{{ formatTime(event.ts).slice(11) }}</time>
        </header>
        <p>{{ event.content }}</p>
        <footer>
          <code v-if="event.batch_no">{{ event.batch_no }}</code>
          <span>{{ event.sender }}</span>
        </footer>
      </article>
    </section>
  </section>
</template>
