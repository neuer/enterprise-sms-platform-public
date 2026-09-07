// 单个 Document 的会话运行时：代际、内存凭据、定向信号与页内锁。
// 生产使用 defaultSessionDocument；双上下文测试创建独立实例，不共享 generation。

import type { PlatformUser } from "./auth"
import { type SessionMode } from "./sessionMode"
import {
  broadcastSessionRetired,
  createOpaqueHexId,
  createSessionInstanceId,
  isSessionInstanceId,
  publishSessionInstance,
  readPublishedSessionInstance,
  type SessionSignalPublisher,
  type SessionRetiredMessage,
  unpublishSessionInstance,
} from "./sessionSignals"

const REFRESH_LOCK_NAME = "sms-refresh-rotation"
const REFRESH_TAB_ID_KEY = "sms_refresh_tab_id"
const LEGACY_TOKEN_KEY = "sms_token"
const LEGACY_USER_KEY = "sms_user"
const SEEN_EVENT_LIMIT = 64

export interface SessionOperationOrigin {
  sessionInstanceId: string | null
  localGeneration: number
  operationId: string
}

export class SessionGenerationStaleError extends Error {
  constructor() {
    super("登录失败，请稍后重试")
    this.name = "SessionGenerationStaleError"
  }
}

export class SessionDocument {
  generation = 0
  accessToken: string | null = null
  sessionUser: PlatformUser | null = null
  sessionMode: SessionMode | null = null
  logicalSessionInstanceId: string | null = null
  refreshTabId: string | null = null
  legacyMigrationAttempted = false
  legacyMigrationClosed = false
  private readonly controllers = new Set<AbortController>()
  private readonly seenEventIds: string[] = []
  private readonly seenEventIndex = new Set<string>()
  private signalPublisher: SessionSignalPublisher | null = null
  private inPageBusy = false
  private readonly inPageWaiters: Array<() => void> = []

  constructor(options?: { publisher?: SessionSignalPublisher | null }) {
    if (options && "publisher" in options) this.signalPublisher = options.publisher ?? null
  }

  installPublisher(publisher: SessionSignalPublisher | null): void {
    this.signalPublisher = publisher
  }

  getSessionInstanceId(): string | null {
    return this.logicalSessionInstanceId
  }

  captureOrigin(): SessionOperationOrigin {
    return {
      sessionInstanceId: this.logicalSessionInstanceId,
      localGeneration: this.generation,
      operationId: createOpaqueHexId(),
    }
  }

  isOriginCurrent(origin: SessionOperationOrigin): boolean {
    return origin.sessionInstanceId === this.logicalSessionInstanceId && origin.localGeneration === this.generation
  }

  invalidateGeneration(): number {
    this.generation += 1
    for (const controller of this.controllers) {
      try {
        controller.abort(new DOMException("会话已切换", "AbortError"))
      } catch {
        // 已中止的控制器忽略。
      }
    }
    this.controllers.clear()
    return this.generation
  }

  trackController(controller: AbortController): () => void {
    if (controller.signal.aborted) return () => undefined
    this.controllers.add(controller)
    const release = (): void => {
      this.controllers.delete(controller)
    }
    controller.signal.addEventListener("abort", release, { once: true })
    return release
  }

  async withLocalMutex<T>(run: () => Promise<T>): Promise<T> {
    if (this.inPageBusy) {
      await new Promise<void>((resolve) => {
        this.inPageWaiters.push(resolve)
      })
    }
    this.inPageBusy = true
    try {
      return await run()
    } finally {
      const next = this.inPageWaiters.shift()
      if (next) next()
      else this.inPageBusy = false
    }
  }

  async withSessionLock<T>(run: () => Promise<T>): Promise<T> {
    const locks = globalThis.navigator?.locks
    if (locks && typeof locks.request === "function") {
      return locks.request(REFRESH_LOCK_NAME, run)
    }
    return this.withLocalMutex(run)
  }

  async withSessionGeneration<T>(
    options: { invalidateFirst?: boolean; origin?: SessionOperationOrigin },
    work: (ctx: { generation: number; signal: AbortSignal; isLive: () => boolean }) => Promise<T>,
  ): Promise<T> {
    return this.withSessionLock(async () => {
      if (options.origin && !this.isOriginCurrent(options.origin)) {
        throw new SessionGenerationStaleError()
      }
      if (options.invalidateFirst) {
        this.invalidateGeneration()
        if (options.origin) options.origin.localGeneration = this.generation
      }
      const current = this.generation
      const controller = new AbortController()
      const release = this.trackController(controller)
      try {
        if (current !== this.generation) throw new SessionGenerationStaleError()
        if (options.origin && options.origin.sessionInstanceId !== this.logicalSessionInstanceId) {
          throw new SessionGenerationStaleError()
        }
        return await work({
          generation: current,
          signal: controller.signal,
          isLive: () =>
            current === this.generation &&
            (!options.origin || options.origin.sessionInstanceId === this.logicalSessionInstanceId),
        })
      } finally {
        release()
      }
    })
  }

