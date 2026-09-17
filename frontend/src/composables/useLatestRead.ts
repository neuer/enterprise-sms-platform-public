import { getCurrentScope, onScopeDispose } from "vue"

/** 同一读取通道只保留最新请求；切换查询与作用域销毁时取消，写操作不得使用此通道。 */
export function useLatestRead(): { start: () => AbortSignal; cancel: () => void } {
  let controller: AbortController | undefined

  function cancel(): void {
    controller?.abort()
    controller = undefined
  }

  function start(): AbortSignal {
    cancel()
    controller = new AbortController()
    return controller.signal
  }

  if (getCurrentScope()) onScopeDispose(cancel)
  return { start, cancel }
}
