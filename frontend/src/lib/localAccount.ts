import type { PasswordPolicy, UserRole } from "../api/auth"

const LOCAL_LOGIN_RE = /^[a-z0-9._-]{3,64}$/
const CHARACTER_CLASSES = [/\p{Ll}/u, /\p{Lu}/u, /\p{N}/u, /[^\p{L}\p{N}]/u]

export const ROLE_DESCRIPTIONS: Record<UserRole, string> = {
  admin: "全部功能，含账号维护、角色覆盖、队列恢复与强制下线",
  approver: "审批（不能审本人提交），查看全量记录与报表",
  operator: "Web 人工发送（通知/营销），本部门记录与全局模板维护",
  viewer: "本部门记录与报表",
}

export function providerLabel(providerCode: string): string {
  if (providerCode === "local") return "本地账号"
  if (providerCode === "ad") return "AD 账号"
  return providerCode.toUpperCase()
}

export function usernameProblem(value: string): string | null {
  // 与服务端 validate_local_login_name 同一规则（规范化后 3–64 位小写字符集）。
  return LOCAL_LOGIN_RE.test(value.trim().toLowerCase())
    ? null
    : "本地用户名必须为 3–64 位字母、数字、点、下划线或短横线"
}

export function passwordProblem(password: string, username: string, policy: PasswordPolicy): string | null {
  // 提交前按服务端下发的策略即时校验；服务端仍为权威校验。
  if (password.length < policy.min_length || password.length > policy.max_length) {
    return `密码长度必须为 ${policy.min_length}–${policy.max_length} 位`
  }
  const classes = CHARACTER_CLASSES.filter((re) => re.test(password)).length
  if (classes < policy.required_character_classes) {
    return `密码必须满足至少 ${policy.required_character_classes} 类：大写字母、小写字母、数字、特殊字符`
  }
  const normalized = username.trim().toLowerCase()
  if (policy.forbid_username && normalized && password.toLowerCase().includes(normalized)) {
    return "密码不能包含用户名"
  }
  return null
}

export interface Precheck {
  key: string
  label: string
  ok: boolean | null
}

/** 与服务端密码策略同口径的单项预检，未输入时保持中性（null）。 */
export function passwordPrechecks(password: string, username: string, policy: PasswordPolicy): Precheck[] {
  const lengthLabel = `长度 ${policy.min_length}–${policy.max_length} 位`
  const classesLabel = `字符类别 ≥${policy.required_character_classes}`
  if (!password) {
    return [
      { key: "length", label: lengthLabel, ok: null },
      { key: "classes", label: classesLabel, ok: null },
      { key: "forbid", label: "不含用户名", ok: null },
    ]
  }
  const classes = CHARACTER_CLASSES.filter((re) => re.test(password)).length
  const normalized = username.trim().toLowerCase()
  return [
    {
      key: "length",
      label: lengthLabel,
      ok: password.length >= policy.min_length && password.length <= policy.max_length,
    },
    { key: "classes", label: classesLabel, ok: classes >= policy.required_character_classes },
    {
      key: "forbid",
      label: "不含用户名",
      ok: !(policy.forbid_username && normalized && password.toLowerCase().includes(normalized)),
    },
  ]
}
