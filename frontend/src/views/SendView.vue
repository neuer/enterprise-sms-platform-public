<script setup lang="ts">
import "../styles/workspace.css"

import type { UploadRequestOptions } from "element-plus"
import { computed, getCurrentInstance, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue"
import type { Router } from "vue-router"

import {
  previewBilling,
  downloadImportInvalidFile,
  sendWebMessage,
  uploadPhones,
  type BillingPreview,
  type Category,
  type ImportResult,
  type SendResult,
  type WebMessagePayload,
} from "../api/webMessages"
import { listTemplates, type SmsTemplate } from "../api/templates"
import { listSigns, type SmsSign } from "../api/signs"
import { getDashboard } from "../api/dashboard"
import SegmentBar from "../components/SegmentBar.vue"
import EmptyState from "../components/EmptyState.vue"

const router = getCurrentInstance()?.appContext.config.globalProperties.$router as Router | undefined

const form = reactive({
  category: "notice" as Category,
  source: "paste" as "paste" | "import",
  contentMode: "content" as "content" | "template",
  mobilesText: "",
  content: "",
  templateId: "",
  templateParamsText: "",
  signName: "",
  scheduledAt: "",
  isTest: false,
  consentConfirmed: false,
  remark: "",
})

const imported = ref<ImportResult | null>(null)
const importState = ref<"idle" | "parsing" | "ready" | "failed">("idle")
const importFilename = ref("")
const importError = ref("")
const preview = ref<BillingPreview | null>(null)
const previewLoading = ref(false)
const previewError = ref("")
const busy = ref(false)
const errorMessage = ref("")
const sendResult = ref<SendResult | null>(null)
const copied = ref(false)
/** biz_id 契约上限 32 字符；UUID 必须去连字符（36→32），否则服务端 400。 */
function newIdempotencyKey(): string {
  return crypto.randomUUID().replaceAll("-", "")
}

const idempotencyKey = ref(newIdempotencyKey())
const templates = ref<SmsTemplate[]>([])
const templateParams = ref<string[]>([])
const signs = ref<SmsSign[]>([])
const testSendMax = ref<number | null>(null)
const approvedTemplates = computed(() => templates.value.filter((item) => item.vendor_state === "approved"))
const approvedSigns = computed(() => signs.value.filter((item) => item.vendor_state === "approved"))
const selectedTemplate = computed(() => templates.value.find((item) => item.id === Number(form.templateId)) || null)
const renderedTemplate = computed(() => {
  const template = selectedTemplate.value
  if (!template) return ""
  return template.content.replace(/\{(\d+)\}/g, (placeholder, position: string) => {
    const value = templateParams.value[Number(position) - 1]?.trim()
    return value || placeholder
  })
})

const pastedMobiles = computed(() =>
  form.mobilesText
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean),
)

// 与服务端一致的 ^1\d{10}$；提交前即时暴露格式错误，避免整单被 400 拒绝却只看到笼统提示。
const invalidMobiles = computed(() => pastedMobiles.value.filter((value) => !/^1\d{10}$/.test(value)))
const dedupedCount = computed(() => new Set(pastedMobiles.value).size)
const duplicateCount = computed(() => pastedMobiles.value.length - dedupedCount.value)

const recipientCount = computed(() =>
  form.source === "import" ? (imported.value?.valid ?? 0) : pastedMobiles.value.length,
)

// 预检受众口径：导入取预检后有效数，粘贴取客户端去重后数量，与服务端批内去重对齐；
// 黑名单与频控剔除只有服务端受理时可判定，预检不做虚假精确。
const previewCount = computed(() =>
  form.source === "import" ? recipientCount.value : dedupedCount.value,
)

const contentReady = computed(() =>
  form.contentMode === "content"
    ? form.content.trim().length > 0
    : Number(form.templateId) > 0,
)

const testLimitExceeded = computed(
  () => form.isTest && testSendMax.value !== null && recipientCount.value > testSendMax.value,
)

