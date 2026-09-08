<script setup lang="ts">
import { CATEGORY_OPTIONS, MESSAGE_STATUS_OPTIONS, BLACKLIST_SOURCE_LABELS } from "../lib/labels"
import { rangeToIsoParams } from "../lib/time"
import { computed, ref } from "vue"

import { ElMessage } from "element-plus"

import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"
import FilterSeg from "../components/FilterSeg.vue"
import ListPagination from "../components/ListPagination.vue"
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
import { usePagedList } from "../composables/usePagedList"
import { CATEGORY_LABELS } from "../lib/labels"
import { phoneProblem, maskPhone, PHONE_RE } from "../lib/phone"
import { formatDateTime } from "../lib/time"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
const phone = ref("")
const range = ref<[Date, Date] | null>(null)
const category = ref("")
const status = ref("")
const mode = ref<"list" | "timeline">("list")
const timeline = ref<TimelineResult | null>(null)
const badge = ref<PhoneBadge | null>(null)
const searched = ref(false)
const searchedPhone = ref("")
const searchedMask = ref("")
const decryptId = ref<number>()
/** 徽标条是否已完成一次授权查看（由 PhoneReveal 的 revealed 事件驱动），仅控制辅助文案。 */
const badgeRevealed = ref(false)
const canDecrypt = computed(() => session.canDecrypt)
const displayMask = computed(() => items.value[0]?.phone || searchedMask.value)

/** 手机号即时校验提示：空或合法为 undefined，非法时表单内联展示（与上行回复同规则同文案）。 */
const phoneError = computed(() => phoneProblem(phone.value.trim()))

const categoryOptions = CATEGORY_OPTIONS
const statusOptions = MESSAGE_STATUS_OPTIONS

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]

const groupedEvents = computed(() => {
  const groups = new Map<string, TimelineEvent[]>()
  for (const event of timeline.value?.events || []) {
    const day = formatDateTime(event.ts).slice(0, 10)
    const bucket = groups.get(day)
    if (bucket) bucket.push(event)
    else groups.set(day, [event])
  }
  return [...groups.entries()].map(([day, events]) => ({
    day,
    weekday: WEEKDAYS[new Date(`${day}T12:00:00+08:00`).getDay()],
    events,
  }))
})

const blacklistSourceLabel = BLACKLIST_SOURCE_LABELS

function isCategory(value: string): value is "verify" | "notice" | "market" {
  return value === "verify" || value === "notice" || value === "market"
}

function reportTip(item: MessageItem): string {
  return item.report_time ? `厂商回执 ${formatDateTime(item.report_time)}` : "厂商回执描述"
}

function showReport(item: MessageItem): boolean {
  return Boolean(item.report_desc) && (item.status === "failed" || item.status === "unknown")
}

/**
 * 号码轨迹查询：list / timeline 两种模式共用同一竞态守卫（usePagedList 单点）。
 * fetcher 返回 null 之外的全部差异（徽标、时间线、解密锚点）经 onLoaded/onError 回写。
 */
const {
  items,
  total,
  page,
  loading,
  errorMessage,
  load: run,
  cancel: cancelQuery,
} = usePagedList({
  fetcher: async (page) => {
    const queryPhone = searchedPhone.value || phone.value
    const { start, end } = rangeToIsoParams(range.value)
    if (mode.value === "list") {
      const result = await searchMessages(queryPhone, {
        start,
        end,
        category: category.value || undefined,
        status: status.value || undefined,
        page,
      })
      return {
        items: result.items,
        total: result.total,
        badge: result.badge,
        timeline: null,
        decryptId: result.items[0]?.id,
        firstPhone: result.items[0]?.phone,
      }
    }
    const next = await getTimeline(queryPhone, start, end)
    let firstId: number | undefined
    let firstPhone: string | undefined
    if (canDecrypt.value) {
      try {
        const firstPage = await searchMessages(queryPhone, { page: 1 })
        firstId = firstPage.items[0]?.id
        firstPhone = firstPage.items[0]?.phone
      } catch {
        firstId = undefined
      }
    }
    return { items: [], total: next.events.length, badge: next.badge, timeline: next, decryptId: firstId, firstPhone }
  },
  errorMessage: "号码查询失败",
  clearOnError: true,
  onLoaded: (result) => {
    badge.value = result.badge
    timeline.value = result.timeline
    decryptId.value = result.decryptId
    if (result.firstPhone) searchedMask.value = result.firstPhone
  },
  onError: () => {
    timeline.value = null
    badge.value = null
    decryptId.value = undefined
  },
})

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
  cancelQuery()
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

