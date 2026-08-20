<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, ref } from "vue"

import { listApps, type ManagedApp } from "../api/apps"
import {
  createSign,
  deleteSign,
  listSigns,
  syncSign,
  updateSign,
  type SignState,
  type SmsSign,
} from "../api/signs"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"
import { useSessionStore } from "../stores/session"

const EDITOR_FOOTNOTE = "提交后进入厂商人工审核，期间不可修改或删除；审核结果由轮询同步，也可在列表手动同步。"

interface StateSub {
  text: string
  tone?: "verm"
}

interface TrailStep {
  title: string
  desc: string
  tone: "done" | "wait" | "fail"
}

const session = useSessionStore()

const items = ref<SmsSign[]>([])
const loading = ref(false)
const saving = ref(false)
const syncingId = ref<number | null>(null)
const errorMessage = ref("")
const stateFilter = ref<SignState | "all">("all")
const keyword = ref("")
const detail = ref<SmsSign | null>(null)
const detailOpen = ref(false)
const editorOpen = ref(false)
/** 编辑入口的源签名：驳回重交时用于回显上次厂商驳回原因。 */
const editingSource = ref<SmsSign | null>(null)
const name = ref("")
/** 默认签名引用：仅 admin 在详情抽屉发起联查，数据来自 GET /admin/apps 现有 default_sign 字段。 */
const signApps = ref<ManagedApp[]>([])
const signAppsLoading = ref(false)
const signAppsError = ref(false)
/** 签名读写入口仅 admin；operator/approver 只读。 */
const canWrite = computed(() => session.role === "admin")
const isAdmin = computed(() => session.role === "admin")

const STATE_FILTERS: { label: string; value: SignState | "all" }[] = [
  { label: "全部", value: "all" },
  { label: "待审核", value: "pending" },
  { label: "已通过", value: "approved" },
  { label: "已拒绝", value: "rejected" },
]

/** 接口全量返回，状态计数与关键词过滤均为前端推导，不新增查询参数。 */
const stateOptions = computed(() =>
  STATE_FILTERS.map((option) => ({
    ...option,
    count:
      option.value === "all"
        ? items.value.length
        : items.value.filter((item) => item.vendor_state === option.value).length,
  })),
)

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return items.value.filter((item) => {
    if (stateFilter.value !== "all" && item.vendor_state !== stateFilter.value) return false
    if (kw && !item.name.toLowerCase().includes(kw)) return false
    return true
  })
})

const emptyTitle = computed(() =>
  items.value.length === 0 ? "当前没有签名" : "没有符合筛选条件的签名",
)
const emptyDescription = computed(() =>
  items.value.length === 0
    ? "管理员提交签名并通过厂商审核后，发送内容将自动拼接规范签名。"
    : "重置状态筛选或清空关键词后查看全部签名。",
)

function stateLabel(state: SignState): string {
  return { pending: "待审核", approved: "已通过", rejected: "已拒绝" }[state]
}

/** 状态副行：待审核按 vendor_sign_id 区分提交中/审核中；已拒绝直接显示驳回原因。 */
function stateSub(item: SmsSign): StateSub {
  switch (item.vendor_state) {
    case "approved":
      return { text: item.vendor_sign_id ? `厂商 #${item.vendor_sign_id}` : "厂商编号待同步" }
    case "pending":
      return {
        text: item.vendor_sign_id ? `厂商审核中 · #${item.vendor_sign_id}` : "提交厂商中…",
      }
    case "rejected":
      return { text: item.vendor_reject_reason || "厂商未附驳回原因", tone: "verm" }
  }
}

/** 详情抽屉标题副行：平台编号 / 已绑定厂商编号，缺项省略。 */
function detailHeadMeta(item: SmsSign): string {
  const parts = [`平台 #${item.id}`]
  if (item.vendor_sign_id) parts.push(`厂商 #${item.vendor_sign_id}`)
  return parts.join(" · ")
}

const canSync = (item: SmsSign) =>
  item.vendor_state === "pending" && item.vendor_sign_id !== null
const canEdit = (item: SmsSign) => item.vendor_state === "rejected"
const canDelete = (item: SmsSign) => item.vendor_state !== "approved"

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
const billingChars = computed(() =>
  bareName.value && !nameIssue.value ? bareName.value.length + 2 : null,
)

