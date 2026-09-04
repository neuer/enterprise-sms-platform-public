<script setup lang="ts">
import { computed, ref } from "vue"

import { ElMessage } from "element-plus"

import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import PhoneMask from "../components/PhoneMask.vue"
import PhoneReveal from "../components/PhoneReveal.vue"
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
import { CATEGORY_LABELS, DEFAULT_PAGE_SIZE } from "../lib/labels"
import { maskPhone, PHONE_RE } from "../lib/phone"
import { formatDateTime } from "../lib/time"
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
const errorMessage = ref("")
/** 徽标条是否已完成一次授权查看（由 PhoneReveal 的 revealed 事件驱动），仅控制辅助文案。 */
const badgeRevealed = ref(false)
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")
const displayMask = computed(() => items.value[0]?.phone || searchedMask.value)

/** 手机号即时校验提示：空或合法为 undefined，非法时表单内联展示（与上行回复同规则同文案）。 */
const phoneError = computed<string | undefined>(() => {
  const value = phone.value.trim()
  return value === "" || PHONE_RE.test(value) ? undefined : "手机号须为 11 位以 1 开头的数字"
})

const categoryOptions = [
  { value: "verify", label: "验证码" },
  { value: "notice", label: "通知" },
  { value: "market", label: "营销" },
]
const statusOptions = [
  { value: "pending", label: "待处理" },
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
    const day = formatDateTime(event.ts).slice(0, 10)
    groups.set(day, [...(groups.get(day) || []), event])
  }
  return [...groups.entries()].map(([day, events]) => ({
    day,
    weekday: WEEKDAYS[new Date(`${day}T12:00:00+08:00`).getDay()],
    events,
  }))
})

const blacklistSourceLabel: Record<string, string> = {
  manual: "人工加入",
  reply_optout: "回复退订",
  import: "导入",
}

function isCategory(value: string): value is "verify" | "notice" | "market" {
  return value === "verify" || value === "notice" || value === "market"
}

function reportTip(item: MessageItem): string {
  return item.report_time ? `厂商回执 ${formatDateTime(item.report_time)}` : "厂商回执描述"
}

function showReport(item: MessageItem): boolean {
  return Boolean(item.report_desc) && (item.status === "failed" || item.status === "unknown")
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
  searchedMask.value = maskPhone(value)
  badgeRevealed.value = false
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
  badgeRevealed.value = false
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
  else if (PHONE_RE.test(phone.value)) search()
}

/** 徽标条授权查看：解密当前首条消息号码；明文只交给 PhoneReveal 内存展示，视图自身不保存明文。 */
async function revealSearched(): Promise<string> {
  if (decryptId.value === undefined) throw new Error("当前没有可授权查看的记录")
  const result = await decryptMessagePhone(decryptId.value)
  return result.phone
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
      <small>当前号码{{ badgeRevealed ? " · 已解密" : "" }}</small>
      <PhoneReveal
        v-if="canDecrypt && decryptId !== undefined"
        :key="decryptId"
        :masked="displayMask"
        :reveal="revealSearched"
        testid="message-phone-decrypt"
        @revealed="badgeRevealed = true"
      />
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
    <div v-if="badgeRevealed" class="message-badge-reveal">
      <small>解密明文仅存页面内存，刷新即失效</small>
      <el-button disabled>已授权查看</el-button>
    </div>
  </div>

  <section v-if="mode === 'list'" class="message-results">
    <el-table v-loading="loading" :data="items" row-key="id" class="query-table">
      <el-table-column label="时间 / 批次" min-width="205">
        <template #default="{ row }">
          <time class="message-time">{{ formatDateTime(row.created_at) }}</time>
          <code class="batch-code cell-sub">{{ row.batch_no }}</code>
        </template>
      </el-table-column>
      <el-table-column label="类别" width="90">
        <template #default="{ row }">
          <CategoryTag v-if="isCategory(row.category)" :category="row.category" />
          <span v-else>{{ CATEGORY_LABELS[row.category] || row.category }}</span>
        </template>
      </el-table-column>
      <el-table-column label="内容摘要" min-width="280">
        <template #default="{ row }"><p class="message-content">{{ row.content }}</p></template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <StatusTag :status="row.status" />
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
          <span v-else>{{ CATEGORY_LABELS[item.category] || item.category }}</span>
          <StatusTag :status="item.status" />
        </header>
        <p>{{ item.content }}</p>
        <p v-if="showReport(item)" class="report-desc">{{ item.report_desc }}</p>
        <footer>
          <time>{{ formatDateTime(item.created_at) }}</time>
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
      <el-pagination v-model:current-page="page" data-testid="message-pagination" :page-size="DEFAULT_PAGE_SIZE" :total="total" layout="prev, pager, next" @current-change="changePage" />
    </footer>
  </section>

  <section v-else v-loading="loading" class="timeline-panel">
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
          <span v-else-if="event.direction === 'out'" class="category-mark">{{ CATEGORY_LABELS[event.category || ''] || '平台下行' }}</span>
          <strong v-else>↩ 用户回复</strong>
          <StatusTag v-if="event.status" :status="event.status" />
          <time>{{ formatDateTime(event.ts).slice(11) }}</time>
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
