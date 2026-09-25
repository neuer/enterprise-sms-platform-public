import { beforeEach, describe, expect, it, vi } from "vitest"

import { recallLoginProvider, rememberLoginProvider } from "../src/lib/loginProvider"

describe("loginProvider 认证源偏好", () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it("无记录时召回 null，由调用方回退服务端默认", () => {
    expect(recallLoginProvider()).toBeNull()
  })

  it("显式选择 local/ad 写入并可召回", () => {
    rememberLoginProvider("ad")
    expect(recallLoginProvider()).toBe("ad")
    rememberLoginProvider("local")
    expect(recallLoginProvider()).toBe("local")
  })

  it("非法值不写入；已存在的非法值召回为 null", () => {
    rememberLoginProvider("ldap-corp")
    expect(window.localStorage.getItem("sms-login-provider")).toBeNull()

    window.localStorage.setItem("sms-login-provider", "evil")
    expect(recallLoginProvider()).toBeNull()
  })

  it("偏好只含认证源代码，不夹带凭据类键", () => {
    rememberLoginProvider("ad")
    const keys = Object.keys(window.localStorage)
    expect(keys).toEqual(["sms-login-provider"])
    expect(window.localStorage.getItem("sms-login-provider")).toBe("ad")
  })

  it("存储不可用（隐私模式）时读写静默降级", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("denied")
    })
    expect(recallLoginProvider()).toBeNull()

    vi.restoreAllMocks()
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("denied")
    })
    expect(() => rememberLoginProvider("ad")).not.toThrow()
  })
})
