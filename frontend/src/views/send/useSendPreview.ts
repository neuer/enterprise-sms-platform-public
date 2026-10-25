import { computed, ref, watch, type ComputedRef, type Ref } from "vue"

import { previewBilling, type BillingPreview, type Category } from "../../api/webMessages"
import type { FinalContentParts, RiskLine } from "../../components/SendPrecheckCards.vue"
import { errorText } from "../../lib/error"

/** 预检依赖的发送表单字段；与页面 form 同一 reactive 对象，只读。 */
export interface SendPreviewForm {
  category: Category
  source: "paste" | "import"
  contentMode: "content" | "template"
  content: string
  signName: string
  isTest: boolean
  scheduledAt: string
  consentConfirmed: boolean
}

export interface SendPreviewOptions {
  form: SendPreviewForm
  /** 跨组件共享的预检结果；重置草稿时由调用方直接置空。 */
  preview: Ref<BillingPreview | null>
  contentPayload: () => { content: string } | { template_id: number; template_params: string[] }
  previewCount: ComputedRef<number>
  recipientCount: ComputedRef<number>
  contentReady: ComputedRef<boolean>
  importState: Ref<"idle" | "parsing" | "ready" | "failed">
  invalidMobiles: ComputedRef<string[]>
  renderedTemplate: ComputedRef<string>
  scheduledAtValue: ComputedRef<string>
  isDisposed: () => boolean
}

/**
 * 人工发送自动预检：表单关键字段 600ms 防抖触发，竞态以指纹裁决，过期显式标记；
 * 同时派生最终内容构成与风险合规行。每次调用新建状态，不在模块级持有任何草稿。
 */
export function useSendPreview(options: SendPreviewOptions) {
  const { form, preview, contentPayload, previewCount, recipientCount, contentReady } = options
  const { importState, invalidMobiles, renderedTemplate, scheduledAtValue, isDisposed } = options
  const previewLoading = ref(false)
  const previewError = ref("")

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
  // 预检请求真实取消：代际区分内容相同的两次请求，避免 A→B→A 把旧取消当成当前结果。
  let previewAbort: AbortController | undefined
  let previewGeneration = 0

  const previewReady = computed(
    () =>
      contentReady.value &&
      recipientCount.value > 0 &&
      (form.source === "paste" || importState.value === "ready") &&
      (form.category !== "market" || form.consentConfirmed) &&
      !(form.source === "paste" && invalidMobiles.value.length > 0),
  )

  const previewStale = computed(() => preview.value !== null && lastPreviewKey.value !== previewKey.value)

  function isValidPreview(value: BillingPreview | null): value is BillingPreview {
    return (
      !!value &&
      typeof value.final_length === "number" &&
      Array.isArray(value.segment_parts) &&
      typeof value.final_content === "string"
    )
  }

  function isCurrentPreview(generation: number, controller: AbortController, key: string): boolean {
    return (
      !isDisposed() &&
      generation === previewGeneration &&
      previewAbort === controller &&
      !controller.signal.aborted &&
      key === previewKey.value
    )
  }

  function retirePreview(): void {
    previewGeneration += 1
    previewAbort?.abort()
    previewAbort = undefined
  }

  async function runPreview(key: string): Promise<void> {
    const generation = ++previewGeneration
    previewAbort?.abort()
    const controller = new AbortController()
    previewAbort = controller
    previewLoading.value = true
    previewError.value = ""
    try {
      const result = await previewBilling(
        {
          category: form.category,
          ...contentPayload(),
          sign_name: form.signName || undefined,
          accepted_count: previewCount.value,
          consent_confirmed: form.consentConfirmed,
        },
        controller.signal,
      )
      if (!isCurrentPreview(generation, controller, key)) return
      previewError.value = ""
      if (isValidPreview(result)) {
        preview.value = result
        lastPreviewKey.value = key
      } else {
        preview.value = null
        lastPreviewKey.value = ""
      }
    } catch (error) {
      if (!isCurrentPreview(generation, controller, key)) return
      if (error instanceof DOMException && error.name === "AbortError") return
      previewError.value = errorText(error, "预检失败")
    } finally {
      if (isCurrentPreview(generation, controller, key)) previewLoading.value = false
    }
  }

  watch(previewKey, () => {
    window.clearTimeout(previewTimer)
    if (!previewReady.value) {
      retirePreview()
      preview.value = null
      previewError.value = ""
      previewLoading.value = false
      lastPreviewKey.value = ""
      return
    }
    previewTimer = window.setTimeout(() => void runPreview(previewKey.value), 600)
  })

  // 最终内容按构成拆分：签名 + 正文 + 服务端追加的退订语；对不上时整体平铺，不伪造高亮。
  const finalParts = computed<FinalContentParts | null>(() => {
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

  // ── 风险与合规行 ──
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

  /** 卸载时取消在途预检请求，并作废其代际。 */
  function abortPreview(): void {
    retirePreview()
  }

  /** 卸载时清除待触发的防抖定时器。 */
  function clearPreviewTimer(): void {
    window.clearTimeout(previewTimer)
  }

  return { previewLoading, previewError, previewStale, finalParts, riskLines, abortPreview, clearPreviewTimer }
}
