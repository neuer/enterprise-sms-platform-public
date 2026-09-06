// 本页会话代际：login/refresh/logout/restore/BFCache/跨标签页清理共用。
// 旧代响应永久失效，禁止写回 Access Token。跨标签页信号不得携带凭据。

import { defaultSessionDocument, SessionGenerationStaleError, type SessionOperationOrigin } from "./sessionDocument"

export { SessionGenerationStaleError, type SessionOperationOrigin }

export function getSessionGeneration(): number {
  return defaultSessionDocument.generation
}

export function isCurrentSessionGeneration(epoch: number): boolean {
  return epoch === defaultSessionDocument.generation
}

/** 在第一个异步等待前捕获；跨页不得比较各页本地 generation。 */
export function captureSessionOperationOrigin(): SessionOperationOrigin {
  return defaultSessionDocument.captureOrigin()
}

export function isSessionOperationOriginCurrent(origin: SessionOperationOrigin): boolean {
  return defaultSessionDocument.isOriginCurrent(origin)
}

/** 推进代际并取消全部在途会话请求；旧代响应此后不得写回。 */
export function invalidateSessionGeneration(): number {
  return defaultSessionDocument.invalidateGeneration()
}

export function trackSessionController(controller: AbortController): () => void {
  return defaultSessionDocument.trackController(controller)
}

export async function withSessionGeneration<T>(
  options: { invalidateFirst?: boolean; origin?: SessionOperationOrigin },
  work: (ctx: { generation: number; signal: AbortSignal; isLive: () => boolean }) => Promise<T>,
): Promise<T> {
  return defaultSessionDocument.withSessionGeneration(options, work)
}
