<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { onMounted, reactive, ref } from "vue"

import {
  createApp,
  disableApp,
  listApps,
  parseFrequencyOverride,
  revokeOldAppKey,
  rotateAppKey,
  rotateCallbackSecret,
  updateApp,
  type AppCategory,
  type AppPayload,
  type ManagedApp,
} from "../api/apps"
import { listConfigs } from "../api/admin"
import CategoryTag from "../components/CategoryTag.vue"
import EmptyState from "../components/EmptyState.vue"

type SecretOperation = "create-app" | "rotate-api-key" | "rotate-callback-secret"

const items = ref<ManagedApp[]>([])
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")
const drawerOpen = ref(false)
const editingId = ref<number | null>(null)
const secretOpen = ref(false)
const secretTitle = ref("")
const secretValue = ref("")
const secretHint = ref("")
const keyGraceHours = ref<number | null>(null)
const secretOperation = ref<SecretOperation | null>(null)
const rotatingKeyId = ref<number | null>(null)
const rotatingCallbackId = ref<number | null>(null)

const form = reactive({
  name: "", dept: "", allowed_categories: ["notice"] as AppCategory[], default_sign: "",
  daily_quota: 0, rate_limit_per_min: 60, blacklist_check: true, freq_override: "",
  callback_url: "", callback_report_enabled: false, status: 1 as 0 | 1,
})

function resetForm(): void {
  Object.assign(form, {
    name: "", dept: "", allowed_categories: ["notice"], default_sign: "",
    daily_quota: 0, rate_limit_per_min: 60, blacklist_check: true, freq_override: "",
    callback_url: "", callback_report_enabled: false, status: 1,
  })
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try { items.value = await listApps() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : "应用列表加载失败" }
  finally { loading.value = false }
}

async function loadKeyGraceHours(): Promise<void> {
  try {
    const configs = await listConfigs()
    const raw = configs.find((item) => item.key === "key_grace_hours")?.value
    const value = Number(raw)
    keyGraceHours.value = Number.isInteger(value) && value > 0 ? value : null
  } catch {
    keyGraceHours.value = null
  }
}

function openCreate(): void {
  editingId.value = null
  resetForm()
  drawerOpen.value = true
}

function openEdit(item: ManagedApp): void {
  editingId.value = item.id
  Object.assign(form, {
    ...item,
    default_sign: item.default_sign || "",
    callback_url: item.callback_url || "",
    freq_override: item.freq_override ? JSON.stringify(item.freq_override) : "",
  })
  drawerOpen.value = true
}

function payload(): AppPayload {
  const override = parseFrequencyOverride(form.freq_override)
  return {
    dept: form.dept.trim(), allowed_categories: form.allowed_categories,
    default_sign: form.default_sign.trim() || null, daily_quota: form.daily_quota,
    rate_limit_per_min: form.rate_limit_per_min, blacklist_check: form.blacklist_check,
    freq_override: override, callback_url: form.callback_url.trim() || null,
    callback_report_enabled: form.callback_report_enabled, status: form.status,
  }
}

function reveal(title: string, value: string, hint = "请立即保存；关闭后平台不会再次展示。") {
  secretTitle.value = title
  secretValue.value = value
  secretHint.value = hint
  secretOpen.value = true
}

function clearSecret(): void {
  secretTitle.value = ""
  secretValue.value = ""
  secretHint.value = ""
  secretOperation.value = null
  rotatingKeyId.value = null
  rotatingCallbackId.value = null
}

function closeSecret(): void {
  clearSecret()
  secretOpen.value = false
}

function beforeSecretClose(done: () => void): void {
  clearSecret()
  done()
}

async function save(): Promise<void> {
  if (!form.name.trim() || !form.dept.trim() || !form.allowed_categories.length) {
    ElMessage.warning("请填写应用名、部门并选择至少一个类别")
    return
  }
  const targetId = editingId.value
  const creating = targetId === null
  if (creating && secretOperation.value !== null) return
  if (creating) secretOperation.value = "create-app"
  let secretRevealed = false
  saving.value = true
  try {
    const body = payload()
    if (creating) {
      const result = await createApp({ ...body, name: form.name.trim() })
      const credentials = result.callback_secret
        ? `API Key: ${result.api_key}\nCallback Secret: ${result.callback_secret}`
        : result.api_key
      secretRevealed = true
      reveal("应用凭据（仅展示一次）", credentials)
    } else {
      await updateApp(targetId, body)
      ElMessage.success("应用配置已更新")
    }
    drawerOpen.value = false
    await load()
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "应用保存失败") }
  finally {
    saving.value = false
    if (creating && !secretRevealed) clearSecret()
  }
}