const sendDisabled = computed(
  () =>
    busy.value ||
    recipientCount.value === 0 ||
    (form.source === "paste" && invalidMobiles.value.length > 0) ||
    !contentReady.value ||
    (form.category === "market" && !form.consentConfirmed) ||
    testLimitExceeded.value,
)
const submitLabel = computed(() => {
  const cost = preview.value ? ` · ${preview.value.quota_cost.toLocaleString()} 计费条` : ""
  if (preview.value?.approval_required) return `提交审批${cost}`
  if (form.scheduledAt || preview.value?.deferred_reason === "market_window") return `安排发送${cost}`
  return `立即发送${cost}`
})

const sendStatusLabel: Record<SendResult["status"], string> = {
  queued: "排队中",
  scheduled: "已排期",
  pending_approval: "待审批",
}

function deferredReasonText(reason: string): string {
  if (reason === "market_window") return "超出营销发送时间窗，已转为定时发送"
  return `窗外转定时原因：${reason}`
}

function sendSuccessText(result: SendResult): string {
  const parts = [`批次 ${result.batch_no} 已受理，状态：${sendStatusLabel[result.status]}`]
  if (result.idempotent) parts.push("本次为幂等命中，返回历史批次，未重复发送")
  if (result.deferred_reason) parts.push(deferredReasonText(result.deferred_reason))
  return parts.join("。")
}

function removedTotal(result: SendResult): number {
  return (
    (result.removed_duplicate ?? 0) +
    (result.removed_blacklist ?? 0) +
    (result.removed_freq_limit ?? 0)
  )
}

function contentPayload() {
  if (form.contentMode === "content") return { content: form.content }
  return {
    template_id: Number(form.templateId),
    template_params: templateParams.value.map((value) => value.trim()),
  }
}

// ── 自动预检：表单关键字段 600ms 防抖触发，竞态以指纹裁决，过期显式标记 ──
const previewKey = computed(() =>
  JSON.stringify({
    category: form.category,
    ...contentPayload(),
    signName: form.signName,
    count: previewCount.value,
    consent: form.consentConfirmed,
  }),
)
const lastPreviewKey = ref("")
let previewTimer: number | undefined

const previewReady = computed(
  () =>
    contentReady.value &&
    recipientCount.value > 0 &&
    (form.source === "paste" || importState.value === "ready") &&
    (form.category !== "market" || form.consentConfirmed) &&
    !(form.source === "paste" && invalidMobiles.value.length > 0),
)

const previewStale = computed(
  () => preview.value !== null && lastPreviewKey.value !== previewKey.value,
)

function isValidPreview(value: BillingPreview | null): value is BillingPreview {
  return (
    !!value &&
    typeof value.final_length === "number" &&
    Array.isArray(value.segment_parts) &&
    typeof value.final_content === "string"
  )
}

async function runPreview(key: string): Promise<void> {
  previewLoading.value = true
  previewError.value = ""
  try {
    const result = await previewBilling({
      category: form.category,
      ...contentPayload(),
      sign_name: form.signName || undefined,
      accepted_count: previewCount.value,
      consent_confirmed: form.consentConfirmed,
    })
    if (key !== previewKey.value) return
    if (isValidPreview(result)) {
      preview.value = result
      lastPreviewKey.value = key
    } else {
      preview.value = null
      lastPreviewKey.value = ""
    }
  } catch (error) {
    if (key !== previewKey.value) return
    previewError.value = error instanceof Error ? error.message : "预检失败"
  } finally {
    if (key === previewKey.value) previewLoading.value = false
  }
}

watch(previewKey, () => {
  window.clearTimeout(previewTimer)
  if (!previewReady.value) {
    preview.value = null
    previewError.value = ""
    previewLoading.value = false
    lastPreviewKey.value = ""
    return
  }
  previewTimer = window.setTimeout(() => void runPreview(previewKey.value), 600)
})

// 最终内容按构成拆分：签名 + 正文 + 服务端追加的退订语；对不上时整体平铺，不伪造高亮。
const finalParts = computed(() => {
  const current = preview.value
  if (!current) return null
  const text = current.final_content
  let sign = ""
  if (form.signName.trim()) {
    const raw = form.signName.trim()
    const formatted = raw.startsWith("【") && raw.endsWith("】") ? raw : `【${raw}】`
    if (text.startsWith(formatted)) sign = formatted
  }
  const rest = text.slice(sign.length)
  if (current.unsubscribe_appended) {
    const rendered = form.contentMode === "content" ? form.content : renderedTemplate.value
    if (rendered && rest.startsWith(rendered) && rest.length > rendered.length) {
      return { sign, body: rendered, suffix: rest.slice(rendered.length) }
    }
  }
  return { sign, body: rest, suffix: "" }
})

