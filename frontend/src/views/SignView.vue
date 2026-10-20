<script setup lang="ts">
import { useVendorResourceList } from "../composables/useVendorResourceList"
import { useLatestRead } from "../composables/useLatestRead"
import FilterSeg from "../components/FilterSeg.vue"
import { ElMessage } from "element-plus"
import { computed, onMounted, ref } from "vue"

import { listApps, type ManagedApp } from "../api/apps"
import {
  adoptExistingSign,
  createSign,
  deleteSign,
  listSigns,
  syncSign,
  updateSign,
  type SignState,
  type SmsSign,
} from "../api/signs"
import EmptyState from "../components/EmptyState.vue"
import SignDetailDrawer from "../components/SignDetailDrawer.vue"

import LoadErrorAlert from "../components/LoadErrorAlert.vue"
import StatusTag from "../components/StatusTag.vue"
import { useMobileLayout } from "../composables/useMobileLayout"
import { useConfirmActions } from "../lib/confirm"
import { vRowActivate } from "../lib/directives"
const { confirmAuditedAction } = useConfirmActions()
import { errorText } from "../lib/error"
import { roleNames, VENDOR_REVIEW_LABELS, vendorReviewSub, type VendorReviewSub } from "../lib/labels"
import { canAdoptSign, canDeleteSign, canEditSign, canSyncSign } from "../lib/signDisplay"
import { useSessionStore } from "../stores/session"

const EDITOR_FOOTNOTE = "提交后进入厂商人工审核，期间不可修改或删除；审核结果由轮询同步，也可在列表手动同步。"

const session = useSessionStore()
const isMobile = useMobileLayout()

const saving = ref(false)
const syncingId = ref<number | null>(null)
const detail = ref<SmsSign | null>(null)
const detailOpen = ref(false)
const editorOpen = ref(false)
const adoptOpen = ref(false)
const adopting = ref(false)
const adoptingSign = ref<SmsSign | null>(null)
const vendorSignId = ref("")
/** 编辑入口的源签名：驳回重交时用于回显上次厂商驳回原因。 */
const editingSource = ref<SmsSign | null>(null)
const name = ref("")
/** 默认签名引用：仅 admin 在详情抽屉发起联查，数据来自 GET /admin/apps 现有 default_sign 字段。 */
const signApps = ref<ManagedApp[]>([])
const signAppsLoading = ref(false)
const signAppsError = ref(false)
/** 签名读写入口仅 admin；operator/approver 只读。 */
const canWrite = computed(() => session.isAdmin)
const isAdmin = computed(() => session.isAdmin)

const { items, loading, errorMessage, load, stateFilter, keyword, stateOptions, filtered } = useVendorResourceList({
  fetcher: listSigns,
  states: ["pending", "approved", "rejected"],
  searchText: (item) => item.name,
  errorMessage: "签名列表加载失败",
})

const emptyTitle = computed(() => (items.value.length === 0 ? "当前没有签名" : "没有符合筛选条件的签名"))
const emptyDescription = computed(() =>
  items.value.length === 0
    ? "管理员提交签名并通过厂商审核后，发送内容将自动拼接规范签名。"
    : "重置状态筛选或清空关键词后查看全部签名。",
)

function stateLabel(state: SignState): string {
  return VENDOR_REVIEW_LABELS[state]
}

/** 状态副行：待审核按 vendor_sign_id 区分提交中/审核中；已拒绝直接显示驳回原因。 */
function stateSub(item: SmsSign): VendorReviewSub {
  return vendorReviewSub(item.vendor_state, item.vendor_sign_id, item.vendor_reject_reason)
}

const canSync = canSyncSign
const canAdopt = (item: SmsSign) => canAdoptSign(item, canWrite.value)
const canEdit = canEditSign
const canDelete = canDeleteSign

/** 与服务端 format_sign_name 一致的规范化：去空白、容忍一对外层方括号。 */
const bareName = computed(() => {
  let value = name.value.trim()
  if (value.startsWith("【") && value.endsWith("】")) value = value.slice(1, -1).trim()
  return value
})

const nameIssue = computed(() => {
  const value = bareName.value
  if (!value) return ""
  if (value.length > 20) return "签名名称不能超过 20 字"
  if (value.includes("【") || value.includes("】")) return "签名名称不得包含方括号"
  return ""
})

/** 计入每条计费长度的签名占用（含方括号），与 services/billing.py 的口径同源的前端演示。 */
const billingChars = computed(() => (bareName.value && !nameIssue.value ? bareName.value.length + 2 : null))

