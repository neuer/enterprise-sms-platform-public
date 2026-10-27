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

/** 筛选控件绑定的无时区上海钟面时间，与浏览器时区无关。 */
export const SHANGHAI_CIVIL_VALUE_FORMAT = "YYYY-MM-DD HH:mm:ss"

export type ShanghaiCivilRange = readonly [string, string]

const CIVIL_TIME = /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/

function pad2(value: number): string {
  return String(value).padStart(2, "0")
}

/** 严格校验上海钟面时间并写成 `YYYY-MM-DDTHH:mm:ss+08:00`；非法日历返回 undefined。 */
export function shanghaiCivilToApi(value: string): string | undefined {
  const match = CIVIL_TIME.exec(value.trim())
  if (!match) return undefined
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const hour = Number(match[4])
  const minute = Number(match[5])
  const second = Number(match[6] ?? "0")
  if (hour > 23 || minute > 59 || second > 59) return undefined
  const probe = new Date(0)
  probe.setUTCFullYear(year, month - 1, day)
  probe.setUTCHours(0, 0, 0, 0)
  if (probe.getUTCFullYear() !== year || probe.getUTCMonth() !== month - 1 || probe.getUTCDate() !== day) {
    return undefined
  }
  return `${match[1]}-${match[2]}-${match[3]}T${pad2(hour)}:${pad2(minute)}:${pad2(second)}+08:00`
}

/**
 * 日期范围转换为接口 ISO 参数。
 * 输入是用户看到的上海钟面时间，不把浏览器本地 Date 再换算一次。
 * 空范围、任一侧非法时不发送起止；合法倒序原样保留，由服务端裁决。
 */
export function rangeToIsoParams(range: ShanghaiCivilRange | null): { start?: string; end?: string } {
  if (!range) return { start: undefined, end: undefined }
  const start = shanghaiCivilToApi(range[0])
  const end = shanghaiCivilToApi(range[1])
  if (!start || !end) return { start: undefined, end: undefined }
  return { start, end }
}

/** 把该时刻的上海钟面写成本地 Date，只供日期控件按用户看到的数字回写。 */
export function dateForShanghaiClock(instant: Date): Date {
  const p = partsOf(DATE_TIME, instant)
  const hour = p.hour === "24" ? 0 : Number(p.hour)
  return new Date(Number(p.year), Number(p.month) - 1, Number(p.day), hour, Number(p.minute), Number(p.second))
}

/** 上海自然日 00:00 的控件值；不使用浏览器本地 setHours。 */
export function dateForShanghaiDayStart(now: Date = new Date()): Date {
  const [year, month, day] = shanghaiDateKey(now).split("-").map(Number)
  return new Date(year, month - 1, day, 0, 0, 0)
}

/** 相对当前时刻的真实时长，两端都按上海钟面交给控件。 */
export function trailingShanghaiRange(durationMs: number, now: Date = new Date()): [Date, Date] {
  return [dateForShanghaiClock(new Date(now.getTime() - durationMs)), dateForShanghaiClock(now)]
}

export type ReportPeriodGranularity = "day" | "week" | "month"

interface CivilDate {
  year: number
  month: number
  day: number
}

/** 严格解析 YYYY-MM-DD。年份 0–99 用 setUTCFullYear，避免 Date.UTC 的 1900 偏移。 */
function parseCivilDate(value: string): CivilDate | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return null
  const year = Number(match[1])
  const month = Number(match[2])
  const day = Number(match[3])
  const date = new Date(0)
  date.setUTCFullYear(year, month - 1, day)
  date.setUTCHours(0, 0, 0, 0)
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null
  return { year, month, day }
}

function civilUtc(date: CivilDate): Date {
  const value = new Date(0)
  value.setUTCFullYear(date.year, date.month - 1, date.day)
  value.setUTCHours(0, 0, 0, 0)
  return value
}

function civilOrdinal(date: CivilDate): number {
  return Math.floor(civilUtc(date).getTime() / 86_400_000)
}

/** PostgreSQL date_trunc('week')：周一为一周起点。 */
function mondayOrdinal(date: CivilDate): number {
  const weekday = civilUtc(date).getUTCDay()
  const delta = weekday === 0 ? 6 : weekday - 1
  return civilOrdinal(date) - delta
}

/**
 * 统计已查询闭区间内的周期数，含零发送日/周/月。
 * 只接受结果上的 start/end/granularity，O(1)，不受图表 62 天补零上限影响。
 */
export function countReportPeriods(start: string, end: string, granularity: ReportPeriodGranularity): number | null {
  const first = parseCivilDate(start)
  const last = parseCivilDate(end)
  if (!first || !last) return null
  const startOrdinal = civilOrdinal(first)
  const endOrdinal = civilOrdinal(last)
  if (endOrdinal < startOrdinal) return null
  if (granularity === "day") return endOrdinal - startOrdinal + 1
  if (granularity === "week") return (mondayOrdinal(last) - mondayOrdinal(first)) / 7 + 1
  if (granularity === "month") return last.year * 12 + last.month - (first.year * 12 + first.month) + 1
  return null
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