async function rotateKey(item: ManagedApp): Promise<void> {
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-api-key"
  rotatingKeyId.value = item.id
  let secretRevealed = false
  try {
    const graceHint = keyGraceHours.value === null
      ? "旧 Key 将进入当前配置的宽限期。"
      : `旧 Key 将进入 ${keyGraceHours.value} 小时宽限期。`
    await ElMessageBox.confirm(
      `将为 ${item.name} 生成新的 API Key。新 Key 仅展示一次，${graceHint}请确认已准备好立即复制并安全保存。`,
      "确认轮换 API Key",
      { type: "warning", confirmButtonText: "确认轮换", cancelButtonText: "取消" },
    )
    const result = await rotateAppKey(item.id)
    secretRevealed = true
    reveal(
      "这是当前最终 API Key（仅展示一次）",
      result.api_key,
      `请立即复制并安全保存，确认保存后再关闭。旧 Key 宽限期至 ${result.old_key_expires_at}`,
    )
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "Key 轮换失败")
    }
  } finally { if (!secretRevealed) clearSecret() }
}

async function revokeKey(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`立即作废 ${item.name} 的旧 Key？`, "确认作废", { type: "warning" })
    await revokeOldAppKey(item.id)
    ElMessage.success("旧 Key 已作废")
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "作废失败") }
}

async function rotateCallback(item: ManagedApp): Promise<void> {
  if (secretOperation.value !== null) return
  secretOperation.value = "rotate-callback-secret"
  rotatingCallbackId.value = item.id
  let secretRevealed = false
  try {
    const result = await rotateCallbackSecret(item.id)
    secretRevealed = true
    reveal("新回调密钥（仅展示一次）", result.callback_secret)
  } catch (error) { ElMessage.error(error instanceof Error ? error.message : "回调密钥轮换失败") }
  finally { if (!secretRevealed) clearSecret() }
}

async function disable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`停用应用 ${item.name}？`, "确认停用", { type: "warning" })
    await disableApp(item.id)
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "停用失败") }
}

async function enable(item: ManagedApp): Promise<void> {
  try {
    await ElMessageBox.confirm(`启用应用 ${item.name}？`, "确认启用", { type: "warning" })
    await updateApp(item.id, {
      dept: item.dept,
      allowed_categories: item.allowed_categories,
      default_sign: item.default_sign,
      daily_quota: item.daily_quota,
      rate_limit_per_min: item.rate_limit_per_min,
      blacklist_check: item.blacklist_check,
      freq_override: item.freq_override,
      callback_url: item.callback_url,
      callback_report_enabled: item.callback_report_enabled,
      status: 1,
    })
    await load()
  } catch (error) { if (error !== "cancel" && error !== "close") ElMessage.error(error instanceof Error ? error.message : "启用失败") }
}

onMounted(() => {
  void load()
  void loadKeyGraceHours()
})
</script>

