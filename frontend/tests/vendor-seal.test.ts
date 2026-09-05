import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { clearCredentialDraft, sealVendorCredentials } from "../src/lib/vendorSeal"

const secureCrypto = globalThis.crypto
const secureContextError = "当前入口不支持正式凭据安全加密，请通过临时 HTTPS 安全入口重新登录"

function base64(bytes: ArrayBuffer): string {
  return btoa(String.fromCharCode(...new Uint8Array(bytes)))
}

function decode(value: string): ArrayBuffer {
  return Uint8Array.from(atob(value), (character) => character.charCodeAt(0)).buffer
}

describe("浏览器正式凭据密封", () => {
  beforeEach(() => {
    vi.stubGlobal("isSecureContext", true)
    vi.stubGlobal("crypto", secureCrypto)
    localStorage.clear()
    sessionStorage.clear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("只生成后端固定 envelope，并以 session AAD 完成可验证的混合加密", async () => {
    const keys = await crypto.subtle.generateKey(
      {
        name: "RSA-OAEP",
        modulusLength: 2048,
        publicExponent: new Uint8Array([1, 0, 1]),
        hash: "SHA-256",
      },
      true,
      ["encrypt", "decrypt"],
    )
    const publicKey = base64(await crypto.subtle.exportKey("spki", keys.publicKey))
    const secretName = "formal-secret-name"
    const secretKey = "formal-secret-key"
    const boundAad =
      'sms-platform:vendor-credentials:v2:{"actor":"admin","expires_at":"2026-07-17T08:02:00+00:00","operation":"install_credentials","session_id":"session-123"}'

    const envelope = await sealVendorCredentials(
      {
        session_id: "session-123",
        public_key: publicKey,
        expires_at: new Date(Date.now() + 60_000).toISOString(),
        aad: base64(new TextEncoder().encode(boundAad).buffer),
      },
      { secretName, secretKey },
    )

    expect(Object.keys(envelope).sort()).toEqual(
      ["aad", "algorithm", "ciphertext", "nonce", "session_id", "wrapped_key"].sort(),
    )
    expect(envelope.algorithm).toBe("RSA-OAEP-256+A256GCM")
    expect(new TextDecoder().decode(decode(envelope.aad))).toBe(boundAad)
    expect(JSON.stringify(envelope)).not.toContain(secretName)
    expect(JSON.stringify(envelope)).not.toContain(secretKey)

    const aesRaw = await crypto.subtle.decrypt({ name: "RSA-OAEP" }, keys.privateKey, decode(envelope.wrapped_key))
    const aesKey = await crypto.subtle.importKey("raw", aesRaw, "AES-GCM", false, ["decrypt"])
    const plaintext = new Uint8Array(
      await crypto.subtle.decrypt(
        { name: "AES-GCM", iv: decode(envelope.nonce), additionalData: decode(envelope.aad) },
        aesKey,
        decode(envelope.ciphertext),
      ),
    )
    const view = new DataView(plaintext.buffer)
    const nameLength = view.getUint16(0)
    const keyLength = view.getUint16(2)
    expect(new TextDecoder().decode(plaintext.slice(4, 4 + nameLength))).toBe(secretName)
    expect(new TextDecoder().decode(plaintext.slice(4 + nameLength, 4 + nameLength + keyLength))).toBe(secretKey)
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
  })

  it("无论提交结果如何都可原地清空组件局部草稿", () => {
    const draft = { secretName: "formal-secret-name", secretKey: "formal-secret-key" }

    clearCredentialDraft(draft)

    expect(draft).toEqual({ secretName: "", secretKey: "" })
  })

  it.each([
    ["非安全上下文", false, secureCrypto],
    ["缺少 WebCrypto subtle", true, { getRandomValues: secureCrypto.getRandomValues.bind(secureCrypto) } as Crypto],
  ])("%s 时在读取凭据前返回固定安全错误", async (_label, isSecure, browserCrypto) => {
    vi.stubGlobal("isSecureContext", isSecure)
    vi.stubGlobal("crypto", browserCrypto)
    const credentials = {
      get secretName(): string {
        throw new Error("sentinel-name-was-read")
      },
      get secretKey(): string {
        throw new Error("sentinel-key-was-read")
      },
    }

    await expect(
      sealVendorCredentials(
        {
          session_id: "session-unsafe",
          public_key: "unused",
          expires_at: new Date(Date.now() + 60_000).toISOString(),
          aad: "unused",
        },
        credentials,
      ),
    ).rejects.toThrow(secureContextError)
  })
})