const signAppsRead = useLatestRead()
async function loadSignApps(signName: string): Promise<void> {
  if (!isAdmin.value) return
  const signal = signAppsRead.start()
  signAppsLoading.value = true
  signAppsError.value = false
  try {
    const apps = await listApps(signal)
    if (signal.aborted) return
    // 停用应用同样计入删除约束（服务端 NOT EXISTS 不过滤 status），这里一并列出。
    signApps.value = apps.filter((app) => {
      const configured = app.default_sign?.trim()
      if (!configured) return false
      const normalized =
        configured.startsWith("【") && configured.endsWith("】") ? configured.slice(1, -1).trim() : configured
      return normalized === signName
    })
  } catch {
    if (signal.aborted) return
    signAppsError.value = true
    signApps.value = []
  } finally {
    if (!signal.aborted) signAppsLoading.value = false
  }
}

function openDetail(item: SmsSign): void {
  detail.value = item
  detailOpen.value = true
  signApps.value = []
  void loadSignApps(item.name)
}

function resetEditor(item?: SmsSign): void {
  editingSource.value = item ?? null
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
    if (editingSource.value === null) {
      await createSign(name.value.trim())
      ElMessage.success("签名已加入厂商审核队列")
    } else {
      await updateSign(editingSource.value.id, name.value.trim())
      ElMessage.success("签名已重新提交厂商审核")
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "签名提交失败"))
  } finally {
    saving.value = false
  }
}

async function sync(item: SmsSign): Promise<void> {
  if (syncingId.value !== null) return
  syncingId.value = item.id
  try {
    await syncSign(item.id)
    ElMessage.success("审核状态同步请求已入队")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "审核状态同步失败"))
  } finally {
    syncingId.value = null
  }
}

function openAdopt(item: SmsSign): void {
  if (!canAdopt(item)) return
  adoptingSign.value = item
  vendorSignId.value = ""
  adoptOpen.value = true
}

async function adopt(): Promise<void> {
  const item = adoptingSign.value
  const value = vendorSignId.value.trim()
  if (!item || !/^\d+$/.test(value)) {
    ElMessage.warning("请输入正整数厂商签名 ID")
    return
  }
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed <= 0 || parsed > 2147483647) {
    ElMessage.warning("请输入正整数厂商签名 ID")
    return
  }
  adopting.value = true
  try {
    await adoptExistingSign(item.id, parsed, item.name)
    ElMessage.success("关联已有厂商签名与审核状态查询已入队")
    adoptOpen.value = false
    await load()
    if (detail.value?.id === item.id) {
      detail.value = items.value.find((candidate) => candidate.id === item.id) ?? detail.value
    }
  } catch (error) {
    ElMessage.error(errorText(error, "关联已有厂商签名失败"))
  } finally {
    adopting.value = false
  }
}

// 删除在途守卫：进入即置位（confirm 之前），拦截确认框期间的重复点击。
const removingId = ref<number | null>(null)
async function remove(item: SmsSign): Promise<void> {
  item = { ...item }
  if (removingId.value !== null) return
  removingId.value = item.id
  try {
    if (
      !(await confirmAuditedAction({
        title: "确认删除签名",
        body: `确认删除签名「${item.name}」？删除后不可恢复；已通过、被应用设为默认签名或已被批次引用的签名不可删除。`,
        auditNote: "删除行为与操作人将写入审计日志。",
        confirmText: "确认删除",
      }))
    )
      return
    await deleteSign(item.id)
    ElMessage.success("签名已删除 · 本次操作已记入审计")
    if (detail.value?.id === item.id) detailOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "签名删除失败"))
  } finally {
    removingId.value = null
  }
}

function editFromDetail(): void {
  if (!detail.value) return
  resetEditor(detail.value)
  detailOpen.value = false
}

onMounted(load)
</script>

