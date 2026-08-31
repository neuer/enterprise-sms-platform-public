<script setup lang="ts">
import "../styles/workspace.css"

import { computed, onMounted, reactive, ref } from "vue"

import { ElMessage } from "element-plus"

import { listAuditActions, listAudits, type AuditItem } from "../api/admin"
import EmptyState from "../components/EmptyState.vue"
import { copyText } from "../lib/clipboard"
import { DEFAULT_PAGE_SIZE } from "../lib/labels"
import { formatDateTime } from "../lib/time"

type DiffState = "added" | "removed" | "changed" | "same"

interface DiffRow {
  key: string
  before: string
  after: string
  state: DiffState
}

const DIFF_LABEL: Record<DiffState, string> = { added: "新增", removed: "删除", changed: "变更", same: "不变" }
const DIFF_RANK: Record<DiffState, number> = { changed: 0, added: 1, removed: 2, same: 3 }

const filters = reactive({ actor: "", actorAccountId: "", action: "", objectType: "", objectId: "", correlationId: "", start: "", end: "", page: 1, pageSize: DEFAULT_PAGE_SIZE })
const items = ref<AuditItem[]>([])
const total = ref(0)
const loading = ref(false)
const errorMessage = ref("")
const selected = ref<AuditItem | null>(null)
const drawer = ref(false)
const timeRange = ref<[Date, Date] | null>(null)
const actionOptions = ref<string[]>([])
// 更多筛选（低频精确字段：稳定账号 ID / 关联 ID，收进气泡）
const moreOpen = ref(false)

const timeShortcuts = [
  { text: "最近 1 小时", value: () => [new Date(Date.now() - 3_600_000), new Date()] as [Date, Date] },
  {
    text: "今天",
    value: () => {
      const end = new Date()
      const start = new Date()
      start.setHours(0, 0, 0, 0)
      return [start, end] as [Date, Date]
    },
  },
  { text: "最近 7 天", value: () => [new Date(Date.now() - 7 * 86_400_000), new Date()] as [Date, Date] },
  { text: "最近 30 天", value: () => [new Date(Date.now() - 30 * 86_400_000), new Date()] as [Date, Date] },
]

const filtering = computed(
  () =>
    Boolean(filters.actor.trim()) ||
    Boolean(filters.actorAccountId.trim()) ||
    Boolean(filters.action.trim()) ||
    Boolean(filters.objectType.trim()) ||
    Boolean(filters.objectId.trim()) ||
    Boolean(filters.correlationId.trim()) ||
    Boolean(filters.start) ||
    Boolean(filters.end),
)

const moreActiveCount = computed(
  () => (filters.actorAccountId.trim() ? 1 : 0) + (filters.correlationId.trim() ? 1 : 0),
)
const moreActive = computed(() => moreActiveCount.value > 0)

const selectedDiff = computed<DiffRow[]>(() =>
  selected.value ? diffRows(selected.value.before_val, selected.value.after_val) : [],
)

function payloadValue(value: unknown): string {
  if (value === null || value === undefined) return "—"
  if (typeof value === "string") return value === "" ? '""' : value
  return JSON.stringify(value)
}

function diffRows(before: Record<string, unknown> | null, after: Record<string, unknown> | null): DiffRow[] {
  const keys = [...new Set([...Object.keys(before ?? {}), ...Object.keys(after ?? {})])]
  return keys
    .map((key): DiffRow => {
      const hasBefore = before !== null && key in before
      const hasAfter = after !== null && key in after
      const beforeValue = hasBefore ? before[key] : undefined
      const afterValue = hasAfter ? after[key] : undefined
      const state: DiffState = !hasBefore
        ? "added"
        : !hasAfter
          ? "removed"
          : JSON.stringify(beforeValue) === JSON.stringify(afterValue)
            ? "same"
            : "changed"
      return {
        key,
        before: hasBefore ? payloadValue(beforeValue) : "—",
        after: hasAfter ? payloadValue(afterValue) : "—",
        state,
      }
    })
    .sort((a, b) => DIFF_RANK[a.state] - DIFF_RANK[b.state] || a.key.localeCompare(b.key))
}

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listAudits(filters)
    if (token !== loadToken) return
    items.value = result.items
    total.value = result.total
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "审计日志加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

async function loadActions(): Promise<void> {
  try {
    actionOptions.value = await listAuditActions()
  } catch {
    actionOptions.value = []
    ElMessage.warning("动作选项加载失败，筛选可稍后重试")
  }
}

function search(): void {
  filters.start = timeRange.value?.[0].toISOString() || ""
  filters.end = timeRange.value?.[1].toISOString() || ""
  filters.page = 1
  void load()
}

