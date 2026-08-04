<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

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

const session = useSessionStore()

const items = ref<SmsTemplate[]>([])
const loading = ref(false)
const saving = ref(false)
const syncingId = ref<number | null>(null)
const errorMessage = ref("")
const stateFilter = ref<TemplateState | "all">("all")
const detail = ref<SmsTemplate | null>(null)
const detailOpen = ref(false)
const editorOpen = ref(false)
const editingId = ref<number | null>(null)
const form = reactive<TemplatePayload>({ name: "", content: "", var_specs: [] })
const canWrite = computed(() => session.role === "operator" || session.role === "admin")

const filtered = computed(() =>
  stateFilter.value === "all"
    ? items.value
    : items.value.filter((item) => item.vendor_state === stateFilter.value),
)

function stateLabel(state: TemplateState): string {
  return { draft: "草稿", pending: "待审核", approved: "已通过", rejected: "已拒绝" }[state]
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
  editingId.value = item?.id ?? null
  form.name = item?.name ?? ""
  form.content = item?.content ?? ""
  form.var_specs = item?.var_specs.map((spec) => ({ ...spec })) ?? []
  editorOpen.value = true
}

function addVariable(): void {
  form.var_specs.push({ pos: form.var_specs.length + 1, max_len: 10 })
}

function removeVariable(index: number): void {
  form.var_specs.splice(index, 1)
  form.var_specs.forEach((spec, position) => (spec.pos = position + 1))
}

/** 按内容中的占位生成变量声明，保留已设置的最大长度。 */
function syncVariablesFromContent(): void {
  const { positions, invalidTokens } = contentPlaceholders(form.content)
  if (invalidTokens > 0 || !positions.length) return
  const keep = new Map(form.var_specs.map((spec) => [spec.pos, spec.max_len]))
  form.var_specs = [...new Set(positions)]
    .sort((a, b) => a - b)
    .map((pos) => ({ pos, max_len: keep.get(pos) ?? 10 }))
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
    if (editingId.value === null) await createTemplate(payload)
    else await updateTemplate(editingId.value, payload)
    editorOpen.value = false
    ElMessage.success("模板已提交厂商审核")
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
    ElMessage.success("审核状态已同步")
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "审核状态同步失败")
  } finally {
    syncingId.value = null
  }
}

async function remove(item: SmsTemplate): Promise<void> {
  try {
    await ElMessageBox.confirm("确认删除模板「" + item.name + "」？", "删除模板", {
      type: "warning",
      confirmButtonText: "确认删除",
      cancelButtonText: "取消",
    })
    await deleteTemplate(item.id)
    ElMessage.success("模板已删除")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "模板删除失败")
    }
  }
}

onMounted(load)
</script>

