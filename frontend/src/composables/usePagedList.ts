import { ElMessage } from "element-plus"
import { ref, type Ref } from "vue"

import { useLatestRead } from "./useLatestRead"

import { errorText } from "../lib/error"

/** 分页接口的最小返回形状；部分端点回带服务端生效页码 page（可能被钳位），更多附加字段经泛型 R 携带。 */
export interface PagedListResult<T> {
  items: T[]
  total: number
  page?: number
}

export interface UsePagedListOptions<T, R extends PagedListResult<T>> {
  page?: Ref<number>
  /** 拉取一页数据；返回 null 表示静默丢弃（视图自行判定上下文已失效，如详情抽屉已切换到其他批次）。 */
  fetcher: (page: number, signal: AbortSignal) => Promise<R | null>
  /** errorText 的兜底文案（如「黑名单加载失败」）。 */
  errorMessage: string
  /** 新鲜结果写入 items/total/page 后的页面副作用（同步计数、徽标、抽屉选中行等）；陈旧响应不触发。 */
  onLoaded?: (result: R) => void
  /** 新鲜失败写入错误后的追加副作用（清空派生态等）；陈旧失败不触发。 */
  onError?: (error: unknown) => void
  /** 覆盖错误文案提取（如安全日报带错误码的 apiErrorMessage）。 */
  formatError?: (error: unknown) => string
  /** 错误改走 ElMessage 浮层而非 errorMessage 内联（详情抽屉遮挡页面 alert 的场景）。 */
  toastError?: boolean
  /** 发起加载前清空 items/total（安全日报列表的既有语义）。 */
  clearOnLoad?: boolean
  /** 加载失败时清空 items/total（号码搜索、安全日报的既有语义）；默认保留上一次成功结果。 */
  clearOnError?: boolean
  /** 重置筛选回调：reset() 先调用它再回第一页重查。 */
  resetFilters?: () => void
}

export interface PagedListController<T> {
  items: Ref<T[]>
  total: Ref<number>
  page: Ref<number>
  loading: Ref<boolean>
  errorMessage: Ref<string>
  /** 拉取当前页；silent 用于轮询等后台刷新（不动 loading）。竞态守卫：只接受最后一次调用的结果。 */
  load: (options?: { silent?: boolean }) => Promise<void>
  /** 回第一页并重查。 */
  search: () => void
  /** 先执行 resetFilters 清空筛选，再回第一页重查。 */
  reset: () => void
  cancel: () => void
}

/**
 * 列表页加载脚手架单点：竞态守卫（最后一次调用胜出）、loading / errorMessage 状态、
 * 页码回写与 search / reset 语义。差异一律经 options 承载，禁止在视图内重新手抄 token 模式。
 * 行类型与附加字段（dead_total、status_counts 等）从 fetcher 返回值整体推断，调用方不写类型实参。
 */
export function usePagedList<R extends PagedListResult<unknown>>(
  options: UsePagedListOptions<R["items"][number], R>,
): PagedListController<R["items"][number]> {
  const items = ref<R["items"]>([]) as Ref<R["items"]>
  const total = ref(0)
  const page = options.page ?? ref(1)
  const loading = ref(false)
  const errorMessage = ref("")
  const read = useLatestRead()
  let loadToken = 0

  function cancel(): void {
    ++loadToken
    read.cancel()
    loading.value = false
  }

  async function load(loadOptions: { silent?: boolean } = {}): Promise<void> {
    const token = ++loadToken
    const signal = read.start()
    if (!loadOptions.silent) loading.value = true
    errorMessage.value = ""
    if (options.clearOnLoad) {
      items.value = []
      total.value = 0
    }
    try {
      const result = await options.fetcher(page.value, signal)
      if (signal.aborted || token !== loadToken || result === null) return
      items.value = result.items
      total.value = result.total
      if (typeof result.page === "number") page.value = result.page
      options.onLoaded?.(result)
    } catch (error) {
      if (signal.aborted || token !== loadToken) return
      if (options.clearOnError) {
        items.value = []
        total.value = 0
      }
      const message = options.formatError ? options.formatError(error) : errorText(error, options.errorMessage)
      if (options.toastError) ElMessage.error(message)
      else errorMessage.value = message
      options.onError?.(error)
    } finally {
      if (token === loadToken) loading.value = false
    }
  }

  function search(): void {
    page.value = 1
    void load()
  }

  function reset(): void {
    options.resetFilters?.()
    page.value = 1
    void load()
  }

  return { items, total, page, loading, errorMessage, load, search, reset, cancel }
}
