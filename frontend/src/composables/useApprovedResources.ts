import { computed, ref, shallowRef } from "vue"
import { useLatestRead } from "./useLatestRead"

/** 已审核资源选项：按页面独立加载，失败清空，旧请求和卸载后的结果不得重新填入。 */
export function useApprovedResources<T extends { vendor_state: string }>(
  fetcher: () => Promise<T[]>,
  onError?: (error: unknown) => void,
) {
  const items = shallowRef<T[]>([])
  const approved = computed(() => items.value.filter((item) => item.vendor_state === "approved"))
  const loading = ref(false)
  const unavailable = ref(false)
  const read = useLatestRead()
  async function load(): Promise<void> {
    const signal = read.start()
    loading.value = true
    try {
      const result = await fetcher()
      if (signal.aborted) return
      items.value = Array.isArray(result) ? result : []
      unavailable.value = false
    } catch (error) {
      if (signal.aborted) return
      items.value = []
      unavailable.value = true
      onError?.(error)
    } finally {
      if (!signal.aborted) loading.value = false
    }
  }
  return { items, approved, loading, unavailable, load }
}
