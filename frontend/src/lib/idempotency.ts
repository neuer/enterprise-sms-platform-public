/** 元数据中的手机号子串不能借助相邻数字绕过校验。 */
export function isSafeBusinessId(value: string): boolean {
  return !/1\d{10}/u.test(value)
}

/** 保持 32 位 hex 合同；与后端一致，避免随机业务键包含手机号片段。 */
export function newIdempotencyKey(): string {
  for (let attempt = 0; attempt < 8; attempt += 1) {
    const key = crypto.randomUUID().replaceAll("-", "")
    if (isSafeBusinessId(key)) return key
  }
  throw new Error("无法生成安全的业务标识，请重试")
}
