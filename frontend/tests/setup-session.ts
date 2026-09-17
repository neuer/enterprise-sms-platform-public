import { beforeEach, vi } from "vitest"

import { resetAccessSessionModule } from "../src/api/sessionTokens"

const passthroughLocks = {
  request: async (_name: string, callback: () => Promise<unknown>) => callback(),
}

/** jsdom 默认没有 Web Locks；测试默认装一把直通锁，避免误入安全单标签页。 */
export function installTestWebLocks(): void {
  if (typeof globalThis.navigator?.locks?.request === "function") return
  Object.defineProperty(globalThis.navigator, "locks", {
    configurable: true,
    enumerable: true,
    writable: true,
    value: passthroughLocks,
  })
}

const unstubAllGlobals = vi.unstubAllGlobals.bind(vi)
vi.unstubAllGlobals = ((...args: Parameters<typeof vi.unstubAllGlobals>) => {
  const result = unstubAllGlobals(...args)
  installTestWebLocks()
  return result
}) as typeof vi.unstubAllGlobals

beforeEach(() => {
  resetAccessSessionModule()
  installTestWebLocks()
})
