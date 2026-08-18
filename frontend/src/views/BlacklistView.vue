<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onMounted, reactive, ref } from "vue"

import {
  addBlacklist,
  deleteBlacklist,
  listBlacklist,
  type BlacklistItem,
  type BlacklistSource,
} from "../api/blacklist"
import EmptyState from "../components/EmptyState.vue"
import PhoneMask from "../components/PhoneMask.vue"

const items = ref<BlacklistItem[]>([])
const total = ref(0)
const filters = reactive<{ source: BlacklistSource | ""; keyword: string; page: number }>({
  source: "",
  keyword: "",
  page: 1,
})
const phonesText = ref("")
const remark = ref("")
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")

const sourceMeta: Record<BlacklistSource, { label: string; type: "primary" | "warning" | "info" }> = {
  manual: { label: "手工添加", type: "primary" },
  reply_optout: { label: "用户退订", type: "warning" },
  import: { label: "文件导入", type: "info" },
}

function sourceLabel(source: BlacklistSource): string {
  return sourceMeta[source]?.label ?? source
}

function sourceType(source: BlacklistSource): "primary" | "warning" | "info" {
  return sourceMeta[source]?.type ?? "info"
}

function formatTime(value: string | null): string {
  if (!value) return "—"
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date(value))
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`
}

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const result = await listBlacklist({
      source: filters.source,
      keyword: filters.keyword.trim(),
      page: filters.page,
    })
    if (token !== loadToken) return
    items.value = result.items
    total.value = result.total
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = error instanceof Error ? error.message : "黑名单加载失败"
  } finally {
    if (token === loadToken) loading.value = false
  }
}

function search(): void {
  filters.page = 1
  void load()
}

async function add(): Promise<void> {
  const phones = phonesText.value.split(/[\s,，;；]+/).map((value) => value.trim()).filter(Boolean)
  if (!phones.length) {
    ElMessage.warning("请先输入要添加的手机号")
    return
  }
  saving.value = true
  try {
    const result = await addBlacklist(phones, remark.value.trim() || null)
    const updatedTip = result.updated ? `，${result.updated} 个已存在并更新` : ""
    ElMessage.success(`已新增 ${result.added} 个号码${updatedTip}`)
    phonesText.value = ""
    remark.value = ""
    filters.page = 1
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "添加失败") }
  finally { saving.value = false }
}

async function remove(item: BlacklistItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`从黑名单移除 ${item.phone_mask}？`, "确认移除", { type: "warning" })
    await deleteBlacklist(item.phone_hmac)
    ElMessage.success(`已移除 ${item.phone_mask}`)
    if (items.value.length === 1 && filters.page > 1) filters.page -= 1
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "移除失败") }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading"><div><p class="eyebrow">RECIPIENT GOVERNANCE / 接收治理</p><h1>黑名单</h1><p>仅持久化号码密文、HMAC 索引与掩码。</p></div><strong>{{ total }} 条</strong></section>
  <el-card shadow="never" class="governance-entry">
    <el-form class="governance-entry-form" label-position="top" @submit.prevent>
      <el-form-item label="批量添加号码"><el-input v-model="phonesText" data-testid="blacklist-phones" type="textarea" :rows="4" placeholder="每行一个手机号，最多 50,000 个" /></el-form-item>
      <div class="blacklist-entry-footer">
        <el-form-item label="备注"><el-input v-model="remark" maxlength="128" /></el-form-item>
        <div class="governance-entry-actions"><el-button data-testid="blacklist-add" type="primary" :loading="saving" @click="add">添加到黑名单</el-button></div>
      </div>
    </el-form>
  </el-card>
  <el-card shadow="never" class="blacklist-filter-card">
    <div class="blacklist-filter filter-toolbar">
      <label for="blacklist-filter-source">来源</label>
      <el-select id="blacklist-filter-source" v-model="filters.source" data-testid="blacklist-filter-source" placeholder="全部来源" clearable style="width: 140px" @change="search">
        <el-option label="手工添加" value="manual" />
        <el-option label="用户退订" value="reply_optout" />
        <el-option label="文件导入" value="import" />
      </el-select>
      <el-input v-model="filters.keyword" data-testid="blacklist-filter-keyword" placeholder="搜索掩码或备注" clearable style="width: 220px" @keyup.enter="search" @clear="search" />
      <el-button data-testid="blacklist-search" type="primary" plain @click="search">查询</el-button>
      <span>关键词只匹配掩码与备注，不涉及号码明文</span>
    </div>
  </el-card>
  <el-card shadow="never" class="blacklist-card">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="phone_hmac" class="blacklist-table">
      <el-table-column label="号码" min-width="140"><template #default="{ row }"><PhoneMask :value="row.phone_mask" /></template></el-table-column>
      <el-table-column label="来源" width="110"><template #default="{ row }"><el-tag :type="sourceType(row.source)" effect="plain">{{ sourceLabel(row.source) }}</el-tag></template></el-table-column>
      <el-table-column label="备注" min-width="160"><template #default="{ row }">{{ row.remark || "—" }}</template></el-table-column>
      <el-table-column label="加入时间" width="178"><template #default="{ row }"><time>{{ formatTime(row.created_at) }}</time></template></el-table-column>
      <el-table-column label="操作" width="90"><template #default="{ row }"><el-button :data-testid="`blacklist-delete-${row.phone_hmac.slice(0,8)}`" link type="danger" @click="remove(row)">移除</el-button></template></el-table-column>
      <template #empty><EmptyState title="当前没有黑名单记录" description="手工添加、导入或用户退订的号码会出现在这里。" /></template>
    </el-table>
    <div v-loading="loading" class="blacklist-mobile-list">
      <article v-for="item in items" :key="item.phone_hmac">
        <header><PhoneMask :value="item.phone_mask" /><el-tag :type="sourceType(item.source)" effect="plain" size="small">{{ sourceLabel(item.source) }}</el-tag></header>
        <p>{{ item.remark || "无备注" }}</p>
        <footer><time>{{ formatTime(item.created_at) }}</time><el-button :data-testid="`mobile-blacklist-delete-${item.phone_hmac.slice(0,8)}`" link type="danger" @click="remove(item)">移除</el-button></footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="当前没有黑名单记录" description="手工添加、导入或用户退订的号码会出现在这里。" />
    </div>
    <footer class="blacklist-pagination">
      <span>共 {{ total }} 条</span>
      <el-pagination v-model:current-page="filters.page" data-testid="blacklist-pagination" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </el-card>
</template>
