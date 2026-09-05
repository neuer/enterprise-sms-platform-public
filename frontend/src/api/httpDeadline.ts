/**
 * 端到端请求截止线：同一 Timer/AbortSignal 覆盖响应头、有界正文读取、
 * UTF-8 解码与 JSON 解析。auth.ts 与 client.ts 共用，禁止再各写一套超时。
 */

export const AUTH_JSON_MAX_BYTES = 32 * 1024
export const API_JSON_MAX_BYTES = 1024 * 1024
export const DOWNLOAD_MAX_BYTES = 32 * 1024 * 1024

export type HttpBodyErrorCode = "RESPONSE_TOO_LARGE" | "INVALID_JSON_RESPONSE"

export class HttpBodyError extends Error {
  readonly code: HttpBodyErrorCode

  constructor(code: HttpBodyErrorCode, message: string) {
    super(message)
    this.name = "HttpBodyError"
    this.code = code
  }
}

export function createDeadline(
  timeoutMs: number,
  options: { callerSignal?: AbortSignal; timeoutMessage?: string } = {},
): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController()
  const timeoutMessage = options.timeoutMessage ?? "请求超时"
  const relayAbort = (): void => {
    controller.abort(options.callerSignal?.reason)
  }
  if (options.callerSignal?.aborted) {
    relayAbort()
    return {
      signal: controller.signal,
      cleanup: () => undefined,
    }
  }
  options.callerSignal?.addEventListener("abort", relayAbort, { once: true })
  const timer = window.setTimeout(() => controller.abort(new DOMException(timeoutMessage, "TimeoutError")), timeoutMs)
  return {
    signal: controller.signal,
    cleanup: () => {
      window.clearTimeout(timer)
      options.callerSignal?.removeEventListener("abort", relayAbort)
    },
  }
}

export function joinAbortSignals(signals: AbortSignal[]): { signal: AbortSignal; cleanup: () => void } {
  const controller = new AbortController()
  const relays: Array<{ signal: AbortSignal; listener: () => void }> = []
  for (const signal of signals) {
    if (signal.aborted) {
      controller.abort(signal.reason)
      break
    }
    const listener = (): void => {
      controller.abort(signal.reason)
    }
    signal.addEventListener("abort", listener, { once: true })
    relays.push({ signal, listener })
  }
  return {
    signal: controller.signal,
    cleanup: () => {
      for (const { signal, listener } of relays) {
        signal.removeEventListener("abort", listener)
      }
    },
  }
}

function abortReason(signal: AbortSignal): unknown {
  return signal.reason ?? new DOMException("请求已取消", "AbortError")
}

function raceAbort<T>(work: Promise<T>, signal: AbortSignal): Promise<T> {
  if (signal.aborted) return Promise.reject(abortReason(signal))
  return new Promise<T>((resolve, reject) => {
    const onAbort = (): void => {
      reject(abortReason(signal))
    }
    signal.addEventListener("abort", onAbort, { once: true })
    work.then(
      (value) => {
        signal.removeEventListener("abort", onAbort)
        resolve(value)
      },
      (error: unknown) => {
        signal.removeEventListener("abort", onAbort)
        reject(error)
      },
    )
  })
}

function concatBytes(chunks: readonly Uint8Array[]): Uint8Array {
  const total = chunks.reduce((sum, chunk) => sum + chunk.byteLength, 0)
  const out = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    out.set(chunk, offset)
    offset += chunk.byteLength
  }
  return out
}

export async function readLimitedBytes(
  response: Response,
  signal: AbortSignal,
  maxBytes: number,
): Promise<Uint8Array | null> {
  if (signal.aborted) throw abortReason(signal)
  if (response.status === 204) return null

  const declared = response.headers.get("content-length")
  if (declared !== null && declared !== "") {
    const length = Number(declared)
    if (Number.isFinite(length) && length >= 0) {
      if (length === 0) return null
      if (length > maxBytes) {
        throw new HttpBodyError("RESPONSE_TOO_LARGE", "响应正文超过允许大小")
      }
    }
  }

  if (!response.body) {
    return readLegacyBytes(response, signal, maxBytes)
  }

  const reader = response.body.getReader()
  const onAbort = (): void => {
    void reader.cancel(signal.reason).catch(() => undefined)
  }
  signal.addEventListener("abort", onAbort, { once: true })
  const chunks: Uint8Array[] = []
  let total = 0
  try {
    while (true) {
      const { done, value } = await raceAbort(reader.read(), signal)
      if (done) break
      if (!value?.byteLength) continue
      total += value.byteLength
      if (total > maxBytes) {
        await reader.cancel().catch(() => undefined)
        throw new HttpBodyError("RESPONSE_TOO_LARGE", "响应正文超过允许大小")
      }
      chunks.push(value)
    }
  } finally {
    signal.removeEventListener("abort", onAbort)
    try {
      reader.releaseLock()
    } catch {
      // cancel() 后锁可能已释放
    }
  }
  return total === 0 ? null : concatBytes(chunks)
}

