<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onMounted, reactive, ref } from "vue"

import { listConfigs, updateConfigs } from "../api/admin"
import { addSensitiveWords, deleteSensitiveWord, listSensitiveWords, type SensitiveWordItem } from "../api/sensitiveWords"
import EmptyState from "../components/EmptyState.vue"

const items = ref<SensitiveWordItem[]>([])
const total = ref(0)
const filters = reactive<{ keyword: string; page: number }>({ keyword: "", page: 1 })
const wordsText = ref("")
const policy = ref("block")
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")

function formatTime(value: string | null): string {
  if (!value) return "—"
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).formatToParts(new Date(value))
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? ""
  return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const [page, configs] = await Promise.all([
      listSensitiveWords({ keyword: filters.keyword.trim(), page: filters.page }),
      listConfigs(),
    ])
    items.value = page.items
    total.value = page.total
    policy.value = configs.find((item) => item.key === "sensitive_action")?.value || "block"
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : "敏感词加载失败" }
  finally { loading.value = false }
}

function search(): void {
  filters.page = 1
  void load()
}

async function add(): Promise<void> {
  const words = wordsText.value.split(/[\n,，;；]+/).map((value) => value.trim()).filter(Boolean)
  if (!words.length) {
    ElMessage.warning("请先输入要添加的敏感词")
    return
  }
  saving.value = true
  try {
    const result = await addSensitiveWords(words)
    const skippedTip = result.skipped ? `，${result.skipped} 个已存在跳过` : ""
    ElMessage.success(`已新增 ${result.added} 个敏感词${skippedTip}`)
    wordsText.value = ""
    filters.page = 1
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "添加失败") }
  finally { saving.value = false }
}

async function savePolicy(value: string): Promise<void> {
  try {
    await updateConfigs([{ key: "sensitive_action", value }])
    ElMessage.success(value === "block" ? "敏感词命中将阻断发送" : "敏感词命中仅审计记录")
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "策略更新失败") }
}

async function remove(item: SensitiveWordItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`删除敏感词“${item.word}”？`, "确认删除", { type: "warning" })
    await deleteSensitiveWord(item.id)
    ElMessage.success(`已删除“${item.word}”`)
    if (items.value.length === 1 && filters.page > 1) filters.page -= 1
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "删除失败") }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading sensitive-heading"><div><p class="eyebrow">CONTENT POLICY / 内容策略</p><h1>敏感词</h1><p>维护内容拦截词库与审计策略。</p></div><el-select v-model="policy" class="sensitive-policy" data-testid="sensitive-policy" aria-label="敏感词策略" @change="savePolicy"><el-option label="命中阻断" value="block" /><el-option label="仅审计" value="audit" /></el-select></section>
  <el-card shadow="never" class="governance-entry">
    <el-form class="governance-entry-form" label-position="top" @submit.prevent>
      <el-form-item label="批量添加敏感词"><el-input v-model="wordsText" data-testid="sensitive-words-input" type="textarea" :rows="4" placeholder="每行一个词，最多 10,000 个，单词不超过 64 字" /></el-form-item>
      <div class="governance-entry-actions sensitive-entry-actions"><el-button data-testid="sensitive-words-add" type="primary" :loading="saving" @click="add">加入词库</el-button></div>
    </el-form>
  </el-card>
  <el-card shadow="never" class="sensitive-filter-card">
    <div class="sensitive-filter filter-toolbar">
      <label for="sensitive-filter-keyword">词面</label>
      <el-input id="sensitive-filter-keyword" v-model="filters.keyword" data-testid="sensitive-filter-keyword" placeholder="搜索敏感词" clearable style="width: 220px" @keyup.enter="search" @clear="search" />
      <el-button data-testid="sensitive-search" type="primary" plain @click="search">查询</el-button>
      <span>共 {{ total }} 个词条，命中按当前策略阻断或仅审计</span>
    </div>
  </el-card>
  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
  <el-card shadow="never" class="sensitive-card">
    <el-table v-loading="loading" :data="items" row-key="id" class="sensitive-table">
      <el-table-column prop="word" label="敏感词" min-width="240" />
      <el-table-column label="添加时间" width="178"><template #default="{ row }"><time>{{ formatTime(row.created_at) }}</time></template></el-table-column>
      <el-table-column label="操作" width="90"><template #default="{ row }"><el-button :data-testid="`sensitive-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button></template></el-table-column>
      <template #empty><EmptyState title="敏感词库为空" description="新增词条后，服务端会按当前策略执行阻断或审计。" /></template>
    </el-table>
    <div v-loading="loading" class="sensitive-mobile-list">
      <article v-for="item in items" :key="item.id">
        <header><strong>{{ item.word }}</strong><time>{{ formatTime(item.created_at) }}</time></header>
        <footer><el-button :data-testid="`mobile-sensitive-delete-${item.id}`" link type="danger" @click="remove(item)">删除</el-button></footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="敏感词库为空" description="新增词条后，服务端会按当前策略执行阻断或审计。" />
    </div>
    <footer class="sensitive-pagination">
      <span>共 {{ total }} 条</span>
      <el-pagination v-model:current-page="filters.page" data-testid="sensitive-pagination" :page-size="20" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </el-card>
</template>
