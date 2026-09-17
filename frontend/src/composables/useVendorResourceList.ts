import { computed, ref, type Ref } from "vue"
import { VENDOR_REVIEW_LABELS } from "../lib/labels"
import { usePagedList } from "./usePagedList"

/** 模板与签名台账共用加载、状态计数及关键词筛选；编辑与授权仍由各页面负责。 */
export function useVendorResourceList<T extends { vendor_state: string }>(options: {
  fetcher: () => Promise<T[]>
  states: readonly T["vendor_state"][]
  searchText: (item: T) => string
  errorMessage: string
}) {
  const stateFilter = ref("all") as Ref<T["vendor_state"] | "all">
  const keyword = ref("")
  const { items, loading, errorMessage, load } = usePagedList({
    fetcher: async () => {
      const items = await options.fetcher()
      return { items, total: items.length }
    },
    errorMessage: options.errorMessage,
  })
  const stateOptions = computed(() => [
    { value: "all" as const, label: "全部", count: items.value.length },
    ...options.states.map((value) => ({
      value,
      label: VENDOR_REVIEW_LABELS[value],
      count: items.value.filter((item) => item.vendor_state === value).length,
    })),
  ])
  const filtered = computed(() => {
    const kw = keyword.value.trim().toLowerCase()
    return items.value.filter(
      (item) =>
        (stateFilter.value === "all" || item.vendor_state === stateFilter.value) &&
        (!kw || options.searchText(item).toLowerCase().includes(kw)),
    )
  })
  return { items, loading, errorMessage, load, stateFilter, keyword, stateOptions, filtered }
}
