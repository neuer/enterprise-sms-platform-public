<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onMounted, ref } from "vue"

import { addBlacklist, deleteBlacklist, listBlacklist, type BlacklistItem } from "../api/blacklist"
import EmptyState from "../components/EmptyState.vue"

const items = ref<BlacklistItem[]>([])
const phonesText = ref("")
const remark = ref("")
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try { items.value = await listBlacklist() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : "黑名单加载失败" }
  finally { loading.value = false }
}

async function add(): Promise<void> {
  const phones = phonesText.value.split(/[\s,，;；]+/).map((value) => value.trim()).filter(Boolean)
  if (!phones.length) return
  saving.value = true
  try {
    const result = await addBlacklist(phones, remark.value.trim() || null)
    ElMessage.success(`已添加 ${result.added} 个号码`)
    phonesText.value = ""
    remark.value = ""
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "添加失败") }
  finally { saving.value = false }
}

async function remove(item: BlacklistItem): Promise<void> {
  try {
    await ElMessageBox.confirm(`从黑名单移除 ${item.phone_mask}？`, "确认移除", { type: "warning" })
    await deleteBlacklist(item.phone_hmac)
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "移除失败") }
}

onMounted(() => void load())
</script>

<template>
  <section class="page-heading"><div><p class="eyebrow">RECIPIENT GOVERNANCE / 接收治理</p><h1>黑名单</h1><p>仅持久化号码密文、HMAC 索引与掩码。</p></div><strong>{{ items.length }} 条</strong></section>
  <el-card shadow="never" class="governance-entry">
    <el-form class="governance-entry-form" label-position="top">
      <el-form-item label="批量添加号码"><el-input v-model="phonesText" data-testid="blacklist-phones" type="textarea" :rows="4" placeholder="每行一个手机号，最多 50,000 个" /></el-form-item>
      <div class="blacklist-entry-footer">
        <el-form-item label="备注"><el-input v-model="remark" maxlength="128" /></el-form-item>
        <div class="governance-entry-actions"><el-button data-testid="blacklist-add" type="primary" :loading="saving" @click="add">添加到黑名单</el-button></div>
      </div>
    </el-form>
  </el-card>
  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
  <el-card shadow="never" class="blacklist-card">
    <el-table v-loading="loading" :data="items" row-key="phone_hmac" class="blacklist-table">
      <el-table-column prop="phone_mask" label="号码" min-width="150" />
      <el-table-column prop="source" label="来源" width="130" />
      <el-table-column prop="remark" label="备注" min-width="180" />
      <el-table-column label="操作" width="90"><template #default="{ row }"><el-button :data-testid="`blacklist-delete-${row.phone_hmac.slice(0,8)}`" link type="danger" @click="remove(row)">移除</el-button></template></el-table-column>
      <template #empty><EmptyState title="当前没有黑名单记录" description="手工添加、导入或用户退订的号码会出现在这里。" /></template>
    </el-table>
    <div v-loading="loading" class="blacklist-mobile-list">
      <article v-for="item in items" :key="item.phone_hmac">
        <header><strong>{{ item.phone_mask }}</strong><span>{{ item.source }}</span></header>
        <p>{{ item.remark || "无备注" }}</p>
        <footer><el-button :data-testid="`mobile-blacklist-delete-${item.phone_hmac.slice(0,8)}`" link type="danger" @click="remove(item)">移除</el-button></footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="当前没有黑名单记录" description="手工添加、导入或用户退订的号码会出现在这里。" />
    </div>
  </el-card>
</template>
