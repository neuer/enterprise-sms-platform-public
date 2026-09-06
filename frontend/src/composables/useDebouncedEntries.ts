import { getCurrentScope, onScopeDispose, shallowRef, watch, type Ref, type ShallowRef } from "vue"

export interface UseDebouncedEntriesOptions {
  /** 文本拆分为条目的解析器；各页面保持与服务端一致的既有拆分口径。 */
  parse: (text: string) => string[]
  /** 不超过该字符数的文本每次变更同步解析，校验提示与计数即时反馈。默认 2000。 */
  syncMaxLength?: number
  /** 更大文本（可能数万行）的解析防抖毫秒数，避免逐键全量 split/Set 阻塞输入。默认 300。 */
  debounceMs?: number
}

export interface DebouncedEntries {
  /**
   * 最新一次落盘的解析结果；shallowRef 持有大数组不做深度响应，仅在解析落盘时整体替换。
   * 允许调用方整体重写（如去重后回写），但必须保持与文本内容同步。
   */
  entries: ShallowRef<string[]>
  /** 提交 / 剔除等即时路径先落盘待定解析，绝不使用防抖窗口内的过期结果。 */
  flush: () => void
}

/**
 * 批量录入文本的防抖解析（#596 模式单点）：小文本同步解析保持即时反馈，
 * 大文本粘贴按防抖窗口合并解析，避免每次按键在主线程全量拆分校验。
 * 解析只发生在内存中，组件作用域销毁时自动清理待定定时器。
 */
export function useDebouncedEntries(text: Ref<string>, options: UseDebouncedEntriesOptions): DebouncedEntries {
  const syncMaxLength = options.syncMaxLength ?? 2_000
  const debounceMs = options.debounceMs ?? 300

  const entries = shallowRef<string[]>(options.parse(text.value))
  let timer: number | undefined

  function parseNow(value: string): void {
    entries.value = options.parse(value)
  }

  // flush: "sync" 让 watcher 随赋值同步运行：flush() 被调用时待定定时器必然已建立，
  // 提交路径不会读到防抖窗口建立前的过期结果。
  watch(
    text,
    (value) => {
      window.clearTimeout(timer)
      if (value.length <= syncMaxLength) {
        timer = undefined
        parseNow(value)
        return
      }
      timer = window.setTimeout(() => {
        timer = undefined
        parseNow(value)
      }, debounceMs)
    },
    { flush: "sync" },
  )

  function flush(): void {
    if (timer === undefined) return
    window.clearTimeout(timer)
    timer = undefined
    parseNow(text.value)
  }

  if (getCurrentScope()) {
    onScopeDispose(() => window.clearTimeout(timer))
  }

  return { entries, flush }
}
