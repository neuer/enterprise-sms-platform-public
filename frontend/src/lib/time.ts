/**
 * 时间格式化单点实现。前端 UI 约定：本地 +08:00，`YYYY-MM-DD HH:mm:ss`。
 * 统一 Asia/Shanghai 时区与 formatToParts 拼装，禁止各页面自行实现 Intl 格式化。
 */

const DATE_TIME = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
})

const DATE_TIME_MINUTE = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
})

const HM = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
})

const HMS = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
})

const DATE_KEY = new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai" })

type Parts = Record<string, string>

function partsOf(formatter: Intl.DateTimeFormat, date: Date): Parts {
  const parts: Parts = {}
  for (const part of formatter.formatToParts(date)) {
    if (part.type !== "literal") parts[part.type] = part.value
  }
  return parts
}

function parse(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null
  const date = value instanceof Date ? value : new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

/** `YYYY-MM-DD HH:mm:ss`（Asia/Shanghai）；空值/非法值返回 empty。 */
export function formatDateTime(value: string | number | Date | null | undefined, empty = "—"): string {
  const date = parse(value)
  if (!date) return empty
  const p = partsOf(DATE_TIME, date)
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`
}

/** `MM-DD HH:mm`（Asia/Shanghai）；空值/非法值返回 empty。 */
export function formatDateTimeMinute(value: string | number | Date | null | undefined, empty = "—"): string {
  const date = parse(value)
  if (!date) return empty
  const p = partsOf(DATE_TIME_MINUTE, date)
  return `${p.month}-${p.day} ${p.hour}:${p.minute}`
}

/** `HH:mm`（Asia/Shanghai）；空值/非法值返回 empty。 */
export function formatHm(value: string | number | Date | null | undefined, empty = "—"): string {
  const date = parse(value)
  if (!date) return empty
  const p = partsOf(HM, date)
  return `${p.hour}:${p.minute}`
}

/** `HH:mm:ss`（Asia/Shanghai）；空值/非法值返回 empty。 */
export function formatHms(value: string | number | Date | null | undefined, empty = "—"): string {
  const date = parse(value)
  if (!date) return empty
  const p = partsOf(HMS, date)
  return `${p.hour}:${p.minute}:${p.second}`
}

/** 剩余毫秒数格式化为倒计时 `HH:MM:SS`（小时可超 24，floor 到秒）；调用方负责非正数/非法值的兜底文案。 */
export function formatDurationHms(remainingMs: number): string {
  const totalSeconds = Math.floor(remainingMs / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((unit) => String(unit).padStart(2, "0")).join(":")
}

/** 秒数的人性化时长单点实现：≥1 天按天、≥1 小时按小时（均保留 1 位小数），否则按分钟取整；负值归零。 */
export function formatDuration(seconds: number): string {
  if (seconds >= 86400) return `${(seconds / 86400).toFixed(1)} 天`
  if (seconds >= 3600) return `${(seconds / 3600).toFixed(1)} 小时`
  return `${Math.max(0, Math.round(seconds / 60))} 分钟`
}

/** Asia/Shanghai 日历日 `YYYY-MM-DD`；与本地浏览器时区无关。 */
export function shanghaiDateKey(date: Date = new Date()): string {
  return DATE_KEY.format(date)
}

/**
 * 提交 API 的时刻序列化单点：`YYYY-MM-DDTHH:mm:ss+08:00`（规则 15 的 ISO8601 +08:00 书面口径）。
 * 与 toISOString() 的 UTC Z 表示同一时刻，仅偏移口径不同；各视图一律改用本 helper。
 */
export function toApiDateTime(date: Date): string {
  const p = partsOf(DATE_TIME, date)
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}:${p.second}+08:00`
}

/**
 * 枚举 `YYYY-MM-DD` 日期键闭区间（纯 UTC 日期算术，键本身不作时区解释）。
 * 非法输入、倒序区间或跨度超过 maxDays 返回 null，由调用方决定兜底。
 */
export function enumerateDateKeys(start: string, end: string, maxDays = 62): string[] | null {
  const startMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(start)
  const endMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(end)
  if (!startMatch || !endMatch) return null
  const first = Date.UTC(Number(startMatch[1]), Number(startMatch[2]) - 1, Number(startMatch[3]))
  const last = Date.UTC(Number(endMatch[1]), Number(endMatch[2]) - 1, Number(endMatch[3]))
  if (last < first) return null
  const days: string[] = []
  for (let current = first; current <= last && days.length <= maxDays; current += 86_400_000) {
    const date = new Date(current)
    days.push(
      `${date.getUTCFullYear()}-${String(date.getUTCMonth() + 1).padStart(2, "0")}-${String(date.getUTCDate()).padStart(2, "0")}`,
    )
  }
  return days.length > maxDays ? null : days
}

/**
 * 以 Asia/Shanghai 日历为基准往前推 n 天的 `YYYY-MM-DD`。
 * 在「当日 00:00 +08:00」上做 24h 整数倍偏移，避免 setDate/toISOString 的时区错位。
 */
export function daysAgoDateKey(days: number, now: Date = new Date()): string {
  const midnight = new Date(`${shanghaiDateKey(now)}T00:00:00+08:00`)
  return shanghaiDateKey(new Date(midnight.getTime() - days * 86_400_000))
}

/** 日期范围转换为接口 ISO 参数；未选范围不发送起止参数。书面口径走 toApiDateTime（规则 15 的 +08:00）。 */
export function rangeToIsoParams(range: readonly [Date, Date] | null): { start?: string; end?: string } {
  return {
    start: range?.[0] ? toApiDateTime(range[0]) : undefined,
    end: range?.[1] ? toApiDateTime(range[1]) : undefined,
  }
}

/** 严格日历日的星期；以纯 UTC 日历运算避免浏览器时区偏移。 */
export function dateKeyWeekday(value: string, empty = "—"): string {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return empty
  const date = new Date(`${value}T00:00:00Z`)
  if (Number.isNaN(date.getTime()) || date.toISOString().slice(0, 10) !== value) return empty
  return ["周日", "周一", "周二", "周三", "周四", "周五", "周六"][date.getUTCDay()] ?? empty
}

/** 日期导出使用次日零点作为不包含的终点。 */
export function nextShanghaiMidnight(value: string): string {
  if (dateKeyWeekday(value) === "—") throw new Error("无效日期")
  const next = new Date(new Date(`${value}T00:00:00+08:00`).getTime() + 86_400_000)
  return `${shanghaiDateKey(next)}T00:00:00+08:00`
}