<template>
  <section class="page-heading sign-heading">
    <div>
      <p class="eyebrow">IDENTITY / 发送身份</p>
      <h1>签名管理</h1>
      <p>平台保存裸名称，计费与厂商下发统一使用中文方括号规范签名；仅厂商审核通过的签名可在发送中使用。</p>
    </div>
    <el-button v-if="canWrite" data-testid="new-sign" type="primary" @click="resetEditor()">申请签名</el-button>
  </section>

  <div class="sign-filter-bar">
    <div class="sign-fld">
      <span>厂商状态</span>
      <FilterSeg
        v-model="stateFilter"
        :options="stateOptions"
        button-testid-prefix="sign-state"
        label="厂商状态筛选"
        data-testid="sign-state-seg"
        ><template #option="{ option }"
          >{{ option.label }} <i>{{ option.count }}</i></template
        ></FilterSeg
      >
    </div>
    <label class="sign-fld">
      <span>关键词</span>
      <el-input v-model="keyword" class="sign-keyword" data-testid="sign-keyword" placeholder="签名名称" clearable />
    </label>
    <span class="sign-filter-note">接口全量返回 · 前端过滤</span>
  </div>

  <LoadErrorAlert class="sign-alert" :message="errorMessage" @retry="load" />

  <section class="sign-results">
    <el-table
      v-if="!isMobile"
      v-loading="loading"
      class="sign-table"
      :data="filtered"
      row-key="id"
      @row-click="openDetail"
    >
      <el-table-column label="规范签名" min-width="220">
        <template #default="{ row }">
          <button
            v-row-activate="() => openDetail(row)"
            :data-testid="`sign-detail-${row.id}`"
            class="table-row-detail sign-name"
            type="button"
            :aria-label="`查看签名 ${row.name} 的详情`"
            @click.stop="openDetail(row)"
          >
            【{{ row.name }}】
          </button>
          <span class="cell-sub cell-sub-mono">平台 #{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="厂商状态" min-width="280">
        <template #default="{ row }">
          <StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" />
          <span class="cell-sub" :class="{ 'cell-sub-verm': stateSub(row).tone === 'verm' }">{{
            stateSub(row).text
          }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="230" fixed="right">
        <template #default="{ row }">
          <div @click.stop>
            <el-button
              v-if="canSync(row)"
              :data-testid="`sign-sync-${row.id}`"
              link
              type="primary"
              :loading="syncingId === row.id"
              @click="sync(row)"
              >同步</el-button
            >
            <el-button
              v-if="canAdopt(row)"
              :data-testid="`sign-adopt-${row.id}`"
              link
              type="primary"
              @click="openAdopt(row)"
              >关联已有签名</el-button
            >
            <el-button v-if="canEdit(row)" :data-testid="`sign-edit-${row.id}`" link @click="resetEditor(row)"
              >修改</el-button
            >
            <el-button
              v-if="canDelete(row)"
              :data-testid="`sign-delete-${row.id}`"
              link
              type="danger"
              :loading="removingId === row.id"
              :disabled="removingId !== null"
              @click="remove(row)"
              >删除</el-button
            >
            <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可编辑或删除"
              >不可编辑/删除</span
            >
          </div>
        </template>
      </el-table-column>
      <template #empty><EmptyState :title="emptyTitle" :description="emptyDescription" /></template>
    </el-table>
    <div v-else v-loading="loading" class="sign-mobile-list">
      <article v-for="row in filtered" :key="row.id">
        <header>
          <button
            v-row-activate="() => openDetail(row)"
            :data-testid="`mobile-sign-detail-${row.id}`"
            class="table-row-detail sign-name"
            type="button"
            :aria-label="`查看签名 ${row.name} 的详情`"
            @click="openDetail(row)"
          >
            【{{ row.name }}】
          </button>
          <StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" />
        </header>
        <p class="cell-sub" :class="{ 'cell-sub-verm': stateSub(row).tone === 'verm' }">{{ stateSub(row).text }}</p>
        <footer v-if="canWrite">
          <el-button
            v-if="canSync(row)"
            :data-testid="`mobile-sign-sync-${row.id}`"
            link
            type="primary"
            :loading="syncingId === row.id"
            @click="sync(row)"
            >同步</el-button
          >
          <el-button
            v-if="canAdopt(row)"
            :data-testid="`mobile-sign-adopt-${row.id}`"
            link
            type="primary"
            @click="openAdopt(row)"
            >关联已有签名</el-button
          >
          <el-button v-if="canEdit(row)" :data-testid="`mobile-sign-edit-${row.id}`" link @click="resetEditor(row)"
            >修改</el-button
          >
          <el-button
            v-if="canDelete(row)"
            :data-testid="`mobile-sign-delete-${row.id}`"
            link
            type="danger"
            :loading="removingId === row.id"
            :disabled="removingId !== null"
            @click="remove(row)"
            >删除</el-button
          >
          <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可编辑或删除"
            >不可编辑/删除</span
          >
        </footer>
      </article>
      <EmptyState v-if="!loading && !filtered.length" :title="emptyTitle" :description="emptyDescription" />
    </div>
    <footer class="sign-foot">
      <span>共 {{ filtered.length }} 个签名</span>
      <span class="sign-foot-role"
        >读：{{ roleNames(["operator", "approver", "admin"]) }} · 写：{{ roleNames(["admin"]) }}</span
      >
    </footer>
  </section>

  <SignDetailDrawer
    v-model="detailOpen"
    :sign="detail"
    :can-write="canWrite"
    :is-admin="isAdmin"
    :sign-apps="signApps"
    :sign-apps-loading="signAppsLoading"
    :sign-apps-error="signAppsError"
    :syncing-id="syncingId"
    :removing-id="removingId"
    @edit="editFromDetail"
    @sync="sync"
    @adopt="openAdopt"
    @remove="remove"
  />

  <el-drawer
    v-model="adoptOpen"
    title="关联已有厂商签名"
    size="min(440px, 92vw)"
    :teleported="false"
    :close-on-click-modal="false"
  >
    <template v-if="adoptingSign">
      <el-alert
        :title="`请先在厂商后台核对：厂商签名 ID 必须对应规范签名【${adoptingSign.name}】`"
        description="此操作只关联已有签名并发起审核状态查询，不会新建厂商签名，也不代表审核已经通过。"
        type="warning"
        :closable="false"
      />
      <el-form label-position="top" class="sign-form">
        <el-form-item label="本地规范签名（固定）">
          <el-input :model-value="`【${adoptingSign.name}】`" disabled data-testid="sign-adopt-confirmed-name" />
        </el-form-item>
        <el-form-item label="厂商签名 ID">
          <el-input
            v-model="vendorSignId"
            data-testid="sign-adopt-vendor-id"
            inputmode="numeric"
            placeholder="请输入正整数 ID"
          />
        </el-form-item>
      </el-form>
    </template>
    <template #footer>
      <el-button @click="adoptOpen = false">取消</el-button>
      <el-button data-testid="sign-adopt-submit" type="primary" :loading="adopting" @click="adopt"
        >确认关联并查询状态</el-button
      >
    </template>
  </el-drawer>

  <el-drawer v-model="editorOpen" class="sign-drawer" size="min(440px, 92vw)" :teleported="false">
    <template #header>
      <div class="sign-drawer-head">
        <div class="sign-drawer-title">{{ editingSource === null ? "申请签名" : "重新提交签名" }}</div>
        <code v-if="editingSource">平台 #{{ editingSource.id }} · 重新提交将生成新的厂商编号</code>
      </div>
    </template>
    <el-form label-position="top" class="sign-form">
      <el-alert
        v-if="editingSource?.vendor_state === 'rejected' && editingSource.vendor_reject_reason"
        :title="`上次厂商驳回：${editingSource.vendor_reject_reason}`"
        description="驳回原因来自厂商 checkRemark，服务端入库前已做手机号打码。"
        type="error"
        :closable="false"
        class="sign-reject-banner"
      />
      <el-form-item class="sign-name-item" :error="nameIssue || undefined">
        <template #label>
          签名名称
          <i>1–20 字 · 不得包含方括号</i>
        </template>
        <el-input v-model="name" maxlength="20" data-testid="sign-name-input">
          <template #prepend>【</template>
          <template #append>】</template>
        </el-input>
      </el-form-item>
      <div class="sign-content-block sign-editor-preview">
        <span>规范化预览（服务端 format_sign_name 唯一实现）</span>
        <template v-if="billingChars !== null">
          <p>平台保存：{{ bareName }} <small>（裸名称，全局唯一）</small></p>
          <p class="sign-preview-text">下发与计费：【{{ bareName }}】</p>
          <small class="sign-proof-hint"
            >计入每条计费长度 +{{ billingChars }} 字（含方括号）· 最终内容含签名与退订语 ≤70 字计 1 条</small
          >
        </template>
        <p v-else class="sign-preview-text">输入名称后实时预览规范化结果</p>
      </div>
    </el-form>
    <template #footer>
      <div class="sign-editor-foot">
        <small>{{ EDITOR_FOOTNOTE }}</small>
        <div>
          <el-button @click="editorOpen = false">取消</el-button>
          <el-button data-testid="sign-submit" type="primary" :loading="saving" @click="submit">{{
            editingSource === null ? "提交厂商审核" : "重新提交审核"
          }}</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
