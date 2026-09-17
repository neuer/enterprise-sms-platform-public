// 跨标签页会话生命周期信号：定向、可去重、不含凭据。
// target_instance_id 只用于关联逻辑会话，不授予任何 API 权限。

export const SESSION_CLEAR_SIGNAL_KEY = "sms_session_clear"
export const SESSION_INSTANCE_META_KEY = "sms_session_instance"
export const SESSION_SIGNAL_VERSION = 1
export const SESSION_RETIRED_TYPE = "session-retired"
export const SESSION_INSTANCE_ID_PATTERN = /^[0-9a-f]{32}$/

export interface SessionRetiredMessage {
  version: 1
  type: "session-retired"
  target_instance_id: string
  event_id: string
}

export type SessionSignalPublisher = (message: SessionRetiredMessage) => void

/**
 * 事件去重器工厂：生产代理由 SessionDocument 每实例持有一份（双上下文测试互不干扰），
 * 模块不再保留任何全局去重表或注入总线，避免与实例实现漂移。
 */
export interface SessionEventIdDeduper {
  remember(eventId: string): boolean
  reset(): void
}

const SEEN_EVENT_LIMIT = 64

export function createSessionEventIdDeduper(): SessionEventIdDeduper {
  const seenEventIds: string[] = []
  const seenEventIndex = new Set<string>()
  return {
    remember(eventId: string): boolean {
      if (!isSessionInstanceId(eventId)) return false
      if (seenEventIndex.has(eventId)) return false
      seenEventIndex.add(eventId)
      seenEventIds.push(eventId)
      if (seenEventIds.length > SEEN_EVENT_LIMIT) {
        const expired = seenEventIds.shift()
        if (expired) seenEventIndex.delete(expired)
      }
      return true
    },
    reset(): void {
      seenEventIds.length = 0
      seenEventIndex.clear()
    },
  }
}

const CREDENTIAL_FIELD_NAMES = new Set([
  "token",
  "access_token",
  "refresh_token",
  "password",
  "cookie",
  "authorization",
  "jti",
  "jwt",
  "change_token",
])

export function createOpaqueHexId(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("")
}

export function isSessionInstanceId(value: unknown): value is string {
  return typeof value === "string" && SESSION_INSTANCE_ID_PATTERN.test(value)
}

export function createSessionInstanceId(): string {
  return createOpaqueHexId()
}

function readLocalStorage(): Storage | null {
  try {
    return window.localStorage
  } catch {
    return null
  }
}

export function publishSessionInstance(instanceId: string): void {
  if (!isSessionInstanceId(instanceId)) return
  const storage = readLocalStorage()
  if (!storage) return
  try {
    storage.setItem(SESSION_INSTANCE_META_KEY, instanceId)
  } catch {
    // 元数据写失败不得阻断内存会话；也不能降级成通配清理。
  }
}

export function readPublishedSessionInstance(): string | null {
  const storage = readLocalStorage()
  if (!storage) return null
  try {
    const value = storage.getItem(SESSION_INSTANCE_META_KEY)
    return isSessionInstanceId(value) ? value : null
  } catch {
    return null
  }
}

export function unpublishSessionInstance(expectedInstanceId: string): void {
  if (!isSessionInstanceId(expectedInstanceId)) return
  const storage = readLocalStorage()
  if (!storage) return
  try {
    if (storage.getItem(SESSION_INSTANCE_META_KEY) === expectedInstanceId) {
      storage.removeItem(SESSION_INSTANCE_META_KEY)
    }
  } catch {
    // 当前页内存清理仍是权威。
  }
}

function hasForbiddenCredentialFields(value: Record<string, unknown>): boolean {
  return Object.keys(value).some((key) => CREDENTIAL_FIELD_NAMES.has(key.toLowerCase()))
}

/** 解析定向退役消息；旧时间戳、损坏、未知版本一律忽略。 */
export function parseSessionRetiredMessage(raw: unknown): SessionRetiredMessage | null {
  if (raw == null) return null
  let value: unknown = raw
  if (typeof raw === "string") {
    if (raw === "" || /^\d+$/.test(raw)) return null
    try {
      value = JSON.parse(raw)
    } catch {
      return null
    }
  }
  if (!value || typeof value !== "object") return null
  const record = value as Record<string, unknown>
  if (hasForbiddenCredentialFields(record)) return null
  if (record.version !== SESSION_SIGNAL_VERSION) return null
  if (record.type !== SESSION_RETIRED_TYPE) return null
  if (!isSessionInstanceId(record.target_instance_id)) return null
  if (!isSessionInstanceId(record.event_id)) return null
  return {
    version: 1,
    type: "session-retired",
    target_instance_id: record.target_instance_id,
    event_id: record.event_id,
  }
}

export function encodeSessionRetiredMessage(message: SessionRetiredMessage): string {
  return JSON.stringify({
    version: 1,
    type: SESSION_RETIRED_TYPE,
    target_instance_id: message.target_instance_id,
    event_id: message.event_id,
  })
}

export function createSessionRetiredMessage(targetInstanceId: string): SessionRetiredMessage | null {
  if (!isSessionInstanceId(targetInstanceId)) return null
  return {
    version: 1,
    type: "session-retired",
    target_instance_id: targetInstanceId,
    event_id: createOpaqueHexId(),
  }
}

/**
 * 向兄弟标签页宣布「退役该逻辑实例」。Storage 失败时返回消息但不降级通配广播。
 * 测试可注入 publisher 走内存总线；生产默认经 localStorage 信号。
 */
export function broadcastSessionRetired(
  targetInstanceId: string,
  publisher?: SessionSignalPublisher | null,
): SessionRetiredMessage | null {
  const message = createSessionRetiredMessage(targetInstanceId)
  if (!message) return null
  if (publisher) {
    publisher(message)
    return message
  }
  const storage = readLocalStorage()
  if (!storage) return message
  try {
    storage.setItem(SESSION_CLEAR_SIGNAL_KEY, encodeSessionRetiredMessage(message))
    storage.removeItem(SESSION_CLEAR_SIGNAL_KEY)
  } catch {
    // 本地条件清理仍由调用方执行；禁止改发无目标时间戳。
  }
  return message
}

export function isStorageRemoveSignal(event: { newValue?: string | null }): boolean {
  return event.newValue == null
}
