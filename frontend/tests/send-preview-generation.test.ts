import { effectScope, nextTick, reactive, ref } from "vue"

import type { BillingPreview } from "../src/api/webMessages"
import { vi } from "vitest"

const previewBilling = vi.hoisted(() => vi.fn())
vi.mock("../src/api/webMessages", () => ({ previewBilling }))

import { useSendPreview } from "../src/views/send/useSendPreview"

function harness(content: string) {
  const form = reactive({
    category: "notice" as const,
    source: "paste" as const,
    contentMode: "content" as const,
    content,
    signName: "",
    isTest: false,
    scheduledAt: "",
    consentConfirmed: false,
  })
  const preview = ref<BillingPreview | null>(null)
  const scope = effectScope()
  const api = scope.run(() =>
    useSendPreview({
      form,
      preview,
      contentPayload: () => ({ content: form.content }),
      previewCount: ref(1) as never,
      recipientCount: ref(1) as never,
      contentReady: ref(true) as never,
      importState: ref("ready") as never,
      invalidMobiles: ref([]) as never,
      renderedTemplate: ref("") as never,
      scheduledAtValue: ref("") as never,
      isDisposed: () => false,
    }),
  )!
  return { form, preview, api, scope }
}

describe("预检请求代际", () => {
  it("A→B→A 时旧取消不覆盖新的相同内容", async () => {
    vi.useFakeTimers()
    let rejectFirst: (error: unknown) => void = () => undefined
    previewBilling
      .mockImplementationOnce(
        () =>
          new Promise((_resolve, reject) => {
            rejectFirst = reject
          }),
      )
      .mockResolvedValueOnce({
        final_length: 1,
        segment_parts: ["A"],
        final_content: "A2",
        unsubscribe_appended: false,
        approval_required: false,
        deferred_reason: null,
      })
    const { form, preview, api, scope } = harness("")
    form.content = "A"
    await nextTick()
    await vi.advanceTimersByTimeAsync(600)
    form.content = "B"
    await nextTick()
    form.content = "A"
    await nextTick()
    await vi.advanceTimersByTimeAsync(600)
    rejectFirst(new DOMException("aborted", "AbortError"))
    await Promise.resolve()
    expect(api.previewError.value).toBe("")
    expect(preview.value?.final_content).toBe("A2")
    scope.stop()
    vi.useRealTimers()
  })
})
