<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, getCurrentInstance, onMounted, reactive, ref, watch } from "vue"
import type { Router } from "vue-router"

import {
  createTemplate,
  deleteTemplate,
  listTemplates,
  syncTemplate,
  updateTemplate,
  type SmsTemplate,
  type TemplatePayload,
  type TemplateState,
} from "../api/templates"
import EmptyState from "../components/EmptyState.vue"
import StatusTag from "../components/StatusTag.vue"
import { useSessionStore } from "../stores/session"

const PLACEHOLDER_TOKEN = /\{[^{}]*\}/g
const PLACEHOLDER_POSITION = /^\{([1-9]\d*)\}$/
const DEFAULT_MAX_LEN = 10

interface ContentPart {
  text: string
  pos?: number
  maxLen?: number
}

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
const router = getCurrentInstance()?.appContext.config.globalProperties.$router as Router | undefined

const items = ref<SmsTemplate[]>([])
const loading = ref(false)
const saving = ref(false)
const syncingId = ref<number | null>(null)
const errorMessage = ref("")
const stateFilter = ref<TemplateState | "all">("all")
const keyword = ref("")
const detail = ref<SmsTemplate | null>(null)
const detailOpen = ref(false)
const editorOpen = ref(false)
/** 编辑入口的源模板：驳回重交时用于回显上次厂商驳回原因。 */
const editingSource = ref<SmsTemplate | null>(null)
const form = reactive<TemplatePayload>({ name: "", content: "", var_specs: [] })
const canWrite = computed(() => session.role === "operator" || session.role === "admin")
/** 非 admin 的响应恒为本部门，部门列只在 admin 跨部门视图渲染。 */
const isAdmin = computed(() => session.role === "admin")

const STATE_FILTERS: { label: string; value: TemplateState | "all" }[] = [
  { label: "全部", value: "all" },
  { label: "待审核", value: "pending" },
  { label: "已通过", value: "approved" },
  { label: "已拒绝", value: "rejected" },
  { label: "草稿", value: "draft" },
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
    if (kw && !item.name.toLowerCase().includes(kw) && !item.content.toLowerCase().includes(kw)) {
      return false
    }
    return true
  })
})

const emptyTitle = computed(() =>
  items.value.length === 0 ? "当前没有模板" : "没有符合筛选条件的模板",
)
const emptyDescription = computed(() =>
  items.value.length === 0
    ? "新建模板并提交厂商审核后，可在这里跟踪审核状态与厂商编号。"
    : "重置状态筛选或清空关键词后查看全部模板。",
)

function stateLabel(state: TemplateState): string {
  return { draft: "草稿", pending: "待审核", approved: "已通过", rejected: "已拒绝" }[state]
}

/** 状态副行：待审核按 vendor_template_id 区分提交中/审核中；已拒绝直接显示驳回原因。 */
function stateSub(item: SmsTemplate): StateSub {
  switch (item.vendor_state) {
    case "approved":
      return { text: item.vendor_template_id ? `厂商 #${item.vendor_template_id}` : "厂商编号待同步" }
    case "pending":
      return {
        text: item.vendor_template_id ? `厂商审核中 · #${item.vendor_template_id}` : "提交厂商中…",
      }
    case "rejected":
      return { text: item.vendor_reject_reason || "厂商未附驳回原因", tone: "verm" }
    default:
      return { text: "未送审" }
  }
}

/** 提取内容中的 {n} 占位位置；非法 {} 片段单独计数用于内联提示。 */
function contentPlaceholders(content: string): { positions: number[]; invalidTokens: number } {
  const tokens = content.match(PLACEHOLDER_TOKEN) ?? []
  const positions: number[] = []
  let invalidTokens = 0
  for (const token of tokens) {
    const matched = PLACEHOLDER_POSITION.exec(token)
    if (matched) positions.push(Number(matched[1]))
    else invalidTokens += 1
  }
  return { positions, invalidTokens }
}

/** 把平台内容拆成正文片段与变量片，供列表/详情内联渲染；非法 {} 片段保持原文。 */
function contentParts(content: string, specs: { pos: number; max_len: number }[]): ContentPart[] {
  const maxLenByPos = new Map(specs.map((spec) => [spec.pos, spec.max_len]))
  const parts: ContentPart[] = []
  let cursor = 0
  for (const match of content.matchAll(PLACEHOLDER_TOKEN)) {
    const matched = PLACEHOLDER_POSITION.exec(match[0])
    if (!matched) continue
    const index = match.index ?? 0
    if (index > cursor) parts.push({ text: content.slice(cursor, index) })
    const pos = Number(matched[1])
    parts.push({ text: match[0], pos, maxLen: maxLenByPos.get(pos) })
    cursor = index + match[0].length
  }
  if (cursor < content.length) parts.push({ text: content.slice(cursor) })
  return parts
}