function switchMode(next: "list" | "timeline"): void {
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
      <FilterSeg
        :model-value="mode"
        :options="[
          ...[{ label: '列表', value: 'list' as const, testid: 'message-view-list' }],
          ...[{ label: '时间线', value: 'timeline' as const, testid: 'message-view-timeline' }],
        ]"
        aria-label="查询视图"
        data-testid="message-mode-seg"
        @update:model-value="switchMode"
      />
    </div>
    <div class="message-filter-go">
      <el-button type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="message-reset" @click="reset">重置</el-button>
    </div>
    <p class="message-privacy"
      >查询参数不进入 Nginx/Uvicorn 访问日志；服务端仅向 SQL 传递
      <code>phone_hmac</code> 候选。类别与状态筛选在列表视图结果区提供。</p
    >
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
        <template #default="{ row }"
          ><p class="message-content">{{ row.content }}</p></template
        >
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
          :description="
            searched
              ? '该号码在所选时间范围内没有收发记录；可扩大时间范围，或核对号码后重试。'
              : '输入完整手机号后，这里会出现该号码跨批次的收发轨迹；切换到时间线可一屏还原「我们发了什么、用户回了什么」。'
          "
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
    <ListPagination
      v-if="searched"
      v-model:page="page"
      :total="total"
      class="message-pager"
      testid="message-pagination"
      @change="run"
    >
      <span class="result-filters">
        <el-select
          v-model="category"
          data-testid="message-category-filter"
          placeholder="全部类别"
          clearable
          size="small"
          style="width: 118px"
          @change="applyFilters"
        >
          <el-option
            v-for="option in categoryOptions"
            :key="option.value"
            :value="option.value"
            :label="option.label"
          />
        </el-select>
        <el-select
          v-model="status"
          data-testid="message-status-filter"
          placeholder="全部状态"
          clearable
          size="small"
          style="width: 118px"
          @change="applyFilters"
        >
          <el-option v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" />
        </el-select>
      </span>
    </ListPagination>
  </section>

  <section v-else v-loading="loading" class="timeline-panel">
    <p v-if="timeline?.truncated" class="timeline-truncated"
      >事件过多，仅显示最近 500 条；缩小时间范围可查看完整轨迹。</p
    >
    <EmptyState
      v-if="!timeline?.events.length"
      :title="searched ? '该号码在所选条件下没有事件' : '尚未生成号码时间线'"
      :description="searched ? '可调整时间范围后重试。' : '输入完整手机号后，下行与用户回复会按日期排列。'"
    />
    <section v-for="group in groupedEvents" :key="group.day" class="timeline-day">
      <h2
        >{{ group.day }}<span class="timeline-day-meta">{{ group.weekday }} · {{ group.events.length }} 事件</span></h2
      >
      <article
        v-for="event in group.events"
        :key="`${event.ts}-${event.direction}-${event.content}`"
        :class="['timeline-event', event.direction === 'in' ? 'incoming' : event.category]"
      >
        <div class="timeline-dot"></div>
        <header>
          <CategoryTag
            v-if="event.direction === 'out' && event.category && isCategory(event.category)"
            :category="event.category"
          />
          <span v-else-if="event.direction === 'out'" class="category-mark">{{
            CATEGORY_LABELS[event.category || ""] || "平台下行"
          }}</span>
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
