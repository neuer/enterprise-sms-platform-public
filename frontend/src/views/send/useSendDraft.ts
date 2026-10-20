import type { UploadRequestOptions } from "element-plus"
import { ElMessage } from "element-plus"
import { computed, reactive, ref, toRef, watch } from "vue"

import { ApiRequestError } from "../../api/client"
import { listSigns } from "../../api/signs"
import { listTemplates } from "../../api/templates"
import {
  downloadImportInvalidFile,
  sendWebMessage,
  uploadPhones,
  type BillingPreview,
  type Category,
  type ImportResult,
  type SendResult,
  type WebMessagePayload,
} from "../../api/webMessages"
import { useApprovedResources } from "../../composables/useApprovedResources"
import { useDebouncedEntries } from "../../composables/useDebouncedEntries"
import { useSendPreview } from "./useSendPreview"
import { admissionReasonOf, admissionReasonText } from "../../lib/admissionReason"
import { saveBlob } from "../../lib/download"
import { errorText } from "../../lib/error"
import { newIdempotencyKey } from "../../lib/idempotency"
import { PHONE_RE } from "../../lib/phone"
import { renderPreview, splitPreviewParts } from "../../lib/templatePreview"
import { formatDateTime, toApiDateTime } from "../../lib/time"

// 与服务端 web_messages.py 的 50_000 上限同口径；客户端只提前提示，服务端仍为权威裁决（规则 40 上界）。
const MAX_RECIPIENTS_PER_BATCH = 50_000

// ── 号码解析：共享 useDebouncedEntries 单点（#596 模式）——≤2000 字的小文本同步解析，
// 校验提示与计数即时反馈不变；更大粘贴（可能数万行）300ms 防抖，避免逐键全量 split/Set 阻塞输入。 ──
function parseMobiles(text: string): string[] {
  return text
    .split(/[\s,，;；]+/)
    .map((value) => value.trim())
    .filter(Boolean)
}

/**
 * 人工发送草稿：表单、号码解析与导入、校验、幂等键轮换、提交与会话清空。
 * 工厂函数每次调用新建状态；号码明文只存在于本次调用返回的组件级 ref 中，不进入任何持久层。
 */