  setAccessSession(token: string, user: PlatformUser, mode: SessionMode = "refresh", sessionInstanceId?: string): void {
    this.legacyMigrationAttempted = true
    this.accessToken = token
    this.sessionUser = user
    this.sessionMode = mode === "access_only" ? "access_only" : "refresh"
    if (isSessionInstanceId(sessionInstanceId)) {
      this.logicalSessionInstanceId = sessionInstanceId
    } else if (!isSessionInstanceId(this.logicalSessionInstanceId)) {
      this.logicalSessionInstanceId = createSessionInstanceId()
    }
    publishSessionInstance(this.logicalSessionInstanceId)
  }

  clearAccessSession(): void {
    const retiredInstance = this.logicalSessionInstanceId
    this.accessToken = null
    this.sessionUser = null
    this.sessionMode = null
    this.logicalSessionInstanceId = null
    this.legacyMigrationClosed = true
    this.legacyMigrationAttempted = true
    this.storageRemove(LEGACY_TOKEN_KEY)
    this.storageRemove(LEGACY_USER_KEY)
    if (retiredInstance) unpublishSessionInstance(retiredInstance)
  }

  clearRefreshTabBinding(): void {
    this.refreshTabId = null
    try {
      sessionStorage.removeItem(REFRESH_TAB_ID_KEY)
    } catch {
      // 内存状态已经清除。
    }
  }

  getAccessToken(): string | null {
    this.bootstrapLegacyAccessSession()
    return this.accessToken
  }

  getSessionUser(): PlatformUser | null {
    this.bootstrapLegacyAccessSession()
    return this.sessionUser
  }

  bootstrapLegacyAccessSession(): void {
    if (this.legacyMigrationClosed || this.legacyMigrationAttempted) return
    this.legacyMigrationAttempted = true
    const legacyToken = this.storageGet(LEGACY_TOKEN_KEY)
    const rawUser = this.storageGet(LEGACY_USER_KEY)
    this.storageRemove(LEGACY_TOKEN_KEY)
    this.storageRemove(LEGACY_USER_KEY)
    if (this.legacyMigrationClosed) return
    if (!this.accessToken && legacyToken) this.accessToken = legacyToken
    if (!this.sessionUser && rawUser) {
      try {
        this.sessionUser = JSON.parse(rawUser) as PlatformUser
      } catch {
        this.sessionUser = null
      }
    }
    if (this.accessToken && !isSessionInstanceId(this.logicalSessionInstanceId)) {
      this.logicalSessionInstanceId = createSessionInstanceId()
    }
  }

  broadcastRetired(targetInstanceId: string): SessionRetiredMessage | null {
    if (this.signalPublisher) {
      const message = {
        version: 1 as const,
        type: "session-retired" as const,
        target_instance_id: targetInstanceId,
        event_id: createOpaqueHexId(),
      }
      if (!isSessionInstanceId(targetInstanceId)) return null
      this.signalPublisher(message)
      return message
    }
    return broadcastSessionRetired(targetInstanceId)
  }

  rememberEventId(eventId: string): boolean {
    if (!isSessionInstanceId(eventId)) return false
    if (this.seenEventIndex.has(eventId)) return false
    this.seenEventIndex.add(eventId)
    this.seenEventIds.push(eventId)
    if (this.seenEventIds.length > SEEN_EVENT_LIMIT) {
      const expired = this.seenEventIds.shift()
      if (expired) this.seenEventIndex.delete(expired)
    }
    return true
  }

  readPublishedInstance(): string | null {
    return readPublishedSessionInstance()
  }

  reset(): void {
    this.generation = 0
    this.controllers.clear()
    this.accessToken = null
    this.sessionUser = null
    this.sessionMode = null
    this.logicalSessionInstanceId = null
    this.refreshTabId = null
    this.legacyMigrationAttempted = false
    this.legacyMigrationClosed = false
    this.seenEventIds.length = 0
    this.seenEventIndex.clear()
    this.signalPublisher = null
    this.inPageBusy = false
    this.inPageWaiters.length = 0
  }

  private storageGet(key: string): string | null {
    try {
      return window.sessionStorage.getItem(key)
    } catch {
      return null
    }
  }

  private storageRemove(key: string): void {
    try {
      window.sessionStorage.removeItem(key)
    } catch {
      // 内存会话才是当前权威。
    }
  }
}

export const defaultSessionDocument = new SessionDocument()
