import { effectScope } from "vue"
import { vi } from "vitest"
import { renderPreview, splitPreviewParts, contentParts, vendorPreviewOf } from "../src/lib/templatePreview"
import { rangeToIsoParams } from "../src/lib/time"
import { phoneProblem } from "../src/lib/phone"
import { useApprovedResources } from "../src/composables/useApprovedResources"
import { usePagedList } from "../src/composables/usePagedList"

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: unknown) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

describe("第二轮共享边界", () => {
  it("预览保留空参数和非法占位，重复占位每次都替换且高亮一致", () => {
    const content = "你好{1}，{2}/{1}，{bad}。"
    const params = [" 张三 ", " "]
    expect(renderPreview(content, params)).toBe("你好张三，{2}/张三，{bad}。")
    const parts = splitPreviewParts(content, params)
    expect(parts.filter((part) => part.highlight).map((part) => part.text)).toEqual(["张三", "张三"])
    expect(parts.map((part) => part.text).join("")).toBe(renderPreview(content, params))
  })
  it("厂商预览依据每个占位的声明长度，缺少声明和非法标记保留原文", () => {
    const content = "{2}/{1}/{bad}/{3}"
    const specs = [
      { pos: 1, max_len: 6 },
      { pos: 2, max_len: 12 },
    ]
    expect(vendorPreviewOf(content, specs)).toBe("{s12}/{s6}/{bad}/{3}")
    expect(
      contentParts(content, specs)
        .map((part) => part.text)
        .join(""),
    ).toBe(content)
    expect(contentParts(content, specs).find((part) => part.pos === 2)?.maxLen).toBe(12)
  })
  it("时间查询保留原时刻，空范围不构造假的日期边界", () => {
    expect(rangeToIsoParams(null)).toEqual({ start: undefined, end: undefined })
    expect(rangeToIsoParams([new Date("2026-09-08T00:00:00+08:00"), new Date("2026-09-09T00:00:00+08:00")])).toEqual({
      start: "2026-09-07T16:00:00.000Z",
      end: "2026-09-08T16:00:00.000Z",
    })
    expect(phoneProblem("")).toBeUndefined()
    expect(phoneProblem("138****8000")).toBe("手机号须为 11 位以 1 开头的数字")
  })
  it("主动清空或卸载后，忽略不响应取消的接口返回及页面副作用", async () => {
    const first = deferred<{ items: { id: number }[]; total: number }>()
    const scope = effectScope()
    const onLoaded = vi.fn()
    const list = scope.run(() => usePagedList({ fetcher: () => first.promise, onLoaded, errorMessage: "读取失败" }))!
    const pending = list.load()
    list.cancel()
    first.resolve({ items: [{ id: 1 }], total: 1 })
    await pending
    expect(list.items.value).toEqual([])
    expect(onLoaded).not.toHaveBeenCalled()
    const again = list.load()
    scope.stop()
    await again
    expect(onLoaded).not.toHaveBeenCalled()
  })
  it("已审核选项读取失败即清空；卸载后的迟到读取不能回填已审核清单", async () => {
    const late = deferred<{ id: number; vendor_state: string }[]>()
    const fetcher = vi
      .fn()
      .mockResolvedValueOnce([
        { id: 1, vendor_state: "approved" },
        { id: 2, vendor_state: "pending" },
      ])
      .mockRejectedValueOnce(new Error("断网"))
      .mockImplementationOnce(() => late.promise)
    const scope = effectScope()
    const resources = scope.run(() => useApprovedResources<{ id: number; vendor_state: string }>(fetcher))!
    await resources.load()
    expect(resources.approved.value.map((item) => item.id)).toEqual([1])
    await resources.load()
    expect(resources.approved.value).toEqual([])
    expect(resources.unavailable.value).toBe(true)
    const loading = resources.load()
    scope.stop()
    late.resolve([{ id: 3, vendor_state: "approved" }])
    await loading
    expect(resources.approved.value).toEqual([])
  })
})
