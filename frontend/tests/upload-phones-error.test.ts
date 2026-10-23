import { afterEach, expect, it, vi } from "vitest"

import { ApiRequestError } from "../src/api/client"
import { uploadPhones } from "../src/api/webMessages"

const importId = "c0a80101-0000-4000-8000-000000000201"

function failedImport(error: string) {
  return {
    import_id: importId,
    valid: 0,
    invalid: 0,
    duplicate: 0,
    blacklisted: 0,
    invalid_download_url: null,
    expires_at: "2026-10-24T09:00:00+08:00",
    status: "failed",
    error,
  }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

it("导入失败把机器码留在 code，界面文案不再是原始枚举", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response(JSON.stringify(failedImport("IMPORT_TOO_LARGE")), { status: 200 })),
  )
  try {
    await uploadPhones(new File(["13800000000\n"], "numbers.csv", { type: "text/csv" }))
    expect.unreachable()
  } catch (error) {
    expect(error).toBeInstanceOf(ApiRequestError)
    const failed = error as ApiRequestError
    expect(failed.code).toBe("IMPORT_TOO_LARGE")
    expect(failed.message).toContain("文件大小或号码行数超过上限")
    expect(failed.message).not.toContain("IMPORT_TOO_LARGE")
  }
})

it("未知导入错误不回显原始载荷", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify(failedImport("Traceback (most recent call last): secret")), { status: 200 }),
    ),
  )
  await expect(uploadPhones(new File(["x"], "numbers.csv", { type: "text/csv" }))).rejects.toMatchObject({
    message: "号码文件处理失败，请重新上传或稍后再试。",
  })
})