// ── 配额展示 ──
const quotaUsedPct = computed(() => {
  const quota = preview.value?.quota
  if (!quota || quota.limit <= 0) return 0
  return Math.min(100, (quota.used / quota.limit) * 100)
})
const quotaThisPct = computed(() => {
  const quota = preview.value?.quota
  const cost = preview.value?.quota_cost ?? 0
  if (!quota || quota.limit <= 0) return 0
  return Math.min(Math.max(0, 100 - quotaUsedPct.value), (cost / quota.limit) * 100)
})
const quotaAfterPct = computed(() => {
  const quota = preview.value?.quota
  const cost = preview.value?.quota_cost ?? 0
  if (!quota || quota.limit <= 0) return 0
  return Math.round(((quota.used + cost) / quota.limit) * 100)
})

// ── 风险与合规行 ──
interface RiskLine {
  tone: "warn" | "ok" | "info"
  title: string
  desc: string
}
const riskLines = computed<RiskLine[]>(() => {
  const lines: RiskLine[] = []
  if (preview.value?.approval_required) {
    lines.push({
      tone: "warn",
      title: form.category === "market" ? "达到营销审批阈值" : "达到通知审批阈值",
      desc: "提交后进入待审批队列，通过后排期发送；驳回自动回补配额。",
    })
  }
  if (preview.value?.deferred_reason === "market_window" && !form.isTest) {
    lines.push({
      tone: "warn",
      title: "当前处于营销时间窗外（默认 08:00–21:00）",
      desc: "提交后将自动转为下一发送窗口起点定时，到点前可取消。",
    })
  } else if (
    form.category === "market" &&
    !form.isTest &&
    !form.scheduledAt &&
    preview.value &&
    preview.value.deferred_reason === null
  ) {
    lines.push({
      tone: "info",
      title: "当前处于营销发送时间窗内",
      desc: "窗外提交将自动转下一窗口起点定时，预检会提前提示。",
    })
  }
  if (preview.value?.unsubscribe_appended) {
    lines.push({
      tone: "ok",
      title: "退订语已自动追加",
      desc: "最终内容末尾由服务端补齐退订语，计费在追加后的文本上进行。",
    })
  }
  if (form.isTest) {
    lines.push({
      tone: "info",
      title: "测试发送",
      desc: "豁免营销时间窗；黑名单、敏感词、频控与配额照常执行。",
    })
  } else if (form.scheduledAt) {
    lines.push({
      tone: "info",
      title: "定时发送",
      desc: "到点前可在批次列表取消；营销定时落在窗外将顺延至窗口起点。",
    })
  }
  return lines
})

watch(
  () =>
    JSON.stringify({
      category: form.category,
      source: form.source,
      contentMode: form.contentMode,
      mobilesText: form.mobilesText,
      content: form.content,
      templateId: form.templateId,
      templateParams: templateParams.value,
      signName: form.signName,
      scheduledAt: form.scheduledAt,
      isTest: form.isTest,
      consentConfirmed: form.consentConfirmed,
      remark: form.remark,
      importId: imported.value?.import_id ?? null,
    }),
  () => {
    idempotencyKey.value = newIdempotencyKey()
    sendResult.value = null
  },
)

watch(
  () => form.isTest,
  (enabled) => {
    // 测试发送与定时互斥（服务端同样拒绝组合），勾选时清除已选时间。
    if (enabled) form.scheduledAt = ""
  },
)

async function loadTemplates(): Promise<void> {
  try { templates.value = await listTemplates() }
  catch (error) { errorMessage.value = error instanceof Error ? error.message : "模板列表加载失败" }
}

async function loadSigns(): Promise<void> {
  try {
    const result = await listSigns()
    signs.value = Array.isArray(result) ? result : []
  } catch {
    signs.value = []
  }
}

async function loadUiPolicy(): Promise<void> {
  try {
    const result = await getDashboard()
    const value = result.ui_policy.test_send_max
    testSendMax.value = typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null
  } catch {
    testSendMax.value = null
  }
}

