<script setup lang="ts">
import "../styles/workspace.css"

import { computed, ref } from "vue"

import PhoneMask from "../components/PhoneMask.vue"
import EmptyState from "../components/EmptyState.vue"
import {
  decryptMessagePhone,
  getTimeline,
  searchMessages,
  type MessageItem,
  type TimelineEvent,
  type TimelineResult,
} from "../api/queries"
import { useSessionStore } from "../stores/session"

const session = useSessionStore()
const phone = ref("")
const range = ref<[Date, Date] | null>(null)
const mode = ref<"list" | "timeline">("list")
const items = ref<MessageItem[]>([])
const timeline = ref<TimelineResult | null>(null)
const total = ref(0)
const loading = ref(false)
const errorMessage = ref("")
const revealed = ref<Record<number, string>>({})
const canDecrypt = computed(() => session.role === "approver" || session.role === "admin")

const groupedEvents = computed(() => {
  const groups = new Map<string, TimelineEvent[]>()
  for (const event of timeline.value?.events || []) {
    const day = formatTime(event.ts).slice(0, 10)
    groups.set(day, [...(groups.get(day) || []), event])
  }
  return [...groups.entries()].map(([day, events]) => ({ day, events }))
})

const categoryLabel: Record<string, string> = { verify: "验证码", notice: "通知", market: "营销" }
const statusLabel: Record<string, string> = { delivered: "已送达", failed: "失败", unknown: "未知", sent: "已提交", pending: "待发送", other: "其他" }

function formatTime(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date(value)).replaceAll("/", "-")
}

async function search(): Promise<void> {
  if (!/^1\d{10}$/.test(phone.value)) { errorMessage.value = "请输入 11 位手机号"; return }
  loading.value = true
  errorMessage.value = ""
  revealed.value = {}
  try {
    const start = range.value?.[0].toISOString()
    const end = range.value?.[1].toISOString()
    if (mode.value === "list") {
      const result = await searchMessages(phone.value, start, end)
      items.value = result.items
      total.value = result.total
      timeline.value = null
    } else {
      timeline.value = await getTimeline(phone.value, start, end)
      items.value = []
      total.value = timeline.value.events.length
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "号码查询失败"
  } finally {
    loading.value = false
  }
}

function switchMode(next: "list" | "timeline"): void {
  mode.value = next
  if (phone.value) void search()
}

async function reveal(item: MessageItem): Promise<void> {
  try {
    const result = await decryptMessagePhone(item.id)
    revealed.value = { ...revealed.value, [item.id]: result.phone }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "授权查看失败"
  }
}
</script>

<template>
  <section class="page-heading message-heading">
    <div><p class="eyebrow">PHONE TRACE / 号码轨迹</p><h1>号码搜索</h1><p>手机号仅用于内存 HMAC 精确匹配；默认返回掩码，授权查看逐条审计。</p></div>
    <div class="view-switch" aria-label="查询视图"><el-button :type="mode === 'list' ? 'primary' : 'default'" @click="switchMode('list')">列表</el-button><el-button :type="mode === 'timeline' ? 'primary' : 'default'" @click="switchMode('timeline')">时间线</el-button></div>
  </section>

  <el-card shadow="never" class="message-search-card">
    <el-form class="message-search filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-4" label="手机号精确查询"><el-input v-model="phone" placeholder="输入 11 位手机号" maxlength="11" inputmode="numeric" clearable /></el-form-item>
      <el-form-item class="filter-span-6" label="时间范围"><el-date-picker v-model="range" type="datetimerange" popper-class="qingluan-date-popper" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /></el-form-item>
      <el-form-item class="query-filter-actions filter-actions filter-span-2"><el-button type="primary" native-type="submit" :loading="loading">查询</el-button></el-form-item>
    </el-form>
    <p class="query-privacy-note">查询参数不会进入 Nginx/Uvicorn 访问日志；服务端仅向 SQL 传递 HMAC 候选。</p>
  </el-card>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

  <el-card v-if="mode === 'list'" shadow="never" class="query-table-card message-results">
    <el-table v-loading="loading" :data="items" row-key="id" class="query-table">
      <el-table-column label="时间 / 批次" min-width="205"><template #default="{ row }"><time>{{ formatTime(row.created_at) }}</time><code class="batch-code cell-sub">{{ row.batch_no }}</code></template></el-table-column>
      <el-table-column label="号码" min-width="175"><template #default="{ row }"><strong v-if="revealed[row.id]" class="revealed-phone">{{ revealed[row.id] }}</strong><PhoneMask v-else :value="row.phone" /><el-button v-if="canDecrypt && !revealed[row.id]" link type="primary" @click="reveal(row)">授权查看</el-button></template></el-table-column>
      <el-table-column label="类别 / 内容" min-width="260"><template #default="{ row }"><span :class="['category-mark', row.category]">{{ categoryLabel[row.category] || row.category }}</span><p class="message-content">{{ row.content }}</p></template></el-table-column>
      <el-table-column label="状态" width="100"><template #default="{ row }"><el-tag :type="row.status === 'delivered' ? 'success' : row.status === 'failed' ? 'danger' : 'info'">{{ statusLabel[row.status] || row.status }}</el-tag></template></el-table-column>
      <el-table-column prop="sender" label="提交方" min-width="120" />
      <template #empty><EmptyState title="尚未查询号码记录" description="输入完整手机号后查询跨批次收发轨迹。" /></template>
    </el-table>
    <div class="query-mobile-list"><article v-for="item in items" :key="item.id"><header><PhoneMask :value="item.phone" /><el-tag>{{ statusLabel[item.status] || item.status }}</el-tag></header><p>{{ item.content }}</p><footer><code>{{ item.batch_no }}</code><el-button v-if="canDecrypt" link type="primary" @click="reveal(item)">{{ revealed[item.id] || '授权查看' }}</el-button></footer></article></div>
    <footer class="query-pagination"><span>共 {{ total }} 条记录</span></footer>
  </el-card>

  <section v-else class="timeline-panel" v-loading="loading">
    <header v-if="timeline" class="phone-badges"><el-tag :type="timeline.badge.blacklisted ? 'danger' : 'success'" effect="dark">{{ timeline.badge.blacklisted ? '已在黑名单' : '未在黑名单' }}</el-tag><span v-if="timeline.badge.blacklist_source">来源 {{ timeline.badge.blacklist_source }}</span><strong>近30日 {{ timeline.badge.recv_30d }} 条</strong></header>
    <EmptyState v-if="!timeline?.events.length" title="尚未生成号码时间线" description="输入完整手机号后，下行与用户回复会按日期排列。" />
    <section v-for="group in groupedEvents" :key="group.day" class="timeline-day"><h2>{{ group.day }}</h2><article v-for="event in group.events" :key="`${event.ts}-${event.direction}-${event.content}`" :class="['timeline-event', event.direction === 'in' && 'incoming']"><div class="timeline-dot"></div><header><strong>{{ event.direction === 'in' ? '↩ 用户回复' : categoryLabel[event.category || ''] || '平台下行' }}</strong><time>{{ formatTime(event.ts).slice(11) }}</time></header><p>{{ event.content }}</p><footer><code v-if="event.batch_no">{{ event.batch_no }}</code><el-tag v-if="event.status" size="small">{{ statusLabel[event.status] || event.status }}</el-tag><span>{{ event.sender }}</span></footer></article></section>
  </section>
</template>
