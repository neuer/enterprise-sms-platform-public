import type {
  DeliveryStatus,
  GenerationStatus,
  SecurityActionItem,
  SecurityDailyConfigurationState,
  SecurityDailyReport,
  SecurityStatus,
} from "../api/securityDaily"
import { formatDateTime } from "./time"

const statusLabels: Record<SecurityStatus, string> = {
  normal: "正常",
  attention: "关注",
  high: "高风险",
}
const generationLabels: Record<GenerationStatus, string> = {
  pending: "生成中",
  ready: "已生成",
  failed: "生成失败",
  unavailable: "数据不可用",
}
export const deliveryLabels: Record<DeliveryStatus, string> = {
  not_sent: "未投递",
  pending: "等待投递器",
  sending: "投递中",
  sent: "已投递",
  failed: "投递失败",
  unknown: "投递结果未知",
}
const configurationLabels: Record<SecurityDailyConfigurationState, string> = {
  disabled: "日报未启用",
  dispatcher_missing: "已启用，投递器未配置",
  recipients_empty: "已启用，收件人未配置",
  ready: "日报已启用",
}
const configurationTagLabels: Record<SecurityDailyConfigurationState, string> = {
  disabled: "未启用",
  dispatcher_missing: "待配置",
  recipients_empty: "待配置",
  ready: "配置完整",
}
const configurationMessages: Record<SecurityDailyConfigurationState, string> = {
  disabled: "当前未启用，不会创建下一次运行计划。",
  dispatcher_missing: "日报已启用，但独立投递器尚未配置，当前不会正常投递。",
  recipients_empty: "日报已启用，但没有收件人配置，当前不会投递。",
  ready: "当前配置完整，按固定时间生成并交由独立投递器发送。",
}
export const priorityLabels: Record<SecurityActionItem["priority"], string> = {
  high: "高",
  medium: "中",
  low: "低",
}

export const statusSegOptions = [
  { label: "全部", value: "" as SecurityStatus | "", key: "all" },
  { label: "正常", value: "normal" as SecurityStatus, key: "normal" },
  { label: "关注", value: "attention" as SecurityStatus, key: "attention" },
  { label: "高风险", value: "high" as SecurityStatus, key: "high" },
]
export const generationSegOptions = [
  { label: "全部", value: "" as GenerationStatus | "", key: "all" },
  { label: "生成中", value: "pending" as GenerationStatus, key: "pending" },
  { label: "已生成", value: "ready" as GenerationStatus, key: "ready" },
  { label: "生成失败", value: "failed" as GenerationStatus, key: "failed" },
  { label: "数据不可用", value: "unavailable" as GenerationStatus, key: "unavailable" },
]
export const deliverySegOptions = [
  { label: "全部", value: "" as DeliveryStatus | "", key: "all" },
  { label: "未投递", value: "not_sent" as DeliveryStatus, key: "not-sent" },
  { label: "等待投递器", value: "pending" as DeliveryStatus, key: "pending" },
  { label: "投递中", value: "sending" as DeliveryStatus, key: "sending" },
  { label: "已投递", value: "sent" as DeliveryStatus, key: "sent" },
  { label: "投递失败", value: "failed" as DeliveryStatus, key: "failed" },
  { label: "投递结果未知", value: "unknown" as DeliveryStatus, key: "unknown" },
]

export function statusLabel(value: string): string {
  return statusLabels[value as SecurityStatus] ?? "未知状态"
}

export function generationLabel(value: string): string {
  return generationLabels[value as GenerationStatus] ?? "数据不可用"
}

export function deliveryLabel(value: string): string {
  return deliveryLabels[value as DeliveryStatus] ?? "数据不可用"
}

export function isHistoricSupersededSent(report: {
  delivered_at?: string | null
  last_error?: string | null
}): boolean {
  return Boolean(report.delivered_at && (report.last_error ?? "").includes("配置已更新"))
}

export interface SecurityDailyStatusCopy {
  generationLabel: string
  deliveryLabel: string
  /** 内容生成事实，不含投递结果。 */
  generationFact: string
  /** 投递事实。数据不可用时说明问题通报，不宣称邮件不会发出。 */
  deliveryFact: string
  /** 没有完整结构化报告时的详情说明；就绪报告为 null。 */
  detailNotice: string | null
  sendActionLabel: string
  retryActionLabel: string
  deliveryTagType: "success" | "warning" | "danger" | "info"
}