function selectTemplate(value: string | number): void {
  form.templateId = String(value)
  const template = templates.value.find((item) => item.id === Number(value))
  templateParams.value = template?.var_specs.map(() => "") || []
}

async function downloadInvalidFile(): Promise<void> {
  if (!imported.value?.invalid_download_url) return
  try {
    const blob = await downloadImportInvalidFile(imported.value.invalid_download_url)
    const url = URL.createObjectURL(blob)
    const anchor = document.createElement("a")
    anchor.href = url
    anchor.download = `sms-import-${imported.value.import_id}-invalid.csv`
    anchor.click()
    URL.revokeObjectURL(url)
  } catch (error) { errorMessage.value = error instanceof Error ? error.message : "剔除清单下载失败" }
}

function resetFeedback(): void {
  errorMessage.value = ""
}

function chooseCategory(category: Category): void {
  form.category = category
  if (category === "notice") form.consentConfirmed = false
  resetFeedback()
}

function removeDuplicates(): void {
  form.mobilesText = [...new Set(pastedMobiles.value)].join("\n")
}

function resetImport(): void {
  imported.value = null
  importState.value = "idle"
  importFilename.value = ""
  importError.value = ""
}

async function handleUpload(options: UploadRequestOptions): Promise<void> {
  resetFeedback()
  importState.value = "parsing"
  importFilename.value = options.file.name
  importError.value = ""
  busy.value = true
  try {
    imported.value = await uploadPhones(options.file)
    importState.value = "ready"
    form.source = "import"
    options.onSuccess(imported.value)
  } catch (error) {
    imported.value = null
    importState.value = "failed"
    importError.value = error instanceof Error ? error.message : "号码文件解析失败"
  } finally {
    busy.value = false
  }
}

function formatExpiry(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false })
}

async function submit(): Promise<void> {
  if (sendDisabled.value) return
  resetFeedback()
  busy.value = true
  const payload: WebMessagePayload = {
    category: form.category,
    biz_id: idempotencyKey.value,
    ...contentPayload(),
    sign_name: form.signName || undefined,
    scheduled_at: form.scheduledAt ? new Date(form.scheduledAt).toISOString() : undefined,
    is_test: form.isTest,
    consent_confirmed: form.consentConfirmed,
    remark: form.remark || undefined,
  }
  if (form.source === "import" && imported.value) payload.import_id = imported.value.import_id
  else payload.mobiles = pastedMobiles.value
  try {
    sendResult.value = await sendWebMessage(payload)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "发送受理失败"
  } finally {
    busy.value = false
  }
}

async function copyBatchNo(): Promise<void> {
  if (!sendResult.value) return
  try {
    await navigator.clipboard.writeText(sendResult.value.batch_no)
    copied.value = true
    window.setTimeout(() => { copied.value = false }, 1600)
  } catch {
    errorMessage.value = "复制失败，请手动选择批次号"
  }
}

function goBatches(): void {
  void router?.push("/batches")
}

function resetForAnother(): void {
  form.mobilesText = ""
  form.content = ""
  form.templateId = ""
  templateParams.value = []
  form.signName = ""
  form.scheduledAt = ""
  form.isTest = false
  form.consentConfirmed = false
  form.remark = ""
  resetImport()
  sendResult.value = null
  preview.value = null
  idempotencyKey.value = newIdempotencyKey()
}

onMounted(() => {
  void loadTemplates()
  void loadSigns()
  void loadUiPolicy()
})

onBeforeUnmount(() => {
  window.clearTimeout(previewTimer)
})
</script>