/** 详情抽屉审核轨迹：三态全部由 vendor_sign_id 与 vendor_state 前端推导。 */
const detailTrail = computed<TrailStep[]>(() => {
  const item = detail.value
  if (!item) return []
  const bound = item.vendor_sign_id !== null
  const result: TrailStep =
    item.vendor_state === "approved"
      ? { title: "厂商审核：已通过", desc: "GetSignState 同步", tone: "done" }
      : item.vendor_state === "rejected"
        ? { title: "厂商审核：已拒绝", desc: "GetSignState 同步", tone: "fail" }
        : {
            title: "厂商审核：等待结果",
            desc: bound ? "轮询同步 · 可手动同步" : "等待厂商绑定",
            tone: "wait",
          }
  return [
    { title: "已提交送审", desc: "Outbox → realtime worker", tone: "done" },
    {
      title: bound ? "已绑定厂商编号" : "等待厂商绑定",
      desc: bound ? `#${item.vendor_sign_id}` : "BindSign",
      tone: bound ? "done" : "wait",
    },
    result,
  ]
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

async function loadSignApps(signName: string): Promise<void> {
  if (!isAdmin.value) return
  signAppsLoading.value = true
  signAppsError.value = false
  try {
    const apps = await listApps()
    // 停用应用同样计入删除约束（服务端 NOT EXISTS 不过滤 status），这里一并列出。
    signApps.value = apps.filter((app) => app.default_sign === signName)
  } catch {
    signAppsError.value = true
    signApps.value = []
  } finally {
    signAppsLoading.value = false
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
    ElMessage.success("审核状态同步请求已入队")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "审核状态同步失败")
  } finally {
    syncingId.value = null
  }
}

async function remove(item: SmsSign): Promise<void> {
  try {
    await ElMessageBox.confirm(
      h("div", { class: "sign-delete-dialog" }, [
        h(
          "p",
          `确认删除签名「${item.name}」？删除后不可恢复；已通过、被应用设为默认签名或已被批次引用的签名不可删除。`,
        ),
        h("p", { class: "sign-delete-audit" }, "删除行为与操作人将写入审计日志。"),
      ]),
      "删除签名",
      {
        type: "warning",
        confirmButtonText: "确认删除",
        cancelButtonText: "取消",
        customClass: "sign-delete-box",
      },
    )
    await deleteSign(item.id)
    ElMessage.success("签名已删除 · 本次操作已记入审计")
    if (detail.value?.id === item.id) detailOpen.value = false
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "签名删除失败")
    }
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
      <div class="sign-seg" role="group" aria-label="厂商状态筛选" data-testid="sign-state-seg">
        <button
          v-for="option in stateOptions"
          :key="option.value"
          type="button"
          :class="{ on: stateFilter === option.value }"
          :data-testid="`sign-state-${option.value}`"
          @click="stateFilter = option.value"
        >
          {{ option.label }} <i>{{ option.count }}</i>
        </button>
      </div>
    </div>
    <label class="sign-fld">
      <span>关键词</span>
      <el-input
        v-model="keyword"
        class="sign-keyword"
        data-testid="sign-keyword"
        placeholder="签名名称"
        clearable
      />
    </label>
    <span class="sign-filter-note">接口全量返回 · 前端过滤</span>
  </div>

  <el-alert v-if="errorMessage" class="sign-alert" :title="errorMessage" type="error" :closable="false" />

  <section class="sign-results">
    <el-table v-loading="loading" class="sign-table" :data="filtered" row-key="id" @row-click="openDetail">
      <el-table-column label="规范签名" min-width="220">
        <template #default="{ row }">
          <button
            :data-testid="`sign-detail-${row.id}`"
            class="table-row-detail sign-name"
            type="button"
            :aria-label="`查看签名 ${row.name} 的详情`"
            @click.stop="openDetail(row)"
            @keydown.enter.stop.prevent="openDetail(row)"
            @keydown.space.stop.prevent="openDetail(row)"
          >
            【{{ row.name }}】
          </button>
          <span class="cell-sub cell-sub-mono">平台 #{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="厂商状态" min-width="280">
        <template #default="{ row }">
          <StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" />
          <span class="cell-sub" :class="{ 'cell-sub-verm': stateSub(row).tone === 'verm' }">{{ stateSub(row).text }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <div @click.stop>
            <el-button v-if="canSync(row)" :data-testid="`sign-sync-${row.id}`" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
            <el-button v-if="canEdit(row)" :data-testid="`sign-edit-${row.id}`" link @click="resetEditor(row)">修改</el-button>
            <el-button v-if="canDelete(row)" :data-testid="`sign-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button>
            <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可变更或删除">不可变更</span>
          </div>
        </template>
      </el-table-column>
      <template #empty><EmptyState :title="emptyTitle" :description="emptyDescription" /></template>
    </el-table>
    <div v-loading="loading" class="sign-mobile-list">
      <article v-for="row in filtered" :key="row.id">
        <header>
          <button
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
          <el-button v-if="canSync(row)" :data-testid="`mobile-sign-sync-${row.id}`" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
          <el-button v-if="canEdit(row)" :data-testid="`mobile-sign-edit-${row.id}`" link @click="resetEditor(row)">修改</el-button>
          <el-button v-if="canDelete(row)" :data-testid="`mobile-sign-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button>
          <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的签名不可变更或删除">不可变更</span>
        </footer>
      </article>
      <EmptyState v-if="!loading && !filtered.length" :title="emptyTitle" :description="emptyDescription" />
    </div>
    <footer class="sign-foot">
      <span>共 {{ filtered.length }} 个签名</span>
      <span class="sign-foot-role">读：operator / approver / admin · 写：admin</span>
    </footer>
  </section>

  <el-drawer v-model="detailOpen" class="sign-drawer" size="min(560px, 94vw)">
    <template #header>
      <div v-if="detail" class="sign-drawer-head">
        <div class="sign-drawer-title">
          <StatusTag :status="detail.vendor_state" :label="stateLabel(detail.vendor_state)" />
          <b>【{{ detail.name }}】</b>
        </div>
        <code>{{ detailHeadMeta(detail) }}</code>
      </div>
    </template>
    <template v-if="detail">
      <div class="sign-trail" data-testid="sign-trail">
        <template v-for="(step, index) in detailTrail" :key="index">
          <span v-if="index > 0" class="trail-connector"></span>
          <span class="trail-step" :class="step.tone">
            <i class="dot"></i>
            <span class="trail-txt"><b>{{ step.title }}</b><small>{{ step.desc }}</small></span>
          </span>
        </template>
      </div>
      <el-alert
        v-if="detail.vendor_state === 'rejected' && detail.vendor_reject_reason"
        :title="`厂商驳回原因：${detail.vendor_reject_reason}`"
        description="修改名称后重新提交，将生成新的厂商编号并重新进入审核。"
        type="error"
        :closable="false"
        class="sign-reject-banner"
      />
      <div class="sign-content-block">
        <span>发送效果预览（签名自动拼接于内容最前）</span>
        <p class="sign-preview-text">【{{ detail.name }}】您的验证码为 123456，5 分钟内有效，请勿泄露。</p>
        <small class="sign-proof-hint">计费长度含签名：本签名 +{{ detail.name.length + 2 }} 字（含方括号）· 含签名与退订语 ≤70 字计 1 条，唯一实现 services/billing.py</small>
      </div>
      <dl class="sign-fact-grid">
        <div><dt>平台编号</dt><dd class="mono-id">{{ detail.id }}</dd></div>
        <div><dt>厂商编号</dt><dd class="mono-id">{{ detail.vendor_sign_id || "—" }}</dd></div>
        <div><dt>规范签名</dt><dd>【{{ detail.name }}】</dd></div>
        <div><dt>计入计费长度</dt><dd class="mono-id">+{{ detail.name.length + 2 }} 字</dd></div>
      </dl>
      <div v-if="isAdmin" class="sign-content-block" data-testid="sign-apps">
        <span>默认签名引用（仅 admin 可见）</span>
        <div class="sign-apps">
          <span v-if="signAppsLoading" class="sign-apps-note">加载中…</span>
          <span v-else-if="signAppsError" class="sign-apps-note">应用引用信息加载失败</span>
          <template v-else>
            <span v-for="app in signApps" :key="app.id" class="sign-app-chip">{{ app.name }}<template v-if="app.status === 0">（已停用）</template></span>
            <span v-if="!signApps.length" class="sign-apps-note">暂无应用把该签名设为默认签名</span>
          </template>
        </div>
        <small class="sign-proof-hint">来自 GET /admin/apps 现有 default_sign 字段前端联查，不新增接口；停用应用同样计入删除约束。</small>
      </div>
      <div v-if="canWrite" class="sign-detail-actions">
        <el-button v-if="canEdit(detail)" data-testid="sign-detail-edit" type="primary" @click="editFromDetail">修改并重新提交</el-button>
        <el-button v-if="canSync(detail)" data-testid="sign-detail-sync" :loading="syncingId === detail.id" @click="sync(detail)">同步审核状态</el-button>
        <el-button v-if="canDelete(detail)" data-testid="sign-detail-delete" link type="danger" @click="remove(detail)">删除签名</el-button>
        <p class="why">已通过审核的签名不可变更或删除；如需调整请申请新签名，默认签名请在应用管理中维护。</p>
      </div>
    </template>
  </el-drawer>

  <el-drawer v-model="editorOpen" class="sign-drawer" size="min(440px, 92vw)">
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
          <small class="sign-proof-hint">计入每条计费长度 +{{ billingChars }} 字（含方括号）· 最终内容含签名与退订语 ≤70 字计 1 条</small>
        </template>
        <p v-else class="sign-preview-text">输入名称后实时预览规范化结果</p>
      </div>
    </el-form>
    <template #footer>
      <div class="sign-editor-foot">
        <small>{{ EDITOR_FOOTNOTE }}</small>
        <div>
          <el-button @click="editorOpen = false">取消</el-button>
          <el-button data-testid="sign-submit" type="primary" :loading="saving" @click="submit">{{ editingSource === null ? "提交厂商审核" : "重新提交审核" }}</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