/** 列表、详情、按钮和结果消息共用的生成/投递说明。 */
export function describeSecurityDailyReport(report: {
  generation_status: string
  delivery_status: string
  delivered_at?: string | null
  last_error?: string | null
  payload?: SecurityDailyReport["payload"] | null
}): SecurityDailyStatusCopy {
  const problemNotice = report.generation_status === "unavailable" || report.generation_status === "failed"
  const generationFact =
    report.generation_status === "unavailable"
      ? "数据不可用：当前没有完整日报数据，当前没有可展示的脱敏结构化报告"
      : report.generation_status === "failed"
        ? "生成失败：当前没有可展示的脱敏结构化报告"
        : report.generation_status === "ready"
          ? "日报已生成"
          : report.generation_status === "pending"
            ? "日报正在生成"
            : generationLabel(report.generation_status)
  const channel = problemNotice ? "问题通报" : "邮件"
  let deliveryFact = `${channel}${deliveryPhrase(report.delivery_status)}`
  if (report.delivery_status === "failed" && isHistoricSupersededSent(report)) {
    deliveryFact = "历史投递已完成"
  }
  const detailNotice = report.generation_status === "ready" ? null : `${generationFact}。${deliveryFact}。`
  return {
    generationLabel: generationLabel(report.generation_status),
    deliveryLabel: reportDeliveryLabel(report),
    generationFact,
    deliveryFact,
    detailNotice,
    sendActionLabel: "手动投递",
    retryActionLabel: "重试投递",
    deliveryTagType: reportDeliveryTagType(report),
  }
}

function deliveryPhrase(status: string): string {
  if (status === "sent") return "已投递"
  if (status === "pending") return "等待投递"
  if (status === "sending") return "投递中"
  if (status === "failed") return "投递失败"
  if (status === "unknown") return "投递结果未知"
  if (status === "not_sent") return "尚未投递"
  return "状态不可用"
}

/** 立即生成结束后的提示：生成事实和投递事实分开，避免把问题通报说成不会投递。 */
export function securityDailyGenerationMessage(report: {
  generation_status: string
  delivery_status: string
  delivered_at?: string | null
  last_error?: string | null
  payload?: SecurityDailyReport["payload"] | null
}): string {
  const copy = describeSecurityDailyReport(report)
  if (report.generation_status === "ready") {
    return "安全日报已生成 · 已新增一条记录（不覆盖历史）并提交邮件投递 · 本次操作已记入审计"
  }
  const reason = report.last_error?.trim() || copy.generationFact
  return `${reason}。${copy.deliveryFact} · 本次操作已记入审计`
}

export function reportDeliveryLabel(report: {
  delivery_status: string
  delivered_at?: string | null
  last_error?: string | null
}): string {
  if (report.delivery_status === "sent") return "已投递"
  if (report.delivery_status === "failed" && isHistoricSupersededSent(report)) {
    return "历史投递已完成"
  }
  return deliveryLabel(report.delivery_status)
}

export function reportDeliveryTagType(report: {
  delivery_status: string
  delivered_at?: string | null
  last_error?: string | null
}): "success" | "warning" | "danger" | "info" {
  if (report.delivery_status === "sent") return "success"
  if (report.delivery_status === "failed" && isHistoricSupersededSent(report)) return "success"
  return tagType(report.delivery_status)
}

export function configurationLabel(value: string): string {
  return configurationLabels[value as SecurityDailyConfigurationState] ?? "配置状态不可用"
}

export function configurationTagLabel(value: string): string {
  return configurationTagLabels[value as SecurityDailyConfigurationState] ?? "不可用"
}

export function configurationMessage(value: string): string {
  return configurationMessages[value as SecurityDailyConfigurationState] ?? "配置状态不可用，请刷新重试。"
}

export function configurationTagType(value: string): "success" | "warning" | "info" {
  if (value === "ready") return "success"
  if (value === "dispatcher_missing" || value === "recipients_empty") return "warning"
  return "info"
}

export function tagType(value: string): "success" | "warning" | "danger" | "info" {
  if (value === "normal" || value === "ready" || value === "sent" || value === "good") return "success"
  if (value === "attention" || value === "pending" || value === "sending" || value === "unknown" || value === "warn")
    return "warning"
  if (value === "high" || value === "failed" || value === "danger") return "danger"
  return "info"
}

export function priorityType(priority: SecurityActionItem["priority"]): "danger" | "warning" | "info" {
  if (priority === "high") return "danger"
  if (priority === "medium") return "warning"
  return "info"
}

export function displayPeriod(start: string, end: string): string {
  const startText = displayMoment(start)
  const endText = displayMoment(end)
  const fullLength = "YYYY-MM-DD HH:mm:ss".length
  const sameDay =
    startText.length === fullLength && endText.length === fullLength && startText.slice(0, 10) === endText.slice(0, 10)
  return sameDay ? `${startText} — ${endText.slice(11)}` : `${startText} — ${endText}`
}

export function displayMoment(value: string | null | undefined): string {
  if (value && Number.isNaN(new Date(value).valueOf())) return "数据不可用"
  return formatDateTime(value)
}
