/**
 * 登录认证源偏好单点：只记上次显式选择的 provider code（"local" / "ad"），
 * 不含账号、密码或任何凭据——与主题偏好同属非敏感设置（硬性规则 26 不适用，
 * 规则 1 的厂商 Key 浏览器禁存清单不涉及此值）。
 * 下次打开登录页时优先选中记忆中的认证源，未开通或非法值一律回退服务端默认。
 */

const STORAGE_KEY = "sms-login-provider"
const KNOWN_CODES = new Set(["local", "ad"])

/** 读取记忆中的认证源；无记录或非法值返回 null，由调用方回退默认。 */
export function recallLoginProvider(): string | null {
  try {
    const value = window.localStorage.getItem(STORAGE_KEY)
    return value !== null && KNOWN_CODES.has(value) ? value : null
  } catch {
    return null
  }
}

/** 记住显式选择；隐私模式等写入失败时仅本次会话生效，不影响登录。 */
export function rememberLoginProvider(code: string): void {
  if (!KNOWN_CODES.has(code)) return
  try {
    window.localStorage.setItem(STORAGE_KEY, code)
  } catch {
    // 写入失败只影响下次默认选中，不阻断登录
  }
}