/** 厂商格式预览：与服务端 to_vendor_template 同一规则，平台 {n} 按声明最大长度转 {s<max_len>}。 */
function vendorPreviewOf(content: string, specs: { pos: number; max_len: number }[]): string {
  const maxLenByPos = new Map(specs.map((spec) => [spec.pos, spec.max_len]))
  return content.replace(PLACEHOLDER_TOKEN, (token) => {
    const matched = PLACEHOLDER_POSITION.exec(token)
    if (!matched) return token
    const maxLen = maxLenByPos.get(Number(matched[1]))
    return maxLen === undefined ? token : `{s${maxLen}}`
  })
}

const editorVendorPreview = computed(() =>
  form.content ? vendorPreviewOf(form.content, form.var_specs) : "填写内容后实时预览",
)

/** 提交前的即时反馈，与服务端校验（占位与 var_specs 一一对应、从 1 连续）保持一致。 */
const contentIssue = computed(() => {
  if (!form.content) return ""
  const { positions, invalidTokens } = contentPlaceholders(form.content)
  if (invalidTokens > 0) return "模板仅允许使用 {1}..{n} 格式占位"
  const used = [...new Set(positions)].sort((a, b) => a - b)
  if (used.some((pos, index) => pos !== index + 1)) return "内容占位必须从 {1} 开始连续编号"
  const declared = form.var_specs.map((spec) => spec.pos)
  const missing = used.filter((pos) => !declared.includes(pos))
  const unused = declared.filter((pos) => !used.includes(pos))
  const issues: string[] = []
  if (missing.length) issues.push(`占位 ${missing.map((pos) => `{${pos}}`).join(" ")} 未声明变量最大长度`)
  if (unused.length) issues.push(`变量 ${unused.map((pos) => `{${pos}}`).join(" ")} 未在内容中使用`)
  return issues.join("；")
})

/** 变量声明随内容自动识别：占位集合变化时增删行，已设最大长度按 pos 保留。 */
function syncVariablesFromContent(): void {
  const { positions } = contentPlaceholders(form.content)
  const keep = new Map(form.var_specs.map((spec) => [spec.pos, spec.max_len]))
  form.var_specs = [...new Set(positions)]
    .sort((a, b) => a - b)
    .map((pos) => ({ pos, max_len: keep.get(pos) ?? DEFAULT_MAX_LEN }))
}

watch(() => form.content, syncVariablesFromContent)

const canSync = (item: SmsTemplate) =>
  item.vendor_state === "pending" && item.vendor_template_id !== null
const canEdit = (item: SmsTemplate) => ["draft", "rejected"].includes(item.vendor_state)
const canDelete = (item: SmsTemplate) => item.vendor_state !== "approved"
const canUse = (item: SmsTemplate) => item.vendor_state === "approved"

/** 详情抽屉审核轨迹：三态全部由 vendor_template_id 与 vendor_state 前端推导。 */
const detailTrail = computed<TrailStep[]>(() => {
  const item = detail.value
  if (!item) return []
  const bound = item.vendor_template_id !== null
  const submitted = item.vendor_state !== "draft"
  const result: TrailStep =
    item.vendor_state === "approved"
      ? { title: "厂商审核：已通过", desc: "GetTemplateState 同步", tone: "done" }
      : item.vendor_state === "rejected"
        ? { title: "厂商审核：已拒绝", desc: "GetTemplateState 同步", tone: "fail" }
        : {
            title: "厂商审核：等待结果",
            desc: bound ? "轮询同步 · 可手动同步" : "等待厂商绑定",
            tone: "wait",
          }
  return [
    {
      title: submitted ? "已提交送审" : "未送审",
      desc: "Outbox → realtime worker",
      tone: submitted ? "done" : "wait",
    },
    {
      title: bound ? "已绑定厂商编号" : "等待厂商绑定",
      desc: bound ? `#${item.vendor_template_id}` : "BindTemplate",
      tone: bound ? "done" : "wait",
    },
    result,
  ]
})

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    items.value = await listTemplates()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "模板列表加载失败"
  } finally {
    loading.value = false
  }
}

function openDetail(item: SmsTemplate): void {
  detail.value = item
  detailOpen.value = true
}

