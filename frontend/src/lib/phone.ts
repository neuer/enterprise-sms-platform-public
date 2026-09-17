/**
 * 手机号校验与掩码单点实现（硬性规则 8：`^1\d{10}$`，11 位）。
 * 服务端为权威；前端仅用于提交前即时提示与过渡展示。
 */

export const PHONE_RE = /^1\d{10}$/

/** 与服务端掩码口径一致：前 3 位 + **** + 后 4 位；非标准号码原样返回。 */
export function maskPhone(phone: string): string {
  if (!PHONE_RE.test(phone)) return phone
  return `${phone.slice(0, 3)}****${phone.slice(-4)}`
}

/** 可选手机号查询的即时提示；空值表示不筛选，服务端仍执行权威校验。 */
export function phoneProblem(value: string): string | undefined {
  return value && !PHONE_RE.test(value) ? "手机号须为 11 位以 1 开头的数字" : undefined
}
