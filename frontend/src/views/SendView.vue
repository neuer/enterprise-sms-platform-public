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
  scheduleEnabled: false,
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

const renderedTemplateParts = computed(() => {
  const template = selectedTemplate.value
  if (!template) return []
  const parts: { text: string; highlight: boolean }[] = []
  const pattern = /\{(\d+)\}/g
  let cursor = 0
  for (const match of template.content.matchAll(pattern)) {
    const index = match.index ?? 0
    if (index > cursor) parts.push({ text: template.content.slice(cursor, index), highlight: false })
    const value = templateParams.value[Number(match[1]) - 1]?.trim()
    parts.push({ text: value || match[0], highlight: Boolean(value) })
    cursor = index + match[0].length
  }
  if (cursor < template.content.length) parts.push({ text: template.content.slice(cursor), highlight: false })
  return parts
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
const scheduledAtValue = computed(() => (form.scheduleEnabled && form.scheduledAt ? form.scheduledAt : ""))

const submitLabel = computed(() => {
  const cost = preview.value ? ` · ${preview.value.quota_cost.toLocaleString()} 计费条` : ""
  if (preview.value?.approval_required) return `提交审批${cost}`
  if (scheduledAtValue.value || preview.value?.deferred_reason === "market_window") return `安排发送${cost}`
  return `立即发送${cost}`
})

const audienceCount = computed(() => previewCount.value)
const removedDuplicate = computed(() =>
  form.source === "import" ? (imported.value?.duplicate ?? 0) : duplicateCount.value,
)
const removedBlacklist = computed(() =>
  form.source === "import" ? (imported.value?.blacklisted ?? 0) : null,
)
const nextSegmentHint = computed(() => {
  const current = preview.value
  if (!current) return "下一段"
  return `再 ${current.next_segment_at} 字进入第 ${current.segment_parts.length + 1} 段`
})
const testSendHint = computed(() =>
  testSendMax.value === null ? "号码上限暂不可用" : `≤${testSendMax.value} 个号码 · 豁免营销时间窗 · 其余管控照常`,
)

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
      title: "当前处于营销时间窗内（08:00–21:00）",
      desc: "窗外提交将自动转次日窗口起点定时，预检会提前提示。",
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
  } else if (scheduledAtValue.value) {
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
      scheduleEnabled: form.scheduleEnabled,
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
    if (enabled) {
      form.scheduleEnabled = false
      form.scheduledAt = ""
    }
  },
)

