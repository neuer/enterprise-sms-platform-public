import type { PasswordPolicy } from "../api/auth"

export type ProviderCode = "local" | "ad"
export type Step =
  | "loading"
  | "blocked"
  | "choose"
  | "account"
  | "password"
  | "verifying"
  | "new-password"
  | "confirm-password"
  | "changing"
  | "done"
export type Mood = "idle" | "back" | "listen" | "shy" | "think" | "oops" | "rest" | "lost" | "fly"
export type Part = string | { strong: string }
export type ChipAction = { kind: "provider"; code: ProviderCode } | { kind: "forgot" } | { kind: "retry" }
export interface Chip {
  label: string
  action: ChipAction
  off?: boolean
  quiet?: boolean
}
export type Item =
  | { id: number; type: "bot"; parts: Part[]; tone?: "err" | "ok" }
  | { id: number; type: "rules" }
  | {
      id: number
      type: "me"
      text: string
      secret: boolean
      edit?: "provider" | "account"
      receipt: string
      failed: boolean
    }
  | { id: number; type: "sys"; text: string; tone?: "warn"; testid?: string }
  | { id: number; type: "chips"; chips: Chip[]; used: boolean }
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never
export type NewItem = DistributiveOmit<Item, "id">
export type ChipsItem = Extract<Item, { type: "chips" }>
export type MineItem = Extract<Item, { type: "me" }>
export interface PasswordRule {
  label: string
  test: (value: string) => boolean
}
export interface ProgressStep {
  label: string
  state: "done" | "current" | "todo"
}

export const PROVIDER_ORDER: ProviderCode[] = ["local", "ad"]
export const SUCCESS_PAUSE_MS = 560
export const SAY: Record<Mood, string[]> = {
  idle: ["有新消息要发？", "先登录吧"],
  back: ["欢迎回来"],
  listen: ["我在听"],
  shy: ["我转过去了", "不看你的密码"],
  think: ["正在送信…"],
  oops: ["好像不太对", "再试一次？"],
  rest: ["先歇一会儿"],
  lost: ["信没送出去"],
  fly: ["出发！"],
}
export const DEFAULT_POLICY: PasswordPolicy = {
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名；服务端检查常见或泄露密码",
}
/** 会话顶部进度条：「设置新密码」一段只在首次改密会话内出现。 */
export const PROGRESS_LABELS = ["登录方式", "账号", "密码"]
export const CHANGE_LABEL = "设置新密码"
export const CHANGE_STEPS: Step[] = ["new-password", "confirm-password", "changing"]
export const PROGRESS_INDEX: Record<Step, number> = {
  loading: 0,
  blocked: 0,
  choose: 0,
  account: 1,
  password: 2,
  verifying: 2,
  "new-password": 3,
  "confirm-password": 3,
  changing: 3,
  done: 3,
}
/** 输入框左侧标签：只说明当前要发送什么，不反映输入内容。 */
export const FIELD_TAG: Record<Step, string> = {
  loading: "连接中",
  blocked: "等待重试",
  choose: "等待选择",
  account: "账号",
  password: "密码",
  verifying: "密码",
  "new-password": "新密码",
  "confirm-password": "确认新密码",
  changing: "新密码",
  done: "已验证",
}
export const SECRET_TAG_STEPS: Step[] = ["password", "verifying", "new-password", "confirm-password", "changing"]
const CLASS_PATTERNS = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/]
const CN_DIGITS = ["零", "一", "二", "三", "四"]
export const INPUT_STEPS: Step[] = ["account", "password", "new-password", "confirm-password"]
export const EDITABLE_STEPS: Step[] = ["choose", "account", "password"]
export const PLACEHOLDER: Record<Step, string> = {
  loading: "正在连接…",
  blocked: "等待重试",
  choose: "请先在上方选择登录方式",
  account: "发送账号",
  password: "发送密码",
  verifying: "正在验证…",
  "new-password": "发送新密码",
  "confirm-password": "再次发送新密码",
  changing: "正在提交…",
  done: "正在进入工作台…",
}

/** 待命台词首句随浏览器本地时段变化，只读本地时钟，不涉及会话数据。 */
export function greetingFor(hour: number): string {
  if (hour >= 6 && hour < 11) return "早，有新消息要发？"
  if (hour >= 18 && hour < 22) return "晚上好，还有消息要发？"
  if (hour >= 22 || hour < 6) return "夜深了，还有消息要发？"
  return SAY.idle[0]
}

/** 由服务端密码策略生成首次改密客户端预检清单；账号经 getter 在校验时读取，不随清单缓存。 */
export function passwordRules(policy: PasswordPolicy, account: () => string): PasswordRule[] {
  const { min_length: min, max_length: max, required_character_classes: classes, forbid_username } = policy
  const list: PasswordRule[] = [
    { label: `${min}–${max} 位`, test: (value: string) => value.length >= min && value.length <= max },
    {
      label:
        classes >= CLASS_PATTERNS.length
          ? "大写、小写、数字、符号四类都要有"
          : `大写、小写、数字、符号至少${CN_DIGITS[classes] ?? classes}类`,
      test: (value: string) => CLASS_PATTERNS.filter((pattern) => pattern.test(value)).length >= classes,
    },
  ]
  if (forbid_username) {
    list.push({
      label: "不包含账号",
      test: (value: string) => value.length > 0 && !value.toLowerCase().includes(account().toLowerCase()),
    })
  }
  return list
}
