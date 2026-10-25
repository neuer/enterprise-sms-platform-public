import { CONTENT_CODE_POINT_MAX, codePointLength, exceedsCodePoints } from "../src/lib/codePoints"

describe("Unicode 码点长度", () => {
  it("非 BMP 字符按码点计数，并在上限后停止", () => {
    expect(codePointLength("🚀".repeat(251))).toBe(251)
    expect(codePointLength("🚀".repeat(800), CONTENT_CODE_POINT_MAX)).toBe(501)
    expect(exceedsCodePoints("🚀".repeat(500), 500)).toBe(false)
    expect(exceedsCodePoints("🚀".repeat(501), 500)).toBe(true)
    expect(codePointLength("汉字")).toBe(2)
  })
})
