export const VENDOR_SEAL_ALGORITHM = "RSA-OAEP-256+A256GCM" as const
export const VENDOR_CREDENTIAL_SECURE_CONTEXT_ERROR =
  "当前入口不支持正式凭据安全加密，请通过临时 HTTPS 安全入口重新登录"

export interface VendorSealSession {
  session_id: string
  public_key: string
  expires_at: string
  aad: string
}

export interface VendorCredentialDraft {
  secretName: string
  secretKey: string
}

export interface VendorCredentialEnvelope {
  session_id: string
  wrapped_key: string
  nonce: string
  ciphertext: string
  aad: string
  algorithm: typeof VENDOR_SEAL_ALGORITHM
}

const encoder = new TextEncoder()

function arrayBuffer(bytes: Uint8Array): ArrayBuffer {
  const copy = new Uint8Array(bytes.byteLength)
  copy.set(bytes)
  return copy.buffer
}

function fromBase64(value: string): Uint8Array {
  const decoded = atob(value)
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0))
}

function toBase64(value: ArrayBuffer | Uint8Array): string {
  const bytes = value instanceof Uint8Array ? value : new Uint8Array(value)
  let binary = ""
  for (const byte of bytes) binary += String.fromCharCode(byte)
  return btoa(binary)
}

function credentialBytes(value: string): Uint8Array {
  const bytes = encoder.encode(value)
  if (
    bytes.byteLength < 1 ||
    bytes.byteLength > 1024 ||
    value.includes("\0") ||
    value.includes("\r") ||
    value.includes("\n")
  ) {
    bytes.fill(0)
    throw new Error("厂商凭据格式无效")
  }
  return bytes
}

function packCredentials(name: Uint8Array, key: Uint8Array): Uint8Array {
  const packed = new Uint8Array(4 + name.byteLength + key.byteLength)
  const view = new DataView(packed.buffer)
  view.setUint16(0, name.byteLength)
  view.setUint16(2, key.byteLength)
  packed.set(name, 4)
  packed.set(key, 4 + name.byteLength)
  return packed
}

export function clearCredentialDraft(draft: VendorCredentialDraft): void {
  draft.secretName = ""
  draft.secretKey = ""
}

export function isVendorCredentialSecureContext(): boolean {
  return (
    globalThis.isSecureContext === true &&
    typeof globalThis.crypto !== "undefined" &&
    typeof globalThis.crypto.subtle !== "undefined"
  )
}

export async function sealVendorCredentials(
  session: VendorSealSession,
  credentials: Readonly<VendorCredentialDraft>,
): Promise<VendorCredentialEnvelope> {
  if (!isVendorCredentialSecureContext()) {
    throw new Error(VENDOR_CREDENTIAL_SECURE_CONTEXT_ERROR)
  }
  const expiresAt = Date.parse(session.expires_at)
  if (!session.session_id || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    throw new Error("凭据密封会话已过期")
  }

  let name: Uint8Array | undefined
  let key: Uint8Array | undefined
  let plaintext: Uint8Array | undefined
  let aad: Uint8Array | undefined
  let nonce: Uint8Array | undefined
  let rawAes: Uint8Array | undefined
  try {
    name = credentialBytes(credentials.secretName)
    key = credentialBytes(credentials.secretKey)
    plaintext = packCredentials(name, key)
    aad = fromBase64(session.aad)
    nonce = crypto.getRandomValues(new Uint8Array(12))
    const publicKey = await crypto.subtle.importKey(
      "spki",
      arrayBuffer(fromBase64(session.public_key)),
      { name: "RSA-OAEP", hash: "SHA-256" },
      false,
      ["encrypt"],
    )
    const aesKey = await crypto.subtle.generateKey(
      { name: "AES-GCM", length: 256 },
      true,
      ["encrypt"],
    )
    rawAes = new Uint8Array(await crypto.subtle.exportKey("raw", aesKey))
    const [wrappedKey, ciphertext] = await Promise.all([
      crypto.subtle.encrypt({ name: "RSA-OAEP" }, publicKey, arrayBuffer(rawAes)),
      crypto.subtle.encrypt(
        {
          name: "AES-GCM",
          iv: arrayBuffer(nonce),
          additionalData: arrayBuffer(aad),
          tagLength: 128,
        },
        aesKey,
        arrayBuffer(plaintext),
      ),
    ])
    return {
      session_id: session.session_id,
      wrapped_key: toBase64(wrappedKey),
      nonce: toBase64(nonce),
      ciphertext: toBase64(ciphertext),
      aad: toBase64(aad),
      algorithm: VENDOR_SEAL_ALGORITHM,
    }
  } finally {
    name?.fill(0)
    key?.fill(0)
    plaintext?.fill(0)
    aad?.fill(0)
    nonce?.fill(0)
    rawAes?.fill(0)
  }
}
