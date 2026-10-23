import { describe, expect, it } from "vitest"

import { importFailureMessage } from "../src/api/webMessages"

describe("异步导入错误文案", () => {
  it("超限码说明文件大小或行数，不回显机器码", () => {
    const text = importFailureMessage("IMPORT_TOO_LARGE")
    expect(text).toContain("文件大小或号码行数超过上限")
    expect(text).not.toContain("IMPORT_TOO_LARGE")
    expect(text).not.toContain("仅字节")
  })

  it("格式、解析、处理中和未知码各有稳定中文，未知码不回显原文", () => {
    expect(importFailureMessage("IMPORT_FORMAT_INVALID")).toContain("CSV")
    expect(importFailureMessage("IMPORT_PARSE_FAILED")).toContain("重新上传")
    expect(importFailureMessage("IMPORT_PENDING")).toContain("稍后重试")
    expect(importFailureMessage("IMPORT_QUEUE_UNAVAILABLE")).toContain("重新上传")
    const unknown = importFailureMessage("Traceback: secret")
    expect(unknown).toBe("号码文件处理失败，请重新上传或稍后再试。")
    expect(unknown).not.toContain("Traceback")
  })
})