function resetEditor(item?: SmsTemplate): void {
  editingSource.value = item ?? null
  form.name = item?.name ?? ""
  form.content = item?.content ?? ""
  form.var_specs = item?.var_specs.map((spec) => ({ ...spec })) ?? []
  editorOpen.value = true
}

async function submit(): Promise<void> {
  if (!form.name.trim()) {
    ElMessage.warning("请填写模板名称")
    return
  }
  if (!form.content.trim()) {
    ElMessage.warning("请填写平台格式内容")
    return
  }
  if (contentIssue.value) {
    ElMessage.warning(contentIssue.value)
    return
  }
  saving.value = true
  try {
    const payload: TemplatePayload = {
      name: form.name.trim(),
      content: form.content,
      var_specs: form.var_specs,
    }
    if (editingSource.value === null) {
      await createTemplate(payload)
      ElMessage.success("模板已加入厂商审核队列")
    } else {
      await updateTemplate(editingSource.value.id, payload)
      ElMessage.success("模板已重新提交厂商审核")
    }
    editorOpen.value = false
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "模板提交失败")
  } finally {
    saving.value = false
  }
}

async function sync(item: SmsTemplate): Promise<void> {
  if (syncingId.value !== null) return
  syncingId.value = item.id
  try {
    await syncTemplate(item.id)
    ElMessage.success("审核状态同步请求已入队")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "审核状态同步失败")
  } finally {
    syncingId.value = null
  }
}

async function remove(item: SmsTemplate): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `确认删除模板「${item.name}」？删除后不可恢复；已通过审核或已被批次引用的模板不可删除。删除行为与操作人将写入审计日志。`,
      "删除模板",
      {
        type: "warning",
        confirmButtonText: "确认删除",
        cancelButtonText: "取消",
      },
    )
    await deleteTemplate(item.id)
    ElMessage.success("模板已删除 · 本次操作已记入审计")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "模板删除失败")
    }
  }
}

/** 已通过模板的唯一出路：跳人工发送并预选该模板（纯前端路由，/send 读取 query）。 */
function useForSend(item: SmsTemplate): void {
  void router?.push({ path: "/send", query: { template_id: String(item.id) } })
}

function editFromDetail(): void {
  if (!detail.value) return
  resetEditor(detail.value)
  detailOpen.value = false
}

onMounted(load)
</script>