export async function readJsonBody<T>(response: Response, signal: AbortSignal, maxBytes: number): Promise<T | null> {
  const bytes = await readLimitedBytes(response, signal, maxBytes)
  if (bytes == null) return null
  let text: string
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes)
  } catch {
    throw new HttpBodyError("INVALID_JSON_RESPONSE", "响应不是有效 JSON")
  }
  if (text.length === 0) return null
  try {
    return JSON.parse(text) as T
  } catch {
    throw new HttpBodyError("INVALID_JSON_RESPONSE", "响应不是有效 JSON")
  }
}

type LegacyReadable = {
  text?: () => Promise<string>
  json?: () => Promise<unknown>
  blob?: () => Promise<Blob>
}

async function readLegacyBytes(response: Response, signal: AbortSignal, maxBytes: number): Promise<Uint8Array | null> {
  const legacy = response as Response & LegacyReadable
  if (typeof legacy.text === "function") {
    const text = await raceAbort(legacy.text.call(response), signal)
    const bytes = new TextEncoder().encode(text)
    if (bytes.byteLength > maxBytes) {
      throw new HttpBodyError("RESPONSE_TOO_LARGE", "响应正文超过允许大小")
    }
    return bytes.byteLength === 0 ? null : bytes
  }
  if (typeof legacy.json === "function") {
    const parsed = await raceAbort(legacy.json.call(response), signal)
    if (parsed === undefined) return null
    const bytes = new TextEncoder().encode(JSON.stringify(parsed))
    if (bytes.byteLength > maxBytes) {
      throw new HttpBodyError("RESPONSE_TOO_LARGE", "响应正文超过允许大小")
    }
    return bytes
  }
  throw new HttpBodyError("INVALID_JSON_RESPONSE", "响应不是有效 JSON")
}

export async function readLimitedBlob(response: Response, signal: AbortSignal, maxBytes: number): Promise<Blob> {
  if (!response.body) {
    const legacy = response as Response & LegacyReadable
    if (typeof legacy.blob === "function") {
      const blob = await raceAbort(legacy.blob.call(response), signal)
      if (blob.size > maxBytes) {
        throw new HttpBodyError("RESPONSE_TOO_LARGE", "响应正文超过允许大小")
      }
      return blob
    }
  }
  const bytes = await readLimitedBytes(response, signal, maxBytes)
  const type = response.headers.get("content-type") || ""
  if (bytes == null) return new Blob([], type ? { type } : {})
  const copy = new Uint8Array(bytes.byteLength)
  copy.set(bytes)
  return new Blob([copy], type ? { type } : {})
}

export async function fetchJsonWithDeadline<T>(
  input: RequestInfo | URL,
  init: RequestInit,
  options: {
    timeoutMs: number
    callerSignal?: AbortSignal
    maxBodyBytes: number
    timeoutMessage?: string
    credentials?: RequestCredentials
  },
): Promise<{ response: Response; body: T | null }> {
  if (options.callerSignal?.aborted) {
    throw options.callerSignal.reason ?? new DOMException("请求已取消", "AbortError")
  }
  const deadline = createDeadline(options.timeoutMs, {
    callerSignal: options.callerSignal,
    timeoutMessage: options.timeoutMessage,
  })
  try {
    const response = await fetch(input, {
      ...init,
      credentials: options.credentials ?? init.credentials,
      signal: deadline.signal,
    })
    const body = await readJsonBody<T>(response, deadline.signal, options.maxBodyBytes)
    return { response, body }
  } finally {
    deadline.cleanup()
  }
}
