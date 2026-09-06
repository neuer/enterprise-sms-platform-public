import { maskPhone, PHONE_RE } from "../src/lib/phone"

describe("手机号校验与掩码单点（硬性规则 8）", () => {
  it("PHONE_RE 只接受 1 开头的 11 位数字", () => {
    expect(PHONE_RE.test("13800138000")).toBe(true)
    expect(PHONE_RE.test("19912345678")).toBe(true)

    for (const invalid of [
      "23800138000", // 非 1 开头
      "1380013800", // 10 位
      "138001380000", // 12 位
      "1380013800a", // 含非数字
      "",
      " 13800138000",
      "13800138000 ",
    ]) {
      expect(PHONE_RE.test(invalid)).toBe(false)
    }
  })

  it("maskPhone 对合法号码输出前 3 位 + **** + 后 4 位", () => {
    expect(maskPhone("13800138000")).toBe("138****8000")
    expect(maskPhone("19912345678")).toBe("199****5678")
  })

  it("maskPhone 对非标准号码原样返回（服务端为权威，前端不猜测掩码口径）", () => {
    for (const nonStandard of ["", "123", "23800138000", "1380013800"]) {
      expect(maskPhone(nonStandard)).toBe(nonStandard)
    }
  })
})