export function useSendDraft() {
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
  const busy = ref(false)
  let requestGeneration = 0
  let draftRevision = 0
  let disposed = false
  let uploadAbort: AbortController | null = null
  const submittedSummary = ref("")
  const errorMessage = ref("")
  // 准入 503（DEPENDENCY_UNAVAILABLE）时展示通往运维中心队列页签的出口。
  const errorAdmission = ref(false)
  const sendResult = ref<SendResult | null>(null)
  const idempotencyKey = ref(newIdempotencyKey())
  const {
    items: templates,
    approved: approvedTemplates,
    load: loadTemplates,
  } = useApprovedResources(listTemplates, (error) => {
    errorMessage.value = errorText(error, "模板列表加载失败")
  })
  const templateParams = ref<string[]>([])
  // 签名是辅助加载：失败不阻断发送，但必须可见，否则用户只看到空下拉而不知原因。
  const { approved: approvedSigns, load: loadSigns } = useApprovedResources(listSigns, () => {
    ElMessage.warning("签名列表加载失败，可刷新页面重试；不指定签名时使用应用默认签名")
  })
  const testSendMax = ref<number | null>(null)
  const selectedTemplate = computed(() => templates.value.find((item) => item.id === Number(form.templateId)) || null)
  const renderedTemplate = computed(() => renderPreview(selectedTemplate.value?.content ?? "", templateParams.value))
  const renderedTemplateParts = computed(() =>
    splitPreviewParts(selectedTemplate.value?.content ?? "", templateParams.value),
  )

  const { entries: pastedMobiles, flush: flushMobilesParse } = useDebouncedEntries(toRef(form, "mobilesText"), {
    parse: parseMobiles,
  })

  // 与服务端一致的 ^1\d{10}$（lib/phone 单点）；提交前即时暴露格式错误，避免整单被 400 拒绝却只看到笼统提示。
  const invalidMobiles = computed(() => pastedMobiles.value.filter((value) => !PHONE_RE.test(value)))
  const dedupedCount = computed(() => new Set(pastedMobiles.value).size)
  const duplicateCount = computed(() => pastedMobiles.value.length - dedupedCount.value)

  const recipientCount = computed(() =>
    form.source === "import" ? (imported.value?.valid ?? 0) : pastedMobiles.value.length,
  )

  // 预检受众口径：导入取预检后有效数，粘贴取客户端去重后数量，与服务端批内去重对齐；
  // 黑名单与频控剔除只有服务端受理时可判定，预检不做虚假精确。
  const previewCount = computed(() => (form.source === "import" ? recipientCount.value : dedupedCount.value))

  const contentReady = computed(() =>
    form.contentMode === "content" ? form.content.trim().length > 0 : Number(form.templateId) > 0,
  )

  const testLimitExceeded = computed(
    () => form.isTest && testSendMax.value !== null && recipientCount.value > testSendMax.value,
  )

  const recipientLimitExceeded = computed(() => recipientCount.value > MAX_RECIPIENTS_PER_BATCH)

  // 定时必须落在未来时刻；过期时间提交只会被服务端拒绝，提前禁用并提示。
  const scheduleInPast = computed(
    () =>
      form.scheduleEnabled &&
      !!form.scheduledAt &&
      Number.isFinite(Date.parse(form.scheduledAt)) &&
      Date.parse(form.scheduledAt) <= Date.now(),
  )

  const sendDisabled = computed(
    () =>
      busy.value ||
      recipientCount.value === 0 ||
      (form.source === "paste" && invalidMobiles.value.length > 0) ||
      !contentReady.value ||
      (form.category === "market" && !form.consentConfirmed) ||
      testLimitExceeded.value ||
      recipientLimitExceeded.value ||
      scheduleInPast.value ||
      (form.scheduleEnabled && (!form.scheduledAt || !Number.isFinite(Date.parse(form.scheduledAt)))),
  )
  const scheduledAtValue = computed(() => (form.scheduleEnabled && form.scheduledAt ? form.scheduledAt : ""))

  const removedDuplicate = computed(() =>
    form.source === "import" ? (imported.value?.duplicate ?? 0) : duplicateCount.value,
  )
  const removedBlacklist = computed(() => (form.source === "import" ? (imported.value?.blacklisted ?? 0) : null))

  function contentPayload() {
    if (form.contentMode === "content") return { content: form.content }
    return {
      template_id: Number(form.templateId),
      template_params: templateParams.value.map((value) => value.trim()),
    }
  }

  const { previewLoading, previewError, previewStale, finalParts, riskLines, abortPreview, clearPreviewTimer } =
    useSendPreview({
      form,
      preview,
      contentPayload,
      previewCount,
      recipientCount,
      contentReady,
      importState,
      invalidMobiles,
      renderedTemplate,
      scheduledAtValue,
      isDisposed: () => disposed,
    })

  // 表单任何实质变更（含号码文本、模板参数按下标原地编辑、导入包切换）都作废上一受理结果并轮换幂等键；
  // 多源逐项浅比较，不再对 mobilesText 等大字段做全量 JSON 序列化。
  watch(
    [
      () => form.category,
      () => form.source,
      () => form.contentMode,
      () => form.mobilesText,
      () => form.content,
      () => form.templateId,
      () => form.signName,
      () => form.scheduleEnabled,
      () => form.scheduledAt,
      () => form.isTest,
      () => form.consentConfirmed,
      () => form.remark,
      () => imported.value?.import_id ?? null,
      // 模板参数经 v-model 按下标原地修改，需按值比较；数组极小（占位符个数），序列化开销可忽略。
      () => JSON.stringify(templateParams.value),
    ],
    () => {
      draftRevision += 1
      idempotencyKey.value = newIdempotencyKey()
      sendResult.value = null
      submittedSummary.value = ""
    },
    { flush: "sync" },
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

  /** 选择模板并按占位符个数清空参数。 */
  function selectTemplate(value: string | number): void {
    if (busy.value || disposed) return
    form.templateId = String(value)
    const template = templates.value.find((item) => item.id === Number(value))
    templateParams.value = template?.var_specs.map(() => "") || []
  }

  /** 下载导入剔除清单（只含掩码与原因）。 */
  async function downloadInvalidFile(): Promise<void> {
    if (!imported.value?.invalid_download_url) return
    try {
      const blob = await downloadImportInvalidFile(imported.value.invalid_download_url)
      saveBlob(blob, `sms-import-${imported.value.import_id}-invalid.csv`)
    } catch (error) {
      errorMessage.value = errorText(error, "剔除清单下载失败")
    }
  }

  function resetFeedback(): void {
    errorMessage.value = ""
    errorAdmission.value = false
  }

  /** 切换消息类别；切回通知时撤销营销同意勾选。 */
  function chooseCategory(category: Category): void {
    if (busy.value || disposed) return
    form.category = category
    if (category === "notice") form.consentConfirmed = false
    resetFeedback()
  }

  /** 粘贴号码去重并回写文本。 */
  function removeDuplicates(): void {
    if (busy.value || disposed) return
    flushMobilesParse()
    const deduped = [...new Set(pastedMobiles.value)]
    form.mobilesText = deduped.join("\n")
    // 与新文本保持同步（元素已去分隔符，与重解析结果一致），统计即时更新，不等防抖窗口
    pastedMobiles.value = deduped
  }

  /** 丢弃导入结果，回到待上传状态。 */
  function resetImport(): void {
    if (busy.value || disposed) return
    imported.value = null
    importState.value = "idle"
    importFilename.value = ""
    importError.value = ""
  }

  /** 上传号码文件并由服务端解析；代际守卫丢弃被取代的迟到结果。 */
  async function handleUpload(options: UploadRequestOptions): Promise<void> {
    if (busy.value || disposed) return
    const generation = ++requestGeneration
    uploadAbort?.abort()
    const controller = new AbortController()
    uploadAbort = controller
    resetFeedback()
    importState.value = "parsing"
    importFilename.value = options.file.name
    importError.value = ""
    busy.value = true
    try {
      const result = await uploadPhones(options.file, controller.signal)
      if (disposed || generation !== requestGeneration) return
      imported.value = result
      importState.value = "ready"
      form.source = "import"
      options.onSuccess(imported.value)
    } catch (error) {
      if (disposed || generation !== requestGeneration) return
      imported.value = null
      importState.value = "failed"
      importError.value = errorText(error, "号码文件解析失败")
    } finally {
      if (!disposed && generation === requestGeneration) busy.value = false
    }
  }

  /** 提交发送：组包携带当前幂等键，代际与草稿修订号守卫丢弃过期响应。 */
  async function submit(): Promise<void> {
    // 大文本粘贴后 300ms 防抖窗口内也可能点击提交：先落盘最新解析，再做禁用校验与组包。
    flushMobilesParse()
    if (sendDisabled.value || disposed) return
    const generation = ++requestGeneration
    const revision = draftRevision
    resetFeedback()
    busy.value = true
    const payload: WebMessagePayload = {
      category: form.category,
      biz_id: idempotencyKey.value,
      ...contentPayload(),
      sign_name: form.signName || undefined,
      scheduled_at: scheduledAtValue.value ? toApiDateTime(new Date(scheduledAtValue.value)) : undefined,
      is_test: form.isTest,
      consent_confirmed: form.consentConfirmed,
      remark: form.remark || undefined,
    }
    if (form.source === "import" && imported.value) payload.import_id = imported.value.import_id
    else payload.mobiles = [...pastedMobiles.value]
    try {
      const result = await sendWebMessage(payload)
      if (disposed || generation !== requestGeneration || revision !== draftRevision) return
      sendResult.value = result
      submittedSummary.value = `${payload.category === "market" ? "营销短信" : "通知短信"} · ${payload.import_id ? "文件导入" : "手工粘贴"} · ${payload.scheduled_at ? formatDateTime(payload.scheduled_at) : "立即受理"}`
    } catch (error) {
      if (disposed || generation !== requestGeneration || revision !== draftRevision) return
      // 准入 503 附 reason 时给出中文解释与运维中心出口；未知 reason 回退通用文案不吞诊断。
      if (error instanceof ApiRequestError && error.status === 503) {
        const reason = error.code === "DEPENDENCY_UNAVAILABLE" ? admissionReasonOf(error) : null
        const text = reason ? admissionReasonText(reason) : null
        errorAdmission.value = true
        errorMessage.value = text ? `发送通道暂时不可用：${text}` : errorText(error, "发送受理失败")
      } else {
        errorMessage.value = errorText(error, "发送受理失败")
      }
    } finally {
      if (!disposed && generation === requestGeneration) busy.value = false
    }
  }

  /** 清空草稿准备下一批，并轮换幂等键。 */
  function resetForAnother(): void {
    if (busy.value || disposed) return
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

  /** 会话清空或卸载时作废在途请求并清空全部易失草稿。 */
  function clearSessionDraft(): void {
    requestGeneration += 1
    busy.value = false
    resetForAnother()
    errorMessage.value = ""
    submittedSummary.value = ""
  }

  function isDisposed(): boolean {
    return disposed
  }

  function markDisposed(): void {
    disposed = true
  }

  function abortUpload(): void {
    uploadAbort?.abort()
  }

  return {
    form,
    imported,
    importState,
    importFilename,
    importError,
    preview,
    previewLoading,
    previewError,
    previewStale,
    busy,
    submittedSummary,
    errorMessage,
    errorAdmission,
    sendResult,
    idempotencyKey,
    templates,
    approvedTemplates,
    loadTemplates,
    templateParams,
    approvedSigns,
    loadSigns,
    testSendMax,
    selectedTemplate,
    renderedTemplateParts,
    pastedMobiles,
    invalidMobiles,
    dedupedCount,
    duplicateCount,
    recipientCount,
    previewCount,
    testLimitExceeded,
    recipientLimitExceeded,
    scheduleInPast,
    sendDisabled,
    removedDuplicate,
    removedBlacklist,
    finalParts,
    riskLines,
    selectTemplate,
    downloadInvalidFile,
    chooseCategory,
    removeDuplicates,
    resetImport,
    handleUpload,
    submit,
    resetForAnother,
    clearSessionDraft,
    isDisposed,
    markDisposed,
    abortUpload,
    abortPreview,
    clearPreviewTimer,
  }
}
