<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onMounted, ref } from "vue"

import { listConfigs, updateConfigs } from "../api/admin"
import { addSensitiveWords, deleteSensitiveWord, listSensitiveWords, type SensitiveWordItem } from "../api/sensitiveWords"
import EmptyState from "../components/EmptyState.vue"

const items = ref<SensitiveWordItem[]>([])
const wordsText = ref("")
const policy = ref("block")
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const [words, configs] = await Promise.all([listSensitiveWords(), listConfigs()])
    items.value = words
    policy.value = configs.find((item) => item.key === "sensitive_action")?.value || "block"
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : "敏感词加载失败" }
  finally { loading.value = false }
}

async function add(): Promise<void> {
  const words = wordsText.value.split(/[\n,，;；]+/).map((value) => value.trim()).filter(Boolean)
  if (!words.length) return
  saving.value = true
  try {
    await addSensitiveWords(words)
    wordsText.value = ""
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
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "删除失败") }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading sensitive-heading"><div><p class="eyebrow">CONTENT POLICY / 内容策略</p><h1>敏感词</h1><p>维护内容拦截词库与审计策略。</p></div><el-select v-model="policy" class="sensitive-policy" data-testid="sensitive-policy" aria-label="敏感词策略" @change="savePolicy"><el-option label="命中阻断" value="block" /><el-option label="仅审计" value="audit" /></el-select></section>
  <el-card shadow="never" class="governance-entry">
    <el-form class="governance-entry-form" label-position="top">
      <el-form-item label="批量添加敏感词"><el-input v-model="wordsText" data-testid="sensitive-words-input" type="textarea" :rows="4" placeholder="每行一个词，最多 10,000 个" /></el-form-item>
      <div class="governance-entry-actions sensitive-entry-actions"><el-button data-testid="sensitive-words-add" type="primary" :loading="saving" @click="add">加入词库</el-button></div>
    </el-form>
  </el-card>
  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
  <el-card shadow="never"><el-table v-loading="loading" :data="items" row-key="id"><el-table-column prop="word" label="敏感词" min-width="240" /><el-table-column label="操作" width="90"><template #default="{ row }"><el-button link type="danger" @click="remove(row)">删除</el-button></template></el-table-column><template #empty><EmptyState title="敏感词库为空" description="新增词条后，服务端会按当前策略执行阻断或审计。" /></template></el-table></el-card>
</template>
