<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, ref } from "vue"

import {
  createSign,
  deleteSign,
  listSigns,
  syncSign,
  updateSign,
  type SignState,
  type SmsSign,
} from "../api/signs"
import { useSessionStore } from "../stores/session"
import EmptyState from "../components/EmptyState.vue"

const session = useSessionStore()
const items = ref<SmsSign[]>([])
const loading = ref(false)
const saving = ref(false)
const syncingId = ref<number | null>(null)
const errorMessage = ref("")
const editorOpen = ref(false)
const editing = ref<SmsSign | null>(null)
const name = ref("")

function stateLabel(state: SignState): string {
  return { pending: "待审核", approved: "已通过", rejected: "已拒绝" }[state]
}

function stateType(state: SignState): "warning" | "success" | "danger" {
  if (state === "pending") return "warning"
  return state === "approved" ? "success" : "danger"
}

/** 与服务端 format_sign_name 一致的内联校验：去空白、容忍一对外层方括号后，剩余部分不得再含方括号。 */
const nameIssue = computed(() => {
  let value = name.value.trim()
  if (value.startsWith("【") && value.endsWith("】")) value = value.slice(1, -1).trim()
  if (!value) return ""
  if (value.length > 20) return "签名名称不能超过 20 字"
  if (value.includes("【") || value.includes("】")) return "签名名称不得包含方括号"
  return ""
})

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    items.value = await listSigns()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "签名列表加载失败"
  } finally {
    loading.value = false
  }
}

function openEditor(item?: SmsSign): void {
  editing.value = item ?? null
  name.value = item?.name ?? ""
  editorOpen.value = true
}

async function submit(): Promise<void> {
  if (!name.value.trim()) {
    ElMessage.warning("请填写签名名称")
    return
  }
  if (nameIssue.value) {
    ElMessage.warning(nameIssue.value)
    return
  }
  saving.value = true
  try {
    if (editing.value) await updateSign(editing.value.id, name.value.trim())
    else await createSign(name.value.trim())
    editorOpen.value = false
    ElMessage.success("签名已加入厂商审核队列")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "签名提交失败")
  } finally {
    saving.value = false
  }
}

async function sync(item: SmsSign): Promise<void> {
  if (syncingId.value !== null) return
  syncingId.value = item.id
  try {
    await syncSign(item.id)
    ElMessage.success("签名状态同步请求已入队")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "签名状态同步失败")
  } finally {
    syncingId.value = null
  }
}

async function remove(item: SmsSign): Promise<void> {
  try {
    await ElMessageBox.confirm("确认删除签名「" + item.name + "」？", "删除签名", {
      type: "warning",
      confirmButtonText: "确认删除",
      cancelButtonText: "取消",
    })
    await deleteSign(item.id)
    ElMessage.success("签名已删除")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "签名删除失败")
    }
  }
}

onMounted(load)
</script>

<template>
  <section class="page-heading sign-heading">
    <div>
      <p class="eyebrow">IDENTITY / 发送身份</p>
      <h1>签名管理</h1>
      <p>平台保存裸名称，计费与厂商下发统一使用中文方括号规范签名。</p>
    </div>
    <el-button v-if="session.role === 'admin'" type="primary" data-testid="new-sign" @click="openEditor()">申请签名</el-button>
  </section>

  <el-card shadow="never" class="sign-card">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" :data="items" row-key="id" class="sign-table">
      <el-table-column label="规范签名" min-width="190">
        <template #default="{ row }"><strong class="formatted-sign">【{{ row.name }}】</strong></template>
      </el-table-column>
      <el-table-column prop="vendor_sign_id" label="厂商编号" min-width="120">
        <template #default="{ row }"><code>{{ row.vendor_sign_id || "—" }}</code></template>
      </el-table-column>
      <el-table-column label="审核状态" width="120">
        <template #default="{ row }">
          <el-tag :type="stateType(row.vendor_state)" effect="dark">{{ stateLabel(row.vendor_state) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="厂商意见" min-width="220">
        <template #default="{ row }"><span :class="{ muted: !row.vendor_reject_reason }">{{ row.vendor_reject_reason || "—" }}</span></template>
      </el-table-column>
      <el-table-column v-if="session.role === 'admin'" label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button v-if="row.vendor_state === 'pending'" :data-testid="`sign-sync-${row.id}`" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
          <el-button v-if="row.vendor_state === 'rejected'" :data-testid="`sign-edit-${row.id}`" link @click="openEditor(row)">修改</el-button>
          <el-button v-if="row.vendor_state !== 'approved'" :data-testid="`sign-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button>
          <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可变更">—</span>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="当前没有签名" description="管理员提交签名后可在这里跟踪审核状态。" /></template>
    </el-table>
    <div v-loading="loading" class="sign-mobile-list">
      <article v-for="item in items" :key="item.id">
        <header>
          <strong class="formatted-sign">【{{ item.name }}】</strong>
          <el-tag :type="stateType(item.vendor_state)" effect="dark">{{ stateLabel(item.vendor_state) }}</el-tag>
        </header>
        <dl>
          <div><dt>厂商编号</dt><dd><code>{{ item.vendor_sign_id || "—" }}</code></dd></div>
          <div><dt>厂商意见</dt><dd>{{ item.vendor_reject_reason || "—" }}</dd></div>
        </dl>
        <footer v-if="session.role === 'admin'">
          <el-button v-if="item.vendor_state === 'pending'" :data-testid="`mobile-sign-sync-${item.id}`" link type="primary" :loading="syncingId === item.id" @click="sync(item)">同步</el-button>
          <el-button v-if="item.vendor_state === 'rejected'" :data-testid="`mobile-sign-edit-${item.id}`" link @click="openEditor(item)">修改</el-button>
          <el-button v-if="item.vendor_state !== 'approved'" :data-testid="`mobile-sign-delete-${item.id}`" link type="danger" @click="remove(item)">删除</el-button>
          <span v-if="item.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可变更">—</span>
        </footer>
      </article>
      <EmptyState v-if="!loading && !items.length" title="当前没有签名" description="管理员提交签名后可在这里跟踪审核状态。" />
    </div>
  </el-card>

  <el-drawer
    v-model="editorOpen"
    :title="editing ? '重新申请签名' : '申请签名'"
    size="min(440px, 92vw)"
  >
    <el-form label-position="top">
      <p class="drawer-intro">
        {{ editing ? "修改后将作为新签名重新提交厂商审核。" : "提交后进入厂商审核，审核期间不可修改或删除。" }}
      </p>
      <el-form-item label="签名名称" :error="nameIssue || undefined">
        <el-input v-model="name" maxlength="20" data-testid="sign-name-input">
          <template #prepend>【</template>
          <template #append>】</template>
        </el-input>
      </el-form-item>
      <p class="form-hint">请输入 1–20 字，不包含方括号；平台将在服务端统一规范化。</p>
    </el-form>
    <template #footer>
      <el-button @click="editorOpen = false">取消</el-button>
      <el-button type="primary" :loading="saving" data-testid="sign-submit" @click="submit">提交审核</el-button>
    </template>
  </el-drawer>
</template>
