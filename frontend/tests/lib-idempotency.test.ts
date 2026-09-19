import { afterEach, describe, expect, it, vi } from "vitest"
import { isSafeBusinessId, newIdempotencyKey } from "../src/lib/idempotency"

const safe = "abcdefab-1234-4abc-8def-abcdefabcdef"
const unsafe = "abcdefab-1234-4abc-8def-a19900000001"

afterEach(() => vi.restoreAllMocks())

describe("business id privacy", () => {
  it("keeps normal UUID entropy and the existing hex contract", () => {
    vi.spyOn(crypto, "randomUUID").mockReturnValue(safe)
    expect(newIdempotencyKey()).toBe(safe.replaceAll("-", ""))
  })
  it("resamples a phone collision before the key is used", () => {
    const random = vi.spyOn(crypto, "randomUUID").mockReturnValueOnce(unsafe).mockReturnValue(safe)
    expect(newIdempotencyKey()).toBe(safe.replaceAll("-", ""))
    expect(random).toHaveBeenCalledTimes(2)
  })
  it("fails closed with a bounded faulty random source", () => {
    const random = vi.spyOn(crypto, "randomUUID").mockReturnValue(unsafe)
    expect(() => newIdempotencyKey()).toThrow("无法生成安全的业务标识")
    expect(random).toHaveBeenCalledTimes(8)
  })
})

it("rejects adjacent digits instead of exempting longer numeric runs", () => {
  const phone = "199" + "0".repeat(7) + "1"
  expect(isSafeBusinessId("0" + phone + "a".repeat(20))).toBe(false)
  expect(isSafeBusinessId(phone + "0" + "a".repeat(20))).toBe(false)
})
