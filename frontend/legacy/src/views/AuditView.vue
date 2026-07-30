<script setup lang="ts">
import "../styles/workspace.css"

import { onMounted, reactive, ref } from "vue"

import { listAudits, type AuditItem } from "../api/admin"

const filters = reactive({ actor: "", actorAccountId: "", action: "", objectType: "", start: "", end: "", page: 1, pageSize: 20 })
const items = ref<AuditItem[]>([])
const total = ref(0)
const loading = ref(false)
const errorMessage = ref("")
const selected = ref<AuditItem | null>(null)
const drawer = ref(false)
const timeRange = ref<[Date, Date] | null>(null)

function time(value: string): string {
  return new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value)).replaceAll("/", "-")
}

function json(value: Record<string, unknown> | null): string {
  return value ? JSON.stringify(value, null, 2) : "—"
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listAudits(filters)
    items.value = result.items
    total.value = result.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "审计日志加载失败"
  } finally {
    loading.value = false
  }
}

function search(): void {
  filters.start = timeRange.value?.[0].toISOString() || ""
  filters.end = timeRange.value?.[1].toISOString() || ""
  filters.page = 1
  void load()
}

function detail(item: AuditItem): void {
  selected.value = item
  drawer.value = true
}

function changePage(page: number): void {
  filters.page = page
  void load()
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading audit-heading">
    <div><p class="eyebrow">IMMUTABLE LEDGER / 不可变账本</p><h1>审计日志</h1><p>覆盖全部写操作与敏感读取；运行态账号只有新增与查询权限。</p></div>
    <span class="audit-lock">APPEND ONLY · 36 MONTHS</span>
  </section>

  <el-card shadow="never" class="audit-filter-card">
    <el-form class="audit-filter filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-2" label="操作人"><el-input v-model="filters.actor" data-testid="audit-actor" clearable placeholder="用户名" /></el-form-item>
      <el-form-item class="filter-span-2" label="稳定账号 ID"><el-input v-model="filters.actorAccountId" data-testid="audit-account-id" clearable placeholder="account_id" /></el-form-item>
      <el-form-item class="filter-span-2" label="动作"><el-input v-model="filters.action" clearable placeholder="如 config_update" /></el-form-item>
      <el-form-item class="filter-span-2" label="对象"><el-input v-model="filters.objectType" clearable placeholder="如 sys_config" /></el-form-item>
      <el-form-item class="filter-span-4" label="时间范围"><el-date-picker v-model="timeRange" data-testid="audit-time-range" type="datetimerange" popper-class="qingluan-date-popper" start-placeholder="开始时间" end-placeholder="结束时间" range-separator="至" /></el-form-item>
      <el-form-item class="filter-actions filter-span-2"><el-button type="primary" native-type="submit" :loading="loading">查询</el-button></el-form-item>
    </el-form>
  </el-card>
  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false"><template #default><el-button link type="primary" @click="load">重新加载</el-button></template></el-alert>

  <el-card shadow="never" class="audit-results" v-loading="loading">
    <header class="audit-result-title"><div><strong>操作事件流</strong><small>按时间倒序 · 仅展示安全审计载荷</small></div><span>{{ total }} 条</span></header>
    <el-table :data="items" class="audit-table"><el-table-column label="稳定主体" min-width="150"><template #default="{ row }">{{ row.actor_account_id ? `账号 #${row.actor_account_id}` : row.actor_app_id ? `应用 #${row.actor_app_id}` : '历史未知' }}</template></el-table-column><el-table-column prop="actor" label="操作人快照" min-width="120" /><el-table-column prop="action" label="动作" min-width="170"><template #default="{ row }"><code>{{ row.action }}</code></template></el-table-column><el-table-column label="对象" min-width="190"><template #default="{ row }">{{ row.object_type || '—' }} · {{ row.object_id || '—' }}</template></el-table-column><el-table-column prop="ip" label="IP" width="135" /><el-table-column label="时间" width="180"><template #default="{ row }">{{ time(row.created_at) }}</template></el-table-column><el-table-column label="操作" width="80"><template #default="{ row }"><el-button link type="primary" @click="detail(row)">详情</el-button></template></el-table-column></el-table>
    <div class="audit-mobile-list"><article v-for="item in items" :key="item.id"><header><code>{{ item.action }}</code><time>{{ time(item.created_at) }}</time></header><strong>{{ item.actor }} · {{ item.role || '—' }}</strong><p>{{ item.object_type || '—' }} / {{ item.object_id || '—' }}</p><el-button link type="primary" @click="detail(item)">详情</el-button></article></div>
    <el-pagination v-model:current-page="filters.page" :page-size="filters.pageSize" :total="total" layout="prev, pager, next" @current-change="changePage" />
  </el-card>

  <el-drawer v-model="drawer" title="审计事件详情" size="min(520px, 100vw)" class="audit-drawer">
    <template v-if="selected"><el-descriptions :column="1" border><el-descriptions-item label="事件">#{{ selected.id }} · {{ selected.action }}</el-descriptions-item><el-descriptions-item label="稳定主体">{{ selected.actor_subject_kind }} / account={{ selected.actor_account_id || '—' }} / identity={{ selected.actor_identity_id || '—' }} / app={{ selected.actor_app_id || '—' }}</el-descriptions-item><el-descriptions-item label="操作人快照">{{ selected.actor }} / {{ selected.role || '—' }}</el-descriptions-item><el-descriptions-item label="来源 IP">{{ selected.ip || '—' }}</el-descriptions-item><el-descriptions-item label="对象">{{ selected.object_type || '—' }} / {{ selected.object_id || '—' }}</el-descriptions-item><el-descriptions-item label="时间">{{ time(selected.created_at) }}</el-descriptions-item></el-descriptions><section class="audit-payload"><div><span>BEFORE</span><pre>{{ json(selected.before_val) }}</pre></div><div><span>AFTER</span><pre>{{ json(selected.after_val) }}</pre></div></section><el-alert title="载荷受数据库 PII 约束保护" type="success" :closable="false" description="手机号、逐号密文与 HMAC 列表无法写入 audit_log。" /></template>
  </el-drawer>
</template>