<template>
  <section class="page-heading"><div><p class="eyebrow">APPLICATION CONTROL / 应用控制</p><h1>应用管理</h1><p>管理调用方、配额、频控与密钥生命周期。</p></div><el-button data-testid="new-app" type="primary" :disabled="secretOperation !== null" @click="openCreate">新建应用</el-button></section>
  <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
  <el-alert
    class="key-grace-notice"
    type="info"
    :closable="false"
    :title="keyGraceHours === null ? '旧 Key 宽限期暂不可用' : `旧 Key 宽限期 ${keyGraceHours} 小时`"
  />
  <el-card shadow="never" class="app-management-card">
    <el-table v-loading="loading" class="app-management-table" :data="items" row-key="id">
      <el-table-column label="应用" min-width="170">
        <template #default="{ row }"><strong>{{ row.name }}</strong><small class="cell-sub">#{{ row.id }} · {{ row.dept }}</small></template>
      </el-table-column>
      <el-table-column label="类别" min-width="170">
        <template #default="{ row }"><CategoryTag v-for="category in row.allowed_categories" :key="category" :category="category" /></template>
      </el-table-column>
      <el-table-column prop="daily_quota" label="日配额" width="110" />
      <el-table-column prop="rate_limit_per_min" label="每分钟限流" width="120" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }"><el-tag :type="row.status ? 'success' : 'info'">{{ row.status ? '启用' : '停用' }}</el-tag></template>
      </el-table-column>
      <el-table-column label="操作" min-width="420" fixed="right">
        <template #default="{ row }">
          <el-button :data-testid="`edit-app-${row.id}`" link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button :data-testid="`rotate-key-${row.id}`" link type="primary" :loading="rotatingKeyId === row.id" :disabled="secretOperation !== null" @click="rotateKey(row)">轮换 Key</el-button>
          <el-button :data-testid="`revoke-key-${row.id}`" link @click="revokeKey(row)">作废旧 Key</el-button>
          <el-button :data-testid="`rotate-callback-${row.id}`" link :loading="rotatingCallbackId === row.id" :disabled="secretOperation !== null" @click="rotateCallback(row)">轮换回调密钥</el-button>
          <el-button v-if="row.status" :data-testid="`disable-app-${row.id}`" link type="danger" @click="disable(row)">停用</el-button>
          <el-button v-else :data-testid="`enable-app-${row.id}`" link type="success" @click="enable(row)">启用</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="当前没有接入应用" description="创建应用后可配置类别、配额、限流和回调。" /></template>
    </el-table>
    <div v-loading="loading" class="app-management-mobile-list">
      <article v-for="row in items" :key="row.id">
        <header>
          <div><strong>{{ row.name }}</strong><small>#{{ row.id }} · {{ row.dept }}</small></div>
          <el-tag :type="row.status ? 'success' : 'info'">{{ row.status ? '启用' : '停用' }}</el-tag>
        </header>
        <div class="app-management-categories">
          <CategoryTag v-for="category in row.allowed_categories" :key="category" :category="category" />
        </div>
        <dl>
          <div><dt>日配额</dt><dd>{{ row.daily_quota }}</dd></div>
          <div><dt>每分钟限流</dt><dd>{{ row.rate_limit_per_min }}</dd></div>
        </dl>
        <footer>
          <el-button :data-testid="`mobile-edit-app-${row.id}`" link type="primary" @click="openEdit(row)">编辑</el-button>
          <el-button :data-testid="`mobile-rotate-key-${row.id}`" link type="primary" :loading="rotatingKeyId === row.id" :disabled="secretOperation !== null" @click="rotateKey(row)">轮换 Key</el-button>
          <el-button :data-testid="`mobile-revoke-key-${row.id}`" link @click="revokeKey(row)">作废旧 Key</el-button>
          <el-button :data-testid="`mobile-rotate-callback-${row.id}`" link :loading="rotatingCallbackId === row.id" :disabled="secretOperation !== null" @click="rotateCallback(row)">轮换回调密钥</el-button>
          <el-button v-if="row.status" :data-testid="`mobile-disable-app-${row.id}`" link type="danger" @click="disable(row)">停用</el-button>
          <el-button v-else :data-testid="`mobile-enable-app-${row.id}`" link type="success" @click="enable(row)">启用</el-button>
        </footer>
      </article>
      <EmptyState
        v-if="!loading && !items.length"
        title="当前没有接入应用"
        description="创建应用后可配置类别、配额、限流和回调。"
      />
    </div>
  </el-card>
  <el-drawer v-model="drawerOpen" :title="editingId === null ? '新建应用' : '编辑应用'" size="min(560px, 100vw)" :teleported="false"><el-form label-position="top"><el-form-item label="应用名"><el-input v-model="form.name" :disabled="editingId !== null" /></el-form-item><el-form-item label="部门"><el-input v-model="form.dept" /></el-form-item><el-form-item label="允许类别"><el-checkbox-group v-model="form.allowed_categories"><el-checkbox value="verify">验证码</el-checkbox><el-checkbox value="notice">通知</el-checkbox><el-checkbox value="market">营销</el-checkbox></el-checkbox-group></el-form-item><el-form-item label="默认签名"><el-input v-model="form.default_sign" /></el-form-item><el-form-item label="日配额"><el-input-number v-model="form.daily_quota" :min="0" /></el-form-item><el-form-item label="每分钟限流"><el-input-number v-model="form.rate_limit_per_min" :min="1" /></el-form-item><el-form-item label="黑名单检查"><el-switch v-model="form.blacklist_check" /></el-form-item><el-form-item label="频控覆盖 JSON"><el-input v-model="form.freq_override" data-testid="freq-override" type="textarea" placeholder='例如 {"verify_per_minute":2,"verify_per_day":20,"market_per_day":1}' /></el-form-item><el-form-item label="回调 URL"><el-input v-model="form.callback_url" /></el-form-item><el-form-item label="消息级回调"><el-switch v-model="form.callback_report_enabled" /></el-form-item></el-form><template #footer><el-button @click="drawerOpen=false">取消</el-button><el-button data-testid="save-app" type="primary" :loading="saving" @click="save">保存</el-button></template></el-drawer>
  <el-dialog v-model="secretOpen" :title="secretTitle" width="min(560px, 92vw)" :close-on-click-modal="false" :before-close="beforeSecretClose" destroy-on-close @closed="clearSecret"><el-alert type="warning" :closable="false" :title="secretHint" /><pre class="one-time-secret">{{ secretValue }}</pre><template #footer><el-button data-testid="secret-close" type="primary" @click="closeSecret">我已安全保存</el-button></template></el-dialog>
</template>