<template>
  <section class="page-heading template-heading">
    <div>
      <p class="eyebrow">ASSETS / 内容治理</p>
      <h1>模板管理</h1>
      <p>平台使用 {1}..{n} 占位并为每个变量登记最大长度；提交后由服务端统一转换为厂商 {s长度} 格式送审，仅厂商审核通过的模板可在发送中使用。</p>
    </div>
    <el-button v-if="canWrite" data-testid="new-template" type="primary" @click="resetEditor()">新建模板</el-button>
  </section>

  <el-card shadow="never" class="template-card">
    <div class="template-toolbar filter-toolbar">
      <div class="template-filter-group">
        <span class="template-filter-label">厂商状态</span>
        <span class="template-state-seg" role="group" aria-label="厂商状态筛选">
          <button
            v-for="option in stateOptions"
            :key="option.value"
            type="button"
            :class="{ on: stateFilter === option.value }"
            :data-testid="`template-state-${option.value}`"
            @click="stateFilter = option.value"
          >
            {{ option.label }} <i>{{ option.count }}</i>
          </button>
        </span>
      </div>
      <div class="template-filter-group template-keyword-group">
        <span class="template-filter-label">关键词</span>
        <el-input
          v-model="keyword"
          class="template-keyword"
          data-testid="template-keyword"
          placeholder="名称 / 内容"
          clearable
        />
      </div>
    </div>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" class="template-table" :data="filtered" row-key="id" @row-click="openDetail">
      <el-table-column label="模板名称" min-width="180">
        <template #default="{ row }">
          <button
            :data-testid="`template-detail-${row.id}`"
            class="table-row-detail template-name"
            type="button"
            :aria-label="`查看模板 ${row.name} 的详情`"
            @click.stop="openDetail(row)"
            @keydown.enter.stop.prevent="openDetail(row)"
            @keydown.space.stop.prevent="openDetail(row)"
          >
            {{ row.name }}
          </button>
          <span class="cell-sub cell-sub-mono">平台 #{{ row.id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="内容" min-width="320">
        <template #default="{ row }">
          <span class="template-inline-content">
            <template v-for="(part, index) in contentParts(row.content, row.var_specs)" :key="index">
              <span
                v-if="part.pos !== undefined"
                class="var-chip"
                :title="`变量 {${part.pos}} · 最大 ${part.maxLen ?? '—'} 字`"
              >{{ "{" + part.pos + "}" }}<i>≤{{ part.maxLen ?? "—" }}</i></span>
              <template v-else>{{ part.text }}</template>
            </template>
          </span>
        </template>
      </el-table-column>
      <el-table-column v-if="isAdmin" label="部门" width="110">
        <template #default="{ row }"><span :class="{ muted: !row.dept }">{{ row.dept || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="厂商状态" width="170">
        <template #default="{ row }">
          <StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" />
          <span class="cell-sub" :class="{ 'cell-sub-verm': stateSub(row).tone === 'verm' }">{{ stateSub(row).text }}</span>
        </template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <div @click.stop>
            <el-button v-if="canUse(row)" :data-testid="`template-use-${row.id}`" link type="primary" @click="useForSend(row)">用于发送</el-button>
            <el-button v-if="canSync(row)" :data-testid="`template-sync-${row.id}`" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
            <el-button v-if="canEdit(row)" :data-testid="`template-edit-${row.id}`" link @click="resetEditor(row)">编辑</el-button>
            <el-button v-if="canDelete(row)" :data-testid="`template-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button>
          </div>
        </template>
      </el-table-column>
      <template #empty><EmptyState :title="emptyTitle" :description="emptyDescription" /></template>
    </el-table>
    <div v-loading="loading" class="template-mobile-list">
      <article v-for="row in filtered" :key="row.id">
        <header>
          <button
            :data-testid="`template-mobile-detail-${row.id}`"
            class="table-row-detail"
            type="button"
            :aria-label="`查看模板 ${row.name} 的详情`"
            @click="openDetail(row)"
          >
            {{ row.name }}
          </button>
          <StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" />
        </header>
        <p class="template-inline-content">
          <template v-for="(part, index) in contentParts(row.content, row.var_specs)" :key="index">
            <span
              v-if="part.pos !== undefined"
              class="var-chip"
              :title="`变量 {${part.pos}} · 最大 ${part.maxLen ?? '—'} 字`"
            >{{ "{" + part.pos + "}" }}<i>≤{{ part.maxLen ?? "—" }}</i></span>
            <template v-else>{{ part.text }}</template>
          </template>
        </p>
        <p class="cell-sub" :class="{ 'cell-sub-verm': stateSub(row).tone === 'verm' }">{{ stateSub(row).text }}</p>
        <dl v-if="isAdmin">
          <div><dt>所属部门</dt><dd :class="{ muted: !row.dept }">{{ row.dept || "—" }}</dd></div>
        </dl>
        <footer v-if="canWrite" @click.stop>
          <el-button v-if="canUse(row)" :data-testid="`template-mobile-use-${row.id}`" link type="primary" @click="useForSend(row)">用于发送</el-button>
          <el-button v-if="canSync(row)" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
          <el-button v-if="canEdit(row)" link @click="resetEditor(row)">编辑</el-button>
          <el-button v-if="canDelete(row)" link type="danger" @click="remove(row)">删除</el-button>
        </footer>
      </article>
      <EmptyState v-if="!loading && !filtered.length" :title="emptyTitle" :description="emptyDescription" />
    </div>
    <div class="template-foot">共 {{ filtered.length }} 个模板</div>
  </el-card>

  <el-drawer v-model="detailOpen" title="模板详情" size="min(560px, 94vw)">
    <template v-if="detail">
      <div class="template-detail-title">
        <StatusTag :status="detail.vendor_state" :label="stateLabel(detail.vendor_state)" />
        <b>{{ detail.name }}</b>
      </div>
      <div class="template-trail" data-testid="template-trail">
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
        description="修改内容或变量后重新提交，将生成新的厂商编号并重新进入审核。"
        type="error"
        :closable="false"
        class="template-reject-banner"
      />
      <div class="content-proof">
        <span>平台格式内容</span>
        <p class="template-inline-content">
          <template v-for="(part, index) in contentParts(detail.content, detail.var_specs)" :key="index">
            <span
              v-if="part.pos !== undefined"
              class="var-chip"
              :title="`变量 {${part.pos}} · 最大 ${part.maxLen ?? '—'} 字`"
            >{{ "{" + part.pos + "}" }}<i>≤{{ part.maxLen ?? "—" }}</i></span>
            <template v-else>{{ part.text }}</template>
          </template>
        </p>
        <small class="template-proof-hint">变量 {{ detail.var_specs.length }} 个 · 渲染时校验参数个数与长度</small>
      </div>
      <div class="content-proof">
        <span>厂商格式（提交时转换）</span>
        <p class="template-vendor-preview">{{ vendorPreviewOf(detail.content, detail.var_specs) }}</p>
      </div>
      <dl class="approval-detail-grid">
        <div><dt>平台编号</dt><dd class="mono-id">{{ detail.id }}</dd></div>
        <div><dt>厂商编号</dt><dd class="mono-id">{{ detail.vendor_template_id || "—" }}</dd></div>
        <div><dt>所属部门</dt><dd :class="{ muted: !detail.dept }">{{ detail.dept || "—" }}</dd></div>
        <div>
          <dt>变量声明</dt>
          <dd>
            <span v-if="!detail.var_specs.length" class="muted">无变量</span>
            <span v-for="spec in detail.var_specs" :key="spec.pos" class="var-chip">
              {{ "{" + spec.pos + "}" }}<i>≤{{ spec.max_len }}</i>
            </span>
          </dd>
        </div>
      </dl>
      <div v-if="canWrite" class="template-detail-actions">
        <el-button v-if="canEdit(detail)" data-testid="template-detail-edit" type="primary" @click="editFromDetail">修改并重新提交</el-button>
        <el-button v-if="canUse(detail)" data-testid="template-detail-use" type="primary" @click="useForSend(detail)">用于发送</el-button>
        <el-button v-if="canSync(detail)" data-testid="template-detail-sync" :loading="syncingId === detail.id" @click="sync(detail)">同步审核状态</el-button>
        <el-button v-if="canDelete(detail)" data-testid="template-detail-delete" link type="danger" @click="remove(detail)">删除模板</el-button>
        <p class="why">已通过审核的模板不可变更；重新提交后旧厂商编号作废。</p>
      </div>
    </template>
  </el-drawer>

  <el-drawer
    v-model="editorOpen"
    :title="editingSource === null ? '新建模板' : '重新提交模板'"
    size="min(560px, 94vw)"
  >
    <el-form label-position="top" class="template-form">
      <el-alert
        v-if="editingSource?.vendor_state === 'rejected' && editingSource.vendor_reject_reason"
        :title="`上次厂商驳回：${editingSource.vendor_reject_reason}`"
        description="驳回原因来自厂商 checkRemark，服务端入库前已做手机号打码。"
        type="error"
        :closable="false"
        class="template-reject-banner"
      />
      <el-form-item label="模板名称"><el-input v-model="form.name" maxlength="64" /></el-form-item>
      <el-form-item label="平台格式内容 · 渲染后全长 ≤500 字" :error="contentIssue || undefined">
        <el-input
          v-model="form.content"
          type="textarea"
          :rows="7"
          maxlength="500"
          show-word-limit
          placeholder="例如：尊敬的{1}，验证码{2}"
        />
      </el-form-item>
      <div class="variable-head">
        <b>变量声明</b>
        <span class="variable-head-note">随内容自动识别 · 已设长度保留</span>
      </div>
      <div v-for="spec in form.var_specs" :key="spec.pos" class="variable-row">
        <code>{{ "{" + spec.pos + "}" }}</code>
        <el-input-number v-model="spec.max_len" :min="1" :max="100" />
        <span>字</span>
        <span class="variable-note">渲染时参数超长即拒 · 范围 1–100</span>
      </div>
      <p v-if="!form.var_specs.length" class="variable-empty">内容中还没有占位变量，输入 {1} 起连续编号。</p>
      <p class="form-hint">占位与变量必须从 1 连续一一对应；变量长度即厂商 {s长度} 上限。</p>
      <div class="content-proof template-editor-preview">
        <span>厂商格式预览（提交时由服务端转换）</span>
        <p class="template-vendor-preview">{{ editorVendorPreview }}</p>
      </div>
    </el-form>
    <template #footer>
      <div class="template-editor-foot">
        <small>{{ editingSource === null
          ? "提交后进入厂商人工审核，期间不可编辑；审核结果由轮询同步，也可在列表手动同步。"
          : "重新提交将生成新的厂商编号，并重新进入厂商人工审核。" }}</small>
        <div>
          <el-button @click="editorOpen = false">取消</el-button>
          <el-button data-testid="template-submit" type="primary" :loading="saving" @click="submit">{{ editingSource === null ? "提交厂商审核" : "重新提交审核" }}</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