<template>
  <section class="page-heading template-heading">
    <div>
      <p class="eyebrow">ASSETS / 内容治理</p>
      <h1>模板管理</h1>
      <p>平台使用 {1}..{n} 占位；变量长度与厂商格式由服务端统一校验和转换。</p>
    </div>
    <el-button v-if="canWrite" data-testid="new-template" type="primary" @click="resetEditor()">新建模板</el-button>
  </section>

  <el-card shadow="never" class="template-card">
    <div class="template-toolbar filter-toolbar">
      <el-segmented
        v-model="stateFilter"
        :options="[
          { label: '全部', value: 'all' },
          { label: '草稿', value: 'draft' },
          { label: '待审核', value: 'pending' },
          { label: '已通过', value: 'approved' },
          { label: '已拒绝', value: 'rejected' },
        ]"
      />
      <span>{{ filtered.length }} 个模板</span>
    </div>
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />
    <el-table v-loading="loading" class="template-table" :data="filtered" row-key="id" @row-click="openDetail">
      <el-table-column label="模板名称" min-width="150">
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
        </template>
      </el-table-column>
      <el-table-column label="平台内容" min-width="280">
        <template #default="{ row }"><code class="template-content">{{ row.content }}</code></template>
      </el-table-column>
      <el-table-column label="变量约束" min-width="180">
        <template #default="{ row }">
          <span v-if="!row.var_specs.length" class="muted">无变量</span>
          <span v-for="spec in row.var_specs" :key="spec.pos" class="var-chip">
            {{ "{" + spec.pos + "}" }} · 最大 {{ spec.max_len }} 字
          </span>
        </template>
      </el-table-column>
      <el-table-column label="部门" min-width="120">
        <template #default="{ row }"><span :class="{ muted: !row.dept }">{{ row.dept || "—" }}</span></template>
      </el-table-column>
      <el-table-column label="厂商状态" width="110">
        <template #default="{ row }"><StatusTag :status="row.vendor_state" :label="stateLabel(row.vendor_state)" /></template>
      </el-table-column>
      <el-table-column v-if="canWrite" label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <div @click.stop>
            <el-button v-if="row.vendor_state === 'pending'" :data-testid="`template-sync-${row.id}`" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
            <el-button v-if="['draft', 'rejected'].includes(row.vendor_state)" :data-testid="`template-edit-${row.id}`" link @click="resetEditor(row)">编辑</el-button>
            <el-button v-if="row.vendor_state !== 'approved'" :data-testid="`template-delete-${row.id}`" link type="danger" @click="remove(row)">删除</el-button>
            <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的模板不可变更">—</span>
          </div>
        </template>
      </el-table-column>
      <template #empty><EmptyState title="当前没有模板" description="新建模板后可在这里跟踪厂商审核状态。" /></template>
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
        <code>{{ row.content }}</code>
        <dl>
          <div>
            <dt>变量约束</dt>
            <dd v-if="!row.var_specs.length" class="muted">无变量</dd>
            <dd v-else>
              <span v-for="spec in row.var_specs" :key="spec.pos" class="var-chip">
                {{ "{" + spec.pos + "}" }} · 最大 {{ spec.max_len }} 字
              </span>
            </dd>
          </div>
          <div><dt>所属部门</dt><dd :class="{ muted: !row.dept }">{{ row.dept || "—" }}</dd></div>
        </dl>
        <footer v-if="canWrite" @click.stop>
          <el-button v-if="row.vendor_state === 'pending'" link type="primary" :loading="syncingId === row.id" @click="sync(row)">同步</el-button>
          <el-button v-if="['draft', 'rejected'].includes(row.vendor_state)" link @click="resetEditor(row)">编辑</el-button>
          <el-button v-if="row.vendor_state !== 'approved'" link type="danger" @click="remove(row)">删除</el-button>
          <span v-if="row.vendor_state === 'approved'" class="muted" title="已通过审核的模板不可变更">—</span>
        </footer>
      </article>
      <EmptyState
        v-if="!loading && !filtered.length"
        title="当前没有模板"
        description="新建模板后可在这里跟踪厂商审核状态。"
      />
    </div>
  </el-card>

  <el-drawer v-model="detailOpen" title="模板详情" size="min(500px, 92vw)">
    <template v-if="detail">
      <div class="template-detail-title">
        <StatusTag :status="detail.vendor_state" :label="stateLabel(detail.vendor_state)" />
        <b>{{ detail.name }}</b>
      </div>
      <dl class="approval-detail-grid">
        <div><dt>平台编号</dt><dd>{{ detail.id }}</dd></div>
        <div><dt>厂商编号</dt><dd>{{ detail.vendor_template_id || "—" }}</dd></div>
        <div><dt>所属部门</dt><dd :class="{ muted: !detail.dept }">{{ detail.dept || "—" }}</dd></div>
      </dl>
      <div class="content-proof"><span>平台格式内容</span><p>{{ detail.content }}</p></div>
      <div class="content-proof">
        <span>变量约束</span>
        <div class="template-detail-vars">
          <span v-if="!detail.var_specs.length" class="muted">无变量</span>
          <span v-for="spec in detail.var_specs" :key="spec.pos" class="var-chip">
            {{ "{" + spec.pos + "}" }} · 最大 {{ spec.max_len }} 字
          </span>
        </div>
      </div>
      <el-alert
        v-if="detail.vendor_reject_reason"
        :title="detail.vendor_reject_reason"
        type="error"
        :closable="false"
      />
    </template>
  </el-drawer>

  <el-drawer
    v-model="editorOpen"
    :title="editingId === null ? '新建模板' : '重新提交模板'"
    size="min(560px, 94vw)"
  >
    <el-form label-position="top" class="template-form">
      <p class="drawer-intro">
        {{ editingId === null
          ? "提交后由服务端转换为厂商格式并送审，审核期间不可编辑。"
          : "重新提交将生成新的厂商模板编号并重新进入审核。" }}
      </p>
      <el-form-item label="模板名称"><el-input v-model="form.name" maxlength="64" /></el-form-item>
      <el-form-item label="平台格式内容" :error="contentIssue || undefined">
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
        <b>变量最大长度</b>
        <div>
          <el-button data-testid="template-sync-vars" @click="syncVariablesFromContent">从内容识别</el-button>
          <el-button @click="addVariable">添加变量</el-button>
        </div>
      </div>
      <div v-for="(spec, index) in form.var_specs" :key="spec.pos" class="variable-row">
        <code>{{ "{" + spec.pos + "}" }}</code>
        <el-input-number v-model="spec.max_len" :min="1" :max="100" />
        <span>字</span>
        <el-button link type="danger" @click="removeVariable(index)">移除</el-button>
      </div>
      <p class="form-hint">占位与变量必须从 1 连续一一对应；提交后由服务端转换为厂商格式。</p>
    </el-form>
    <template #footer>
      <el-button @click="editorOpen = false">取消</el-button>
      <el-button data-testid="template-submit" type="primary" :loading="saving" @click="submit">提交厂商审核</el-button>
    </template>
  </el-drawer>
</template>
