// 本页会话代际：login/refresh/logout/restore/BFCache/跨标签页清理共用。
// 旧代响应永久失效，禁止写回 Access Token。跨标签页信号不得携带凭据。

import { withSessionLock } from "./refreshLock"

let generation = 0
const sessionControllers = new Set<AbortController>()

export class SessionGenerationStaleError extends Error {
  constructor() {
    super("登录失败，请稍后重试")
    this.name = "SessionGenerationStaleError"
  }
}

export function getSessionGeneration(): number {
  return generation
}

export function isCurrentSessionGeneration(epoch: number): boolean {
  return epoch === generation
}

/** 推进代际并取消全部在途会话请求；旧代响应此后不得写回。 */
export function invalidateSessionGeneration(): number {
  generation += 1
  for (const controller of sessionControllers) {
    try {
      controller.abort(new DOMException("会话已切换", "AbortError"))
    } catch {
      // 已中止的控制器忽略。
    }
  }
  sessionControllers.clear()
  return generation
}

export function trackSessionController(controller: AbortController): () => void {
  if (controller.signal.aborted) return () => undefined
  sessionControllers.add(controller)
  const release = (): void => {
    sessionControllers.delete(controller)
  }
  controller.signal.addEventListener("abort", release, { once: true })
  return release
}

export async function withSessionGeneration<T>(
  options: { invalidateFirst?: boolean },
  work: (ctx: { generation: number; signal: AbortSignal; isLive: () => boolean }) => Promise<T>,
): Promise<T> {
  return withSessionLock(async () => {
    if (options.invalidateFirst) invalidateSessionGeneration()
    const current = getSessionGeneration()
    const controller = new AbortController()
    const release = trackSessionController(controller)
    try {
      if (!isCurrentSessionGeneration(current)) {
        throw new SessionGenerationStaleError()
      }
      return await work({
        generation: current,
        signal: controller.signal,
        isLive: () => isCurrentSessionGeneration(current),
      })
    } finally {
      release()
    }
  })
}