watch(
  () => form.scheduleEnabled,
  (enabled) => {
    if (!enabled) form.scheduledAt = ""
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
    scheduled_at: scheduledAtValue.value ? new Date(scheduledAtValue.value).toISOString() : undefined,
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
  form.scheduleEnabled = false
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
        <header>
          <span class="form-index">01</span>
          <h2>消息类别</h2>
          <small>类别策略矩阵决定队列、时间窗、审批与频控</small>
        </header>
        <div class="category-switch">
          <button
            type="button"
            class="notice"
            data-testid="category-notice"
            :class="{ on: form.category === 'notice', selected: form.category === 'notice' }"
            @click="chooseCategory('notice')"
          >
            <b>通知短信<span class="cat-tag notice">NOTICE</span></b>
            <small>实时通道 · 黑名单默认拦截<br>≥100 号码需审批</small>
          </button>
          <button
            type="button"
            class="market"
            data-testid="category-market"
            :class="{ on: form.category === 'market', selected: form.category === 'market' }"
            @click="chooseCategory('market')"
          >
            <b>营销短信<span class="cat-tag market">MARKET</span></b>
            <small>批量通道 · 08:00–21:00 · 强制退订语<br>≥50 号码需审批 · 同号同应用 1 条/天</small>
          </button>
          <button
            type="button"
            class="verify"
            data-testid="category-verify"
            disabled
            aria-describedby="verify-web-hint"
          >
            <b>验证码<span class="cat-tag verify">VERIFY</span></b>
            <small id="verify-web-hint">仅 API 渠道开放</small>
            <span class="cat-why">Web 人工发送不可用</span>
          </button>
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">02</span>
          <h2>收信号码</h2>
          <small>单次最多 50,000 个</small>
          <span class="seg" role="group" aria-label="号码来源">
            <button type="button" :class="{ on: form.source === 'paste' }" @click="form.source = 'paste'">手工粘贴</button>
            <button type="button" :class="{ on: form.source === 'import' }" @click="form.source = 'import'">文件导入</button>
          </span>
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
            <span class="bad">格式无效 <b>{{ invalidMobiles.length.toLocaleString() }}</b></span>
            <span>重复 <b>{{ duplicateCount.toLocaleString() }}</b></span>
            <span class="act">
              <button v-if="duplicateCount > 0" type="button" @click="removeDuplicates">移除重复</button>
            </span>
          </div>
          <p v-if="invalidMobiles.length" class="mobiles-invalid-hint" data-testid="invalid-mobiles-hint">
            {{ invalidMobiles.length }} 个号码格式无效（需为 1 开头的 11 位数字），例如「{{ invalidMobiles[0] }}」；请修正后再提交。
          </p>
        </template>
        <div v-else class="upload-zone">
          <el-upload
            v-if="importState === 'idle'"
            drag
            action="#"
            accept=".csv,.xlsx"
            :show-file-list="false"
            :http-request="handleUpload"
            :disabled="busy"
          >
            <strong>拖入 CSV / XLSX，或点击选择</strong>
            <span>≤10MB · ≤5万行 · 24小时有效</span>
          </el-upload>
          <div v-if="importState === 'parsing'" class="import-box" data-testid="import-parsing">
            <div class="import-processing">
              <i class="import-spinner" aria-hidden="true"></i>
              <div>
                <p>正在解析 {{ importFilename }} …</p>
                <small>大文件流式解析，请稍候</small>
              </div>
            </div>
          </div>
          <div v-if="importState === 'failed'" class="import-box failed" data-testid="import-failed">
            <p>{{ importError || "号码文件解析失败" }}</p>
            <button type="button" class="text-action" @click="resetImport">重新上传</button>
          </div>
          <div v-if="importState === 'ready' && imported" class="import-box" data-testid="import-ready">
            <div class="import-ready">
              <div class="cell"><span>有效</span><b>{{ imported.valid.toLocaleString() }}</b></div>
              <div class="cell" :class="{ bad: imported.invalid > 0 }"><span>格式无效</span><b>{{ imported.invalid.toLocaleString() }}</b></div>
              <div class="cell" :class="{ bad: imported.duplicate > 0 }"><span>重复</span><b>{{ imported.duplicate.toLocaleString() }}</b></div>
              <div class="cell" :class="{ bad: imported.blacklisted > 0 }"><span>黑名单</span><b>{{ imported.blacklisted.toLocaleString() }}</b></div>
              <div class="act">
                <button v-if="imported.invalid_download_url" data-testid="download-invalid" type="button" @click="downloadInvalidFile">下载剔除清单</button>
                <button type="button" @click="resetImport">重新上传</button>
              </div>
            </div>
            <p class="import-meta">
              {{ importFilename || "导入文件" }} · 解析完成 · 导入包 24 小时内有效（至 {{ formatExpiry(imported.expires_at) }}）· 频控剔除在受理时判定
            </p>
          </div>
        </div>
      </section>

      <section class="form-panel">
        <header>
          <span class="form-index">03</span>
          <h2>发送内容</h2>
          <small>最终内容（含签名与退订语）不超过 500 字</small>
          <span class="seg" role="group" aria-label="内容来源">
            <button type="button" :class="{ on: form.contentMode === 'content' }" @click="form.contentMode = 'content'">直接编辑</button>
            <button type="button" :class="{ on: form.contentMode === 'template' }" @click="form.contentMode = 'template'">审核模板</button>
          </span>
        </header>
        <el-input v-if="form.contentMode === 'content'" v-model="form.content" type="textarea" :rows="4" maxlength="500" />
        <div v-else class="template-fields">
          <el-select v-model="form.templateId" data-testid="template-select" filterable placeholder="选择已审核模板" @change="selectTemplate">
            <el-option v-for="item in approvedTemplates" :key="item.id" :label="item.name" :value="String(item.id)" />
          </el-select>
          <template v-if="selectedTemplate">
            <div class="tpl-params">
              <el-input v-for="spec in selectedTemplate.var_specs" :key="spec.pos" v-model="templateParams[spec.pos - 1]" data-testid="template-param" :maxlength="spec.max_len" :placeholder="`参数 {${spec.pos}}，最多 ${spec.max_len} 字`" />
            </div>
            <div class="template-render-preview">
              <span>模板渲染预览</span>
              <p>
                <template v-for="(part, index) in renderedTemplateParts" :key="index">
                  <em v-if="part.highlight">{{ part.text }}</em><template v-else>{{ part.text }}</template>
                </template>
              </p>
            </div>
          </template>
          <EmptyState v-else-if="!approvedTemplates.length" title="当前没有已审核模板" description="请先在模板管理中提交并通过厂商审核。" />
        </div>
        <div class="inline-fields">
          <el-select v-model="form.signName" data-testid="sign-select" clearable placeholder="不指定 · 用应用默认签名">
            <el-option v-for="item in approvedSigns" :key="item.id" :label="`签名：${item.name}（已过审）`" :value="item.name" />
          </el-select>
          <el-input v-model="form.remark" maxlength="200" placeholder="发送备注（可选，写入批次与审计）" />
        </div>
      </section>

      <section class="form-panel">
        <header><span class="form-index">04</span><h2>发送选项</h2></header>
        <div class="opt-row">
          <label class="opt">
            <input v-model="form.scheduleEnabled" type="checkbox" :disabled="form.isTest">
            <span>定时发送<small>不勾选为立即发送；营销窗外自动转定时</small></span>
          </label>
          <el-date-picker
            v-model="form.scheduledAt"
            type="datetime"
            popper-class="qingluan-date-popper"
            value-format="YYYY-MM-DDTHH:mm:ss+08:00"
            placeholder="选择时间（可选）"
            :disabled="form.isTest || !form.scheduleEnabled"
          />
          <label class="opt">
            <input v-model="form.isTest" type="checkbox">
            <span>测试发送<small>{{ testSendHint }}</small></span>
          </label>
        </div>
        <p v-if="testLimitExceeded" class="test-limit-hint" data-testid="test-limit-hint">
          测试发送最多 {{ testSendMax }} 个号码，当前 {{ recipientCount.toLocaleString() }} 个；请删减号码，或取消测试发送按正式批次提交。
        </p>
      </section>
    </div>

    <aside class="send-preview precheck send-rail" aria-label="发送确认">
      <span v-if="previewLoading" class="preview-state">更新中…</span>
      <span v-else-if="previewStale" class="preview-state stale">已过期</span>

      <section v-if="finalParts" class="rail-card">
        <header>最终内容预览 <small>用户实收</small></header>
        <p class="final-content" data-testid="final-content"><span v-if="finalParts.sign" class="fx-sign">{{ finalParts.sign }}</span>{{ finalParts.body }}<span v-if="finalParts.suffix" class="fx-suffix">{{ finalParts.suffix }}</span></p>
        <footer class="final-meta">
          <div class="legend">
            <span><i class="g"></i>签名</span>
            <span v-if="finalParts.suffix"><i class="a"></i>退订语 · 服务端自动追加</span>
          </div>
          <span class="mono">{{ preview?.final_length }} 字 · {{ preview?.est_segments }} 段</span>
        </footer>
      </section>

      <section class="rail-card">
        <header>受众</header>
        <div class="audience-meter">
          <strong data-testid="recipient-count">{{ audienceCount.toLocaleString() }}</strong>
          <span>受理号码（去重与黑名单剔除后）</span>
        </div>
        <div class="removed" data-testid="audience-removed">
          <span>重复 <b>{{ removedDuplicate.toLocaleString() }}</b></span>
          <span v-if="removedBlacklist !== null">黑名单 <b>{{ removedBlacklist.toLocaleString() }}</b></span>
          <span v-else>黑名单 <small>受理时判定</small></span>
          <span>频控 <small>受理时判定</small></span>
        </div>
      </section>

      <section v-if="preview" class="rail-card">
        <header>计费 <small>services/billing.py 单点口径</small></header>
        <SegmentBar :parts="preview.segment_parts" :next-hint="nextSegmentHint" />
        <div class="cost-line">
          <span class="fx">{{ previewCount.toLocaleString() }} × {{ preview.est_segments }} 段 =</span>
          <strong>{{ preview.quota_cost.toLocaleString() }}<small>计费条</small></strong>
        </div>
        <p class="boundary">第 {{ preview.segment_parts.length }} 段已用 {{ preview.segment_parts.at(-1)?.used }}/{{ preview.segment_parts.at(-1)?.capacity }} 字，再增加 {{ preview.next_segment_at }} 字进入第 {{ preview.segment_parts.length + 1 }} 段。</p>
      </section>

      <section v-if="preview && preview.quota" class="rail-card">
        <header>部门日配额</header>
        <div class="quota-row">
          <span>今日已用 / 上限</span>
          <b>{{ preview.quota.limit > 0 ? `${preview.quota.used.toLocaleString()} / ${preview.quota.limit.toLocaleString()}` : `${preview.quota.used.toLocaleString()} / 不限` }}</b>
        </div>
        <template v-if="preview.quota.limit > 0">
          <div class="quota-bar"><i class="used" :style="{ width: `${quotaUsedPct}%` }"></i><i class="this" :style="{ width: `${quotaThisPct}%` }"></i></div>
          <div class="quota-foot">
            <span>斜纹 = 本批预扣 {{ preview.quota_cost.toLocaleString() }}</span>
            <span>提交后 {{ (preview.quota.used + preview.quota_cost).toLocaleString() }} / {{ preview.quota.limit.toLocaleString() }}（{{ quotaAfterPct }}%）</span>
          </div>
        </template>
        <p v-else class="quota-foot">上限不限；本批预扣 {{ preview.quota_cost.toLocaleString() }} 计费条</p>
      </section>
      <section v-else-if="preview" class="rail-card quota-degraded">
        <header>部门日配额</header>
        <p><b>配额投影暂不可确认。</b>用量账本重建中，预览不阻断；提交时以发送入口判定为准。</p>
      </section>

      <section v-if="riskLines.length" class="rail-card">
        <header>风险与合规</header>
        <div class="risk-lines">
          <div v-for="(line, index) in riskLines" :key="index" class="risk-line" :class="line.tone">
            <b>{{ line.title }}</b>
            <small>{{ line.desc }}</small>
          </div>
        </div>
      </section>

      <EmptyState
        v-if="!preview && !previewLoading"
        :title="form.category === 'market' && !form.consentConfirmed ? '勾选同意声明后自动预检' : '等待服务端预检'"
        description="填写号码与内容后，自动获取计费、配额和审批判断。"
      />
      <p v-if="previewError" class="preview-error">{{ previewError }}</p>

      <label v-if="form.category === 'market'" class="consent-panel" data-testid="market-consent">
        <input v-model="form.consentConfirmed" type="checkbox">
        <span>
          <b>我确认以上收信人已同意接收营销信息。</b>
          <p>勾选行为与操作人将写入审计日志；未确认时平台拒绝受理（422 CONSENT_REQUIRED）。</p>
        </span>
      </label>

      <div v-if="sendResult" class="send-result" data-testid="send-result">
        <header><i></i>已受理 · {{ sendStatusLabel[sendResult.status] }}</header>
        <div class="batch-row">
          <code>{{ sendResult.batch_no }}</code>
          <button type="button" @click="copyBatchNo">{{ copied ? "已复制" : "复制批次号" }}</button>
        </div>
        <p class="result-line">{{ sendSuccessText(sendResult) }}。</p>
        <div class="result-stats">
          <span>受理 <b>{{ sendResult.accepted.toLocaleString() }}</b></span>
          <span>剔除 <b>{{ removedTotal(sendResult).toLocaleString() }}</b></span>
          <span>预扣 <b>{{ sendResult.quota_cost.toLocaleString() }}</b> 条</span>
        </div>
        <div class="result-acts">
          <el-button type="primary" @click="goBatches">查看批次</el-button>
          <el-button @click="resetForAnother">再发一批</el-button>
        </div>
      </div>

      <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" />

      <div v-if="!sendResult" class="submit-wrap">
        <el-button
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
      </div>
      <p v-else class="submit-foot">提交即生成批次并预扣配额 · 24h 幂等键防重复下发</p>
    </aside>
  </div>
</template>
