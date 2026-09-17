import { ElMessage } from "element-plus"
import { vi } from "vitest"

import { usePagedList } from "../src/composables/usePagedList"

interface Row {
  id: number
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

describe("usePagedList", () => {
  it("加载成功写入 items/total 并回写服务端生效页码", async () => {
    const list = usePagedList({
      fetcher: async (page) => ({ items: [{ id: page }], total: 42, page }),
      errorMessage: "列表加载失败",
    })
    list.page.value = 3
    await list.load()
    expect(list.items.value).toEqual([{ id: 3 }])
    expect(list.total.value).toBe(42)
    expect(list.page.value).toBe(3)
    expect(list.loading.value).toBe(false)
    expect(list.errorMessage.value).toBe("")
  })

  it("竞态守卫：先发后至的响应被丢弃，loading 只随最后一次调用收敛", async () => {
    const first = deferred<{ items: Row[]; total: number }>()
    const second = deferred<{ items: Row[]; total: number }>()
    const fetcher = vi
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
    const list = usePagedList({ fetcher, errorMessage: "列表加载失败" })

    const loadFirst = list.load()
    const loadSecond = list.load()
    expect(list.loading.value).toBe(true)
    // 后发的请求先完成
    second.resolve({ items: [{ id: 2 }], total: 2 })
    await loadSecond
    expect(list.items.value).toEqual([{ id: 2 }])
    expect(list.loading.value).toBe(false)
    // 先发请求后至：结果被丢弃，不再触碰 loading
    first.resolve({ items: [{ id: 1 }], total: 1 })
    await loadFirst
    expect(list.items.value).toEqual([{ id: 2 }])
    expect(list.loading.value).toBe(false)
  })

  it("竞态守卫：陈旧请求的失败被丢弃，不覆盖新结果与错误态", async () => {
    const first = deferred<{ items: Row[]; total: number }>()
    const second = deferred<{ items: Row[]; total: number }>()
    const list = usePagedList({
      fetcher: vi
        .fn()
        .mockImplementationOnce(() => first.promise)
        .mockImplementationOnce(() => second.promise),
      errorMessage: "列表加载失败",
    })
    const loadFirst = list.load()
    const loadSecond = list.load()
    second.resolve({ items: [{ id: 2 }], total: 2 })
    await loadSecond
    first.reject(new Error("网络错误"))
    await loadFirst
    expect(list.errorMessage.value).toBe("")
    expect(list.items.value).toEqual([{ id: 2 }])
  })

  it("错误兜底：Error 取 message，非 Error 用兜底文案；onError 追加副作用", async () => {
    const onError = vi.fn()
    const list = usePagedList({
      fetcher: async () => {
        throw new Error("服务繁忙")
      },
      errorMessage: "列表加载失败",
      onError,
    })
    await list.load()
    expect(list.errorMessage.value).toBe("服务繁忙")
    expect(onError).toHaveBeenCalledOnce()

    const nonError = usePagedList({
      fetcher: async () => {
        throw "boom"
      },
      errorMessage: "列表加载失败",
    })
    await nonError.load()
    expect(nonError.errorMessage.value).toBe("列表加载失败")
    expect(nonError.loading.value).toBe(false)
  })

  it("search 回第一页并重查；reset 先清筛选再回第一页重查", async () => {
    const fetcher = vi.fn(async (page: number) => ({ items: [{ id: page }], total: 9 }))
    const resetFilters = vi.fn()
    const list = usePagedList({ fetcher, errorMessage: "列表加载失败", resetFilters })
    list.page.value = 5
    list.search()
    expect(list.page.value).toBe(1)
    await flush()
    expect(fetcher).toHaveBeenLastCalledWith(1, expect.any(AbortSignal))

    list.page.value = 4
    list.reset()
    expect(resetFilters).toHaveBeenCalledOnce()
    expect(list.page.value).toBe(1)
    await flush()
    expect(fetcher).toHaveBeenLastCalledWith(1, expect.any(AbortSignal))
  })

  it("silent 刷新不触碰 loading", async () => {
    const gate = deferred<{ items: Row[]; total: number }>()
    const list = usePagedList({ fetcher: () => gate.promise, errorMessage: "列表加载失败" })
    const pending = list.load({ silent: true })
    expect(list.loading.value).toBe(false)
    gate.resolve({ items: [], total: 0 })
    await pending
    expect(list.loading.value).toBe(false)
  })

  it("fetcher 返回 null 视为静默丢弃：items 保持、loading 收敛", async () => {
    let respond: { items: Row[]; total: number } | null = { items: [{ id: 1 }], total: 1 }
    const list = usePagedList({ fetcher: async () => respond, errorMessage: "列表加载失败" })
    await list.load()
    expect(list.items.value).toEqual([{ id: 1 }])
    respond = null
    await list.load()
    expect(list.items.value).toEqual([{ id: 1 }])
    expect(list.loading.value).toBe(false)
  })

  it("clearOnLoad / clearOnError 按选项清空条目", async () => {
    let fail = false
    const list = usePagedList({
      fetcher: async () => {
        if (fail) throw new Error("x")
        return { items: [{ id: 1 }], total: 1 }
      },
      errorMessage: "列表加载失败",
      clearOnLoad: true,
      clearOnError: true,
    })
    await list.load()
    expect(list.items.value).toHaveLength(1)
    fail = true
    await list.load()
    expect(list.items.value).toEqual([])
    expect(list.total.value).toBe(0)
  })

  it("toastError 模式错误走 ElMessage 浮层而非 errorMessage", async () => {
    const spy = vi.spyOn(ElMessage, "error").mockImplementation(() => ({ close: () => undefined }) as never)
    const list = usePagedList({
      fetcher: async () => {
        throw new Error("明细失败")
      },
      errorMessage: "列表加载失败",
      toastError: true,
    })
    await list.load()
    expect(spy).toHaveBeenCalledWith("明细失败")
    expect(list.errorMessage.value).toBe("")
    spy.mockRestore()
  })

  it("formatError 覆盖错误文案提取；onLoaded 只对新鲜结果触发", async () => {
    const gate = deferred<{ items: Row[]; total: number }>()
    const onLoaded = vi.fn()
    const list = usePagedList({
      fetcher: vi
        .fn()
        .mockImplementationOnce(() => gate.promise)
        .mockImplementationOnce(async () => {
          throw new Error("原始")
        }),
      errorMessage: "列表加载失败",
      formatError: () => "自定义文案",
      onLoaded,
    })
    const slow = list.load()
    await list.load()
    gate.resolve({ items: [{ id: 9 }], total: 9 })
    await slow
    // 陈旧成功结果不触发 onLoaded
    expect(onLoaded).not.toHaveBeenCalled()
    expect(list.errorMessage.value).toBe("自定义文案")
  })
})

async function flush(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0))
}
