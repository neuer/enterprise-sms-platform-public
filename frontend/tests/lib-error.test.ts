import { describe, expect, it } from "vitest"

import { errorText } from "../src/lib/error"

describe("错误文案单点 errorText", () => {
  it("Error 实例取 message", () => {
    expect(errorText(new Error("余额不足"), "操作失败")).toBe("余额不足")
  })

  it("Error 子类同样取 message", () => {
    expect(errorText(new TypeError("类型错误"), "操作失败")).toBe("类型错误")
  })

  it("空 message 的 Error 原样返回空串（保持真实错误形态，不回退）", () => {
    expect(errorText(new Error(""), "操作失败")).toBe("")
  })

  it("非 Error 抛出物回退到兜底文案", () => {
    expect(errorText("cancel", "操作失败")).toBe("操作失败")
    expect(errorText(undefined, "操作失败")).toBe("操作失败")
    expect(errorText(null, "操作失败")).toBe("操作失败")
    expect(errorText(42, "操作失败")).toBe("操作失败")
    expect(errorText({ message: "对象非 Error" }, "操作失败")).toBe("操作失败")
  })
})