<template>
  <section class="page-heading send-heading">
    <div>
      <p class="eyebrow">DELIVERY / 人工下发</p>
      <h1>人工发送</h1>
      <p>号码仅在受控内存中处理，计费、审批与时间窗均由服务端裁决。</p>
    </div>
    <span class="security-note"><i></i> 敏感数据保护已启用 · 预检自动更新</span>
  </section>

  <div class="send-workbench">
    <div class="send-editor" aria-label="短信编辑区">
      <section class="form-panel">
        <header><span class="form-index">01</span><h2>消息类别</h2><small>类别策略矩阵决定队列、时间窗、审批与频控</small></header>
        <div class="category-switch">
          <button
            type="button"
            data-testid="category-verify"
            disabled
            aria-describedby="verify-web-hint"
          >
            <b>验证码短信<span class="cat-tag verify">VERIFY</span></b><span id="verify-web-hint">仅 API 渠道开放</span>
          </button>
          <button
            type="button"
            data-testid="category-notice"
            :class="{ selected: form.category === 'notice' }"
            @click="chooseCategory('notice')"
          >
            <b>通知短信<span class="cat-tag notice">NOTICE</span></b><span>实时通道 · 黑名单默认拦截 · ≥100 需审批</span>
          </button>
          <button
            type="button"
            data-testid="category-market"
            :class="{ selected: form.category === 'market' }"
            @click="chooseCategory('market')"
          >
            <b>营销短信<span class="cat-tag market">MARKET</span></b><span>批量通道 · 08:00–21:00 · 强制退订语 · ≥50 需审批</span>
          </button>
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">02</span><h2>收信号码</h2><small>单次最多 50,000 个</small>
          <el-radio-group v-model="form.source" class="compact-radio">
            <el-radio-button value="paste">手工粘贴</el-radio-button>
            <el-radio-button value="import">文件导入</el-radio-button>
          </el-radio-group>
        </header>
        <template v-if="form.source === 'paste'">
          <el-input
            v-model="form.mobilesText"
            type="textarea"
            :rows="5"
            resize="vertical"
            placeholder="每行一个手机号，也支持逗号或空格分隔"
          />
          <div v-if="pastedMobiles.length" class="phone-stats" data-testid="phone-stats">
            <span>共 <b>{{ pastedMobiles.length.toLocaleString() }}</b></span>
            <span>去重后 <b>{{ dedupedCount.toLocaleString() }}</b></span>
            <span :class="{ bad: invalidMobiles.length > 0 }">格式无效 <b>{{ invalidMobiles.length.toLocaleString() }}</b></span>
            <span :class="{ bad: duplicateCount > 0 }">重复 <b>{{ duplicateCount.toLocaleString() }}</b></span>
            <button v-if="duplicateCount > 0" type="button" class="stats-action" @click="removeDuplicates">移除重复</button>
          </div>
          <p v-if="invalidMobiles.length" class="mobiles-invalid-hint" data-testid="invalid-mobiles-hint">
            {{ invalidMobiles.length }} 个号码格式无效（需为 1 开头的 11 位数字），例如「{{ invalidMobiles[0] }}」；请修正后再提交。
          </p>
        </template>
        <div v-else class="upload-zone">
          <el-upload
            v-if="importState !== 'ready'"
            drag
            action="#"
            accept=".csv,.xlsx"
            :show-file-list="false"
            :http-request="handleUpload"
            :disabled="busy || importState === 'parsing'"
          >
            <strong>拖入 CSV / XLSX，或点击选择</strong>
            <span>≤10MB · ≤5万行 · 24小时有效</span>
          </el-upload>
          <p v-if="importState === 'parsing'" class="import-state" data-testid="import-parsing">
            <i class="import-spinner" aria-hidden="true"></i>
            正在解析 {{ importFilename }} …大文件流式解析，请稍候
          </p>
          <div v-if="importState === 'failed'" class="import-state failed" data-testid="import-failed">
            <p>{{ importError || "号码文件解析失败" }}</p>
            <button type="button" class="stats-action" @click="resetImport">重新上传</button>
          </div>
          <div v-if="importState === 'ready' && imported" class="import-summary">
            <b>{{ imported.valid.toLocaleString() }}</b> 有效
            <span>{{ imported.invalid.toLocaleString() }} 无效</span>
            <span>{{ imported.duplicate.toLocaleString() }} 重复</span>
            <span>{{ imported.blacklisted.toLocaleString() }} 黑名单</span>
            <small>有效期至 {{ formatExpiry(imported.expires_at) }}</small>
            <button v-if="imported.invalid_download_url" data-testid="download-invalid" type="button" class="download-link" @click="downloadInvalidFile">下载剔除清单</button>
            <button type="button" class="download-link" @click="resetImport">重新上传</button>
          </div>
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">03</span><h2>发送内容</h2><small>最终内容（含签名与退订语）不超过 500 字</small>
          <el-radio-group v-model="form.contentMode" class="compact-radio">
            <el-radio-button value="content">直接编辑</el-radio-button>
            <el-radio-button value="template">审核模板</el-radio-button>
          </el-radio-group>
        </header>
        <el-input v-if="form.contentMode === 'content'" v-model="form.content" type="textarea" :rows="6" maxlength="500" show-word-limit />
        <div v-else class="template-fields">
          <el-select v-model="form.templateId" data-testid="template-select" filterable placeholder="选择已审核模板" @change="selectTemplate">
            <el-option v-for="item in approvedTemplates" :key="item.id" :label="item.name" :value="String(item.id)" />
          </el-select>
          <template v-if="selectedTemplate">
            <el-input v-for="spec in selectedTemplate.var_specs" :key="spec.pos" v-model="templateParams[spec.pos - 1]" data-testid="template-param" :maxlength="spec.max_len" :placeholder="`参数 {${spec.pos}}，最多 ${spec.max_len} 字`" />
            <div class="template-render-preview"><span>模板渲染预览</span><p>{{ renderedTemplate }}</p></div>
          </template>
          <EmptyState v-else-if="!approvedTemplates.length" title="当前没有已审核模板" description="请先在模板管理中提交并通过厂商审核。" />
        </div>
        <div class="inline-fields">
          <el-select v-model="form.signName" data-testid="sign-select" clearable placeholder="签名（可选，不选为应用默认签名）">
            <el-option v-for="item in approvedSigns" :key="item.id" :label="item.name" :value="item.name" />
          </el-select>
          <el-input v-model="form.remark" maxlength="200" placeholder="发送备注（可选，写入批次与审计）" />
        </div>
      </section>

      <section class="form-panel">
        <header><span class="form-index">04</span><h2>发送选项</h2></header>
        <div class="option-fields">
          <el-date-picker
            v-model="form.scheduledAt"
            type="datetime"
            popper-class="qingluan-date-popper"
            value-format="YYYY-MM-DDTHH:mm:ss+08:00"
            placeholder="定时发送（可选）"
            :disabled="form.isTest"
          />
          <el-checkbox v-model="form.isTest" class="test-checkbox">
            测试发送（{{ testSendMax === null ? '号码上限暂不可用' : `最多 ${testSendMax} 个号码` }}，豁免营销时间窗）
          </el-checkbox>
        </div>
        <p v-if="testLimitExceeded" class="test-limit-hint" data-testid="test-limit-hint">
          测试发送最多 {{ testSendMax }} 个号码，当前 {{ recipientCount.toLocaleString() }} 个；请删减号码，或取消测试发送按正式批次提交。
        </p>
      </section>
    </div>

    <aside class="send-preview precheck" aria-label="发送预检">
      <div class="preview-head">
        <div><p class="eyebrow">SERVER PRECHECK</p><h2>发送预检</h2></div>
        <span v-if="previewLoading" class="preview-state">更新中…</span>
        <span v-else-if="previewStale" class="preview-state stale">已过期</span>
      </div>

      <section v-if="finalParts" class="rail-card">
        <header>最终内容 <small>用户实收</small></header>
        <p class="final-content" data-testid="final-content"><span v-if="finalParts.sign" class="fx-sign">{{ finalParts.sign }}</span>{{ finalParts.body }}<span v-if="finalParts.suffix" class="fx-suffix">{{ finalParts.suffix }}</span></p>
        <footer>
          <span>{{ preview?.final_length }} 字 · {{ preview?.est_segments }} 段</span>
          <span v-if="finalParts.suffix" class="fx-suffix-tag">退订语自动追加</span>
        </footer>
      </section>

      <section class="rail-card">
        <header>受众</header>
        <div class="audience-meter">
          <strong data-testid="recipient-count">{{ recipientCount.toLocaleString() }}</strong>
          <span>{{ form.source === "import" ? "导入有效号码" : "粘贴号码（受理时去重与剔除）" }}</span>
        </div>
        <p v-if="form.source === 'paste' && duplicateCount > 0" class="audience-note">
          重复 {{ duplicateCount.toLocaleString() }} 个，提交时由服务端剔除；黑名单与频控剔除在受理时判定。
        </p>
      </section>

      <section v-if="preview" class="rail-card">
        <header>计费 <small>services/billing.py 单点口径</small></header>
        <SegmentBar :parts="preview.segment_parts" />
        <div class="cost-line">
          <span class="fx">{{ previewCount.toLocaleString() }} × {{ preview.est_segments }} 段 =</span>
          <strong>{{ preview.quota_cost.toLocaleString() }}<small> 计费条</small></strong>
        </div>
        <p class="boundary-hint">再增加 {{ preview.next_segment_at }} 个字符进入下一计费段</p>
      </section>

      <section v-if="preview && preview.quota" class="rail-card">
        <header>部门日配额</header>
        <div class="quota-row">
          <span>今日已用 / 上限</span>
          <b>{{ preview.quota.limit > 0 ? `${preview.quota.used.toLocaleString()} / ${preview.quota.limit.toLocaleString()}` : `${preview.quota.used.toLocaleString()} / 不限` }}</b>
        </div>
        <template v-if="preview.quota.limit > 0">
          <div class="quota-bar"><i class="used" :style="{ width: `${quotaUsedPct}%` }"></i><i class="this" :style="{ width: `${quotaThisPct}%` }"></i></div>
          <p class="quota-foot">斜纹为本批预扣 {{ preview.quota_cost.toLocaleString() }}；提交后 {{ (preview.quota.used + preview.quota_cost).toLocaleString() }} / {{ preview.quota.limit.toLocaleString() }}（{{ quotaAfterPct }}%）</p>
        </template>
        <p v-else class="quota-foot">上限不限；本批预扣 {{ preview.quota_cost.toLocaleString() }} 计费条</p>
      </section>
      <section v-else-if="preview" class="rail-card quota-degraded">
        <header>部门日配额</header>
        <p>配额投影暂不可确认，提交时以服务端判定为准。</p>
      </section>

      <section v-if="riskLines.length" class="rail-card risk-card">
        <div v-for="(line, index) in riskLines" :key="index" class="risk-line" :class="line.tone">
          <b>{{ line.title }}</b>
          <small>{{ line.desc }}</small>
        </div>
      </section>

      <EmptyState
        v-if="!preview && !previewLoading"
        :title="form.category === 'market' && !form.consentConfirmed ? '勾选同意声明后自动预检' : '等待服务端预检'"
        description="填写号码与内容后，自动获取计费、配额和审批判断。"
      />
      <p v-if="previewError" class="preview-error">{{ previewError }}</p>

      <label v-if="form.category === 'market'" class="consent-panel" data-testid="market-consent">
        <el-checkbox v-model="form.consentConfirmed">我确认已获得接收用户的明确同意，向其发送营销信息</el-checkbox>
        <p>同意状态将写入审计；勾选行为与操作人留痕，未确认时平台拒绝受理（422 CONSENT_REQUIRED）。</p>
      </label>

      <div v-if="sendResult" class="send-result" data-testid="send-result">
        <header><i></i>已受理 · {{ sendStatusLabel[sendResult.status] }}</header>
        <div class="batch-row">
          <code>{{ sendResult.batch_no }}</code>
          <button type="button" @click="copyBatchNo">{{ copied ? "已复制" : "复制批次号" }}</button>
        </div>
        <p class="result-line">{{ sendSuccessText(sendResult) }}。</p>
        <p class="result-stats">受理 {{ sendResult.accepted.toLocaleString() }} · 剔除 {{ removedTotal(sendResult).toLocaleString() }} · 预扣 {{ sendResult.quota_cost.toLocaleString() }} 计费条</p>
        <div class="result-acts">
          <el-button type="primary" @click="goBatches">查看批次</el-button>
          <el-button @click="resetForAnother">再发一批</el-button>
        </div>
      </div>

      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

      <el-button
        v-if="!sendResult"
        type="primary"
        size="large"
        class="send-submit"
        :class="{ approval: preview?.approval_required }"
        data-testid="send-button"
        :disabled="sendDisabled"
        :loading="busy"
        @click="submit"
      >
        {{ submitLabel }}
      </el-button>
      <p class="submit-foot">提交即生成批次并预扣配额 · 24h 幂等键防重复下发</p>
    </aside>
  </div>
</template>