function reset(): void {
  filters.actor = ""
  filters.actorAccountId = ""
  filters.action = ""
  filters.objectType = ""
  filters.objectId = ""
  filters.correlationId = ""
  timeRange.value = null
  search()
}

function detail(item: AuditItem): void {
  selected.value = item
  drawer.value = true
}

async function copyCorrelation(): Promise<void> {
  if (!selected.value) return
  if (await copyText(selected.value.correlation_id)) {
    ElMessage.success("关联 ID 已复制到剪贴板")
  } else {
    ElMessage.error("复制失败，请手动选择文本复制")
  }
}

/** 同链路追踪复用 correlation_id 精确匹配；展开气泡如实展示激活的过滤条件。 */
function traceCorrelation(): void {
  const correlationId = selected.value?.correlation_id
  if (!correlationId) return
  filters.actor = ""
  filters.actorAccountId = ""
  filters.action = ""
  filters.objectType = ""
  filters.objectId = ""
  timeRange.value = null
  filters.start = ""
  filters.end = ""
  filters.correlationId = correlationId
  filters.page = 1
  drawer.value = false
  moreOpen.value = true
  void load()
}

onMounted(() => {
  void load()
  void loadActions()
})
</script>

<template>
  <section class="page-heading audit-heading">
    <div><p class="eyebrow">IMMUTABLE LEDGER / 不可变账本</p><h1>审计日志</h1><p>覆盖全部写操作与敏感读取；append-only，运行角色只有新增与查询权限。载荷受数据库 PII 约束保护，手机号与逐号密文无法写入。</p></div>
    <span class="audit-lock">APPEND ONLY · 36 MONTHS</span>
  </section>

  <form class="audit-filter-bar" @submit.prevent="search">
    <div class="audit-fld">
      <span>操作人</span>
      <el-input v-model="filters.actor" class="audit-keyword" data-testid="audit-actor" clearable placeholder="用户名" />
    </div>
    <div class="audit-fld">
      <span>动作</span>
      <el-select v-model="filters.action" class="audit-action" data-testid="audit-action" filterable clearable allow-create placeholder="全部动作">
        <el-option v-for="option in actionOptions" :key="option" :label="option" :value="option" />
      </el-select>
    </div>
    <div class="audit-fld">
      <span>对象类型</span>
      <el-input v-model="filters.objectType" class="audit-object-type" data-testid="audit-object-type" clearable placeholder="如 sys_config" />
    </div>
    <div class="audit-fld">
      <span>对象 ID</span>
      <el-input v-model="filters.objectId" class="audit-object-id" data-testid="audit-object-id" clearable placeholder="批次号 / 配置 key" />
    </div>
    <div class="audit-fld">
      <span>时间范围</span>
      <el-date-picker v-model="timeRange" class="audit-dates" data-testid="audit-time-range" type="datetimerange" :shortcuts="timeShortcuts" popper-class="qingluan-date-popper" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" />
    </div>
    <div class="audit-fld">
      <span>精确匹配</span>
      <el-popover v-model:visible="moreOpen" placement="bottom-end" :width="320" trigger="click">
        <template #reference>
          <button type="button" class="audit-more-trigger" :class="{ 'is-more-active': moreActive }" data-testid="audit-more-filters">更多筛选 ▾<b v-if="moreActiveCount" class="audit-more-count">{{ moreActiveCount }}</b></button>
        </template>
        <div class="audit-more">
          <label>稳定账号 ID</label>
          <el-input v-model="filters.actorAccountId" data-testid="audit-account-id" clearable placeholder="account_id 精确匹配" />
          <label>关联 ID</label>
          <el-input v-model="filters.correlationId" data-testid="audit-correlation-id" clearable placeholder="request ID 精确匹配" />
        </div>
      </el-popover>
    </div>
    <div class="audit-filter-go">
      <el-button type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="audit-reset" @click="reset">重置</el-button>
    </div>
    <p class="audit-privacy">操作人 / 动作 / 对象类型 / 对象 ID 与时间为服务端等值筛选；稳定账号 ID 与关联 ID（request ID）为精确匹配，收进「更多筛选」并带激活计数。筛选与分页均在服务端执行，不走「接口全量返回 · 前端过滤」。</p>
  </form>

  <aside class="audit-rules" aria-label="不可变账本与 PII 边界">
    <div><span>不可变账本</span><p>审计事件 append-only：七个运行角色均无 UPDATE/DELETE/TRUNCATE 权限；保留 36 个月，到期清理由 DBA 变更单驱动。</p></div>
    <div><span>PII 边界</span><p>载荷受数据库约束保护，手机号、逐号密文与 HMAC 列表无法写入 audit_log；载荷只记数量与引用，本页与详情均为只读。</p></div>
  </aside>

  <el-alert v-if="errorMessage" class="audit-alert" :title="errorMessage" type="error" :closable="false"><template #default><el-button link type="primary" @click="load">重新加载</el-button></template></el-alert>

  <section class="audit-results">
    <template v-if="items.length || loading">
      <el-table v-loading="loading" :data="items" class="audit-table"><el-table-column label="稳定主体" min-width="150"><template #default="{ row }">{{ row.actor_account_id ? `账号 #${row.actor_account_id}` : row.actor_app_id ? `应用 #${row.actor_app_id}` : '历史未知' }}</template></el-table-column><el-table-column prop="actor" label="操作人快照" min-width="120" /><el-table-column prop="action" label="动作" min-width="170"><template #default="{ row }"><code>{{ row.action }}</code></template></el-table-column><el-table-column label="对象" min-width="190"><template #default="{ row }">{{ row.object_type || '—' }} · {{ row.object_id || '—' }}</template></el-table-column><el-table-column prop="ip" label="IP" width="135" /><el-table-column label="时间" width="180"><template #default="{ row }">{{ formatDateTime(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="80"><template #default="{ row }"><el-button link type="primary" @click="detail(row)">详情</el-button></template></el-table-column></el-table>
      <div class="audit-mobile-list"><article v-for="item in items" :key="item.id"><header><code>{{ item.action }}</code><time>{{ formatDateTime(item.created_at) }}</time></header><strong>{{ item.actor }} · {{ item.role || '—' }}</strong><p>{{ item.object_type || '—' }} / {{ item.object_id || '—' }}</p><el-button link type="primary" @click="detail(item)">详情</el-button></article></div>
    </template>
    <div v-else-if="filtering" class="audit-empty-action">
      <EmptyState title="没有符合条件的审计事件" description="调整筛选条件或扩大时间范围后重新查询。" />
      <el-button data-testid="audit-clear-filters" @click="reset">清除筛选</el-button>
    </div>
    <div v-else class="audit-empty-action">
      <EmptyState title="暂无审计事件" description="全部写操作与敏感读取都会在此留下不可变记录。" />
    </div>
    <footer class="audit-pagination">
      <span>共 {{ total }} 条 · 每页 20</span>
      <el-pagination v-model:current-page="filters.page" :page-size="filters.pageSize" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </section>

  <el-drawer v-model="drawer" title="审计事件详情" size="min(560px, 92vw)" :teleported="false" class="audit-drawer">
    <template v-if="selected"><el-descriptions :column="1" border><el-descriptions-item label="事件">#{{ selected.id }} · {{ selected.action }}</el-descriptions-item><el-descriptions-item label="关联 ID"><div class="audit-correlation"><code>{{ selected.correlation_id }}</code><el-button link type="primary" data-testid="audit-copy-correlation" @click="copyCorrelation">复制</el-button><el-button link type="primary" data-testid="audit-trace-correlation" @click="traceCorrelation">同链路事件</el-button></div></el-descriptions-item><el-descriptions-item label="稳定主体">{{ selected.actor_subject_kind }} / account={{ selected.actor_account_id || '—' }} / identity={{ selected.actor_identity_id || '—' }} / app={{ selected.actor_app_id || '—' }}</el-descriptions-item><el-descriptions-item label="操作人快照">{{ selected.actor }} / {{ selected.role || '—' }}</el-descriptions-item><el-descriptions-item label="来源 IP">{{ selected.ip || '—' }}</el-descriptions-item><el-descriptions-item label="对象">{{ selected.object_type || '—' }} / {{ selected.object_id || '—' }}</el-descriptions-item><el-descriptions-item label="时间">{{ formatDateTime(selected.created_at) }}</el-descriptions-item></el-descriptions><section class="audit-diff" aria-label="载荷前后差异"><header class="audit-diff-head"><span>字段</span><span>BEFORE</span><span>AFTER</span></header><template v-if="selectedDiff.length"><div v-for="row in selectedDiff" :key="row.key" class="audit-diff-row" :class="`is-${row.state}`"><div class="audit-diff-key"><code>{{ row.key }}</code><em v-if="row.state !== 'same'">{{ DIFF_LABEL[row.state] }}</em></div><span class="audit-diff-value audit-diff-value--before">{{ row.before }}</span><span class="audit-diff-value audit-diff-value--after">{{ row.after }}</span></div></template><p v-else class="audit-diff-empty">该事件无 before/after 载荷记录</p></section><el-alert title="载荷受数据库 PII 约束保护" type="success" :closable="false" description="手机号、逐号密文与 HMAC 列表无法写入 audit_log。" /></template>
  </el-drawer>
</template>
