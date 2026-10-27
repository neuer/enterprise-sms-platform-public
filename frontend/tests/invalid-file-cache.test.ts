import { vi } from "vitest"

const authorizedBlob = vi.hoisted(() => vi.fn(async () => new Blob(["mask"])))

vi.mock("../src/api/client", () => ({
  authorizedBlob,
  apiRequest: vi.fn(),
  DOWNLOAD_TIMEOUT_MS: 120_000,
}))

import { downloadImportInvalidFile } from "../src/api/webMessages"

describe("剔除清单下载", () => {
  it("显式禁止浏览器缓存", async () => {
    await downloadImportInvalidFile("/api/v1/web/messages/import/1/invalid-file")
    expect(authorizedBlob).toHaveBeenCalledWith(
      "/api/v1/web/messages/import/1/invalid-file",
      expect.objectContaining({ method: "GET", cache: "no-store" }),
      expect.any(Number),
    )
  })
})
