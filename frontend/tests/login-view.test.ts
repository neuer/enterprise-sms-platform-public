import { flushPromises, mount, type VueWrapper } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { afterEach, beforeEach, vi } from "vitest"

import { resetAccessSessionModule } from "../src/api/sessionTokens"
import LoginView from "../src/views/LoginView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

const localProvider = { code: "local", name: "本地账号", auth_flow: "password" }
const adProvider = { code: "ad", name: "AD 账号", auth_flow: "password" }
const loginOk = (providerCode = "local", username = "admin") =>
  response({
    token: "jwt",
    expires_in: 900,
    refresh_expires_in: 604800,
    user: {
      account_id: 8,
      identity_id: 18,
      provider_code: providerCode,
      username,
      display_name: "平台管理员",
      dept: "平台部",
      role: "admin",
    },
  })
const policy = (overrides: Record<string, unknown> = {}) =>
  response({
    min_length: 12,
    max_length: 128,
    required_character_classes: 3,
    forbid_username: true,
    description: "12–128 位，至少包含三类字符，不能包含用户名",
    ...overrides,
  })
const changeRequired = () => response({ change_token: "change-once", expires_in: 600, next_action: "change_password" })

async function mountLogin() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", component: LoginView, meta: { public: true } },
      { path: "/dashboard", component: { template: "<div>仪表盘</div>" } },
    ],
  })
  await router.push("/login")
  await router.isReady()
  const wrapper = mount(LoginView, {
    attachTo: document.body,
    global: { plugins: [createPinia(), router, ElementPlus] },
  })
  await flushPromises()
  return { router, wrapper }
}

async function send(wrapper: VueWrapper, field: "login-username" | "login-password", value: string) {
  await wrapper.get(`[data-testid='${field}']`).setValue(value)
  await wrapper.get("form").trigger("submit")
  await flushPromises()
}

async function choose(wrapper: VueWrapper, code: "local" | "ad") {
  await wrapper.get(`[data-testid='provider-${code}']`).trigger("click")
  await flushPromises()
}

function bubbles(wrapper: VueWrapper): string[] {
  return wrapper.findAll(".login-bubble").map((bubble) => bubble.text())
}

function lastBody(fetch: ReturnType<typeof vi.fn>) {
  return JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))
}

describe("对话式登录", () => {
  let mounted: VueWrapper | null = null

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    vi.unstubAllGlobals()
  })

  afterEach(() => {
    mounted?.unmount()
    mounted = null
    vi.useRealTimers()
  })

  it("首次访问请用户显式选择认证源，未开通的 AD 只提示不切换", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([localProvider])))
    const { wrapper } = await mountLogin()
    mounted = wrapper

    expect(wrapper.findAll("main")).toHaveLength(1)
    expect(wrapper.text()).toContain("你好，我是青鸾。")
    expect(wrapper.text()).toContain("请选择登录方式")
    expect(wrapper.get("[data-testid='provider-local']").attributes("aria-disabled")).toBe("false")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-disabled")).toBe("true")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-label")).toBe("AD 账号，未开通")
    expect(wrapper.get("[data-testid='login-username']").attributes("disabled")).toBeDefined()

    await choose(wrapper, "ad")
    expect(wrapper.text()).toContain("企业目录尚未开通")
    expect(wrapper.get("[data-testid='login-username']").attributes("disabled")).toBeDefined()
    expect(localStorage.getItem("sms-login-provider")).toBeNull()
  })

  it("选择认证源后依次发送账号与密码，按所选认证源提交并进入仪表盘", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(loginOk())
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "local")
    expect(localStorage.getItem("sms-login-provider")).toBe("local")
    expect(document.activeElement).toBe(wrapper.get("[data-testid='login-username']").element)
    await send(wrapper, "login-username", "admin")
    expect(wrapper.text()).toContain("再发送密码")
    expect(document.activeElement).toBe(wrapper.get("[data-testid='login-password']").element)
    await send(wrapper, "login-password", "Temp@Password123")

    expect(fetch).toHaveBeenLastCalledWith("/api/v1/web/auth/login", expect.objectContaining({ method: "POST" }))
    expect(lastBody(fetch)).toMatchObject({
      provider_code: "local",
      username: "admin",
      password: "Temp@Password123",
      session_mode: "refresh",
      tab_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    })
    expect(wrapper.text()).toContain("验证通过，正在进入工作台")
    expect(wrapper.html()).not.toContain("Temp@Password123")
    expect(wrapper.get(".login-bubble.is-secret").text()).toBe("密码已隐藏••••••••")
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(localStorage.getItem("sms_token")).toBeNull()
  })

  it("仅 AD 开通时本地标未开通，选择 AD 后按 AD 提交", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([adProvider]))
      .mockResolvedValueOnce(loginOk("ad", "operator01"))
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "local")
    expect(wrapper.text()).toContain("本地账号尚未开通")
    await choose(wrapper, "ad")
    expect(wrapper.get("[data-testid='login-username']").attributes("placeholder")).toBe("发送企业 AD 账号")
    await send(wrapper, "login-username", "operator01")
    await send(wrapper, "login-password", "Temp@Password123")

    expect(lastBody(fetch)).toMatchObject({ provider_code: "ad", username: "operator01" })
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
  })

  it("回访时按记忆的认证源直接要账号，也可以一键换成另一个", async () => {
    localStorage.setItem("sms-login-provider", "ad")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(loginOk())
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()
    mounted = wrapper

    expect(wrapper.text()).toContain("欢迎回来。上次用的是AD 账号")
    expect(wrapper.get("[data-testid='login-username']").attributes("disabled")).toBeUndefined()
    expect(wrapper.get("[data-testid='login-username']").attributes("placeholder")).toBe("发送企业 AD 账号")

    await choose(wrapper, "local")
    expect(localStorage.getItem("sms-login-provider")).toBe("local")
    expect(wrapper.get("[data-testid='login-username']").attributes("placeholder")).toBe("发送账号")
    await send(wrapper, "login-username", "admin")
    await send(wrapper, "login-password", "Temp@Password123")
    expect(lastBody(fetch)).toMatchObject({ provider_code: "local", username: "admin" })
  })

  it("记忆中的认证源未开通或为非法值时回到显式选择", async () => {
    localStorage.setItem("sms-login-provider", "ad")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([localProvider])))
    const first = await mountLogin()
    expect(first.wrapper.text()).toContain("请选择登录方式")
    first.wrapper.unmount()

    localStorage.setItem("sms-login-provider", "oauth2")
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([localProvider, adProvider])))
    const second = await mountLogin()
    mounted = second.wrapper
    expect(second.wrapper.text()).toContain("请选择登录方式")
    expect(second.wrapper.text()).not.toContain("欢迎回来")
  })

  it("凭据错误时呈现服务端话术、清空密码并提供忘记密码指引", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED", message: "账号或密码错误", detail: null }, 401))
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "local")
    await send(wrapper, "login-username", "admin")
    await send(wrapper, "login-password", "wrong-password")

    expect(wrapper.get(".login-bubble.is-err").text()).toBe("账号或密码错误")
    expect(wrapper.get("[data-testid='login-password']").element).toHaveProperty("value", "")
    expect(document.activeElement).toBe(wrapper.get("[data-testid='login-password']").element)
    expect(wrapper.html()).not.toContain("wrong-password")

    await wrapper.get("[data-testid='login-chip-forgot']").trigger("click")
    expect(wrapper.text()).toContain("本地账号的密码由系统管理员重置")
  })

  it("点自己发过的账号即可修改后重新登录", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(response({ code: "UNAUTHORIZED", message: "账号或密码错误", detail: null }, 401))
      .mockResolvedValueOnce(loginOk("local", "admin2"))
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "local")
    await send(wrapper, "login-username", "admin")
    await send(wrapper, "login-password", "wrong-password")
    await wrapper.get("button.login-bubble.is-editable[aria-label='admin，点按修改']").trigger("click")
    await flushPromises()

    expect(wrapper.find(".login-bubble.is-err").exists()).toBe(false)
    expect(wrapper.get("[data-testid='login-username']").element).toHaveProperty("value", "admin")
    await send(wrapper, "login-username", "admin2")
    await send(wrapper, "login-password", "Temp@Password123")
    expect(lastBody(fetch)).toMatchObject({ provider_code: "local", username: "admin2" })
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
  })

  it("账号锁定时如实提示且仍可继续发送密码", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(response({ code: "ACCOUNT_LOCKED", message: "账号已锁定，请稍后重试", detail: null }, 423))
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "ad")
    await send(wrapper, "login-username", "operator01")
    await send(wrapper, "login-password", "wrong")

    expect(wrapper.get("[role='alert']").text()).toContain("账号已锁定，请稍后重试")
    expect(wrapper.get("main").attributes("data-mood")).toBe("rest")
    expect(wrapper.find("[data-testid='login-chip-forgot']").exists()).toBe(false)
    expect(wrapper.get("[data-testid='login-password']").attributes("disabled")).toBeUndefined()
  })

  it("服务不可用时把密码气泡标为发送失败并提示稍后重发", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(
        response({ code: "AUTH_SESSION_UNAVAILABLE", message: "认证服务暂不可用，请稍后重试", detail: null }, 503),
      )
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()
    mounted = wrapper

    await choose(wrapper, "local")
    await send(wrapper, "login-username", "admin")
    await send(wrapper, "login-password", "Temp@Password123")

    expect(wrapper.find(".login-row.is-failed").exists()).toBe(true)
    expect(wrapper.get(".login-receipt.is-failed").text()).toBe("发送失败")
    expect(wrapper.get(".login-sys.is-warn").text()).toBe("认证服务暂不可用，请稍后重试")
    expect(wrapper.get("[data-testid='login-status']").text()).toBe("连接不稳定")
    expect(wrapper.get("[data-testid='login-password']").attributes("disabled")).toBeUndefined()
  })

  it("认证源读取失败时给出重试入口", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response({ code: "INTERNAL_ERROR", message: "认证源暂不可用", detail: null }, 500))
      .mockResolvedValueOnce(response([localProvider]))
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()
    mounted = wrapper

    expect(wrapper.text()).toContain("认证源暂不可用")
    expect(wrapper.get("[data-testid='login-username']").attributes("disabled")).toBeDefined()
    await wrapper.get("[data-testid='login-chip-retry']").trigger("click")
    await flushPromises()
    expect(wrapper.text()).toContain("请选择登录方式")
    expect(wrapper.text()).not.toContain("认证源暂不可用")
  })

  it("密码管理器在账号步一次填好账号和密码时直接验证", async () => {
    localStorage.setItem("sms-login-provider", "local")
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(loginOk())
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()
    mounted = wrapper

    await wrapper.get("[data-testid='login-username']").setValue("admin")
    await wrapper.get("[data-testid='login-password']").setValue("Temp@Password123")
    await flushPromises()

    expect(wrapper.text()).toContain("已使用浏览器保存的密码")
    expect(wrapper.text()).not.toContain("再发送密码")
    expect(lastBody(fetch)).toMatchObject({ provider_code: "local", username: "admin", password: "Temp@Password123" })
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
  })

  it("无 Web Locks 时提示短会话并提交 access_only", async () => {
    vi.stubGlobal("navigator", {})
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(
        response({
          session_mode: "access_only",
          token: "jwt",
          expires_in: 900,
          user: {
            account_id: 8,
            identity_id: 18,
            provider_code: "local",
            username: "admin",
            display_name: "平台管理员",
            dept: "平台部",
            role: "admin",
          },
        }),
      )
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()
    mounted = wrapper

    expect(wrapper.get("[data-testid='login-access-only']").text()).toContain("短会话模式")
    await choose(wrapper, "local")
    await send(wrapper, "login-username", "admin")
    await send(wrapper, "login-password", "Temp@Password123")

    expect(lastBody(fetch)).toEqual({
      provider_code: "local",
      username: "admin",
      password: "Temp@Password123",
      session_mode: "access_only",
    })
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
  })

  it("具备 Web Locks 时不显示短会话提示", async () => {
    vi.stubGlobal("navigator", {
      locks: {
        request: async (_name: string, _options: { signal?: AbortSignal }, callback: () => Promise<unknown>) =>
          callback(),
      },
    })
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([localProvider])))
    const { wrapper } = await mountLogin()
    mounted = wrapper

    expect(wrapper.find("[data-testid='login-access-only']").exists()).toBe(false)
  })
})

describe("首次登录在对话内改密", () => {
  let mounted: VueWrapper | null = null

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    vi.unstubAllGlobals()
    localStorage.setItem("sms-login-provider", "local")
  })

  afterEach(() => {
    mounted?.unmount()
    mounted = null
    vi.useRealTimers()
  })

  async function reachChange(fetch: ReturnType<typeof vi.fn>) {
    vi.stubGlobal("fetch", fetch)
    const result = await mountLogin()
    mounted = result.wrapper
    await send(result.wrapper, "login-username", "admin")
    await send(result.wrapper, "login-password", "Temp@Password123")
    return result
  }

  it("改密令牌只留在组件内，规则清单跟随服务端策略并随输入打勾", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(
        policy({
          min_length: 16,
          max_length: 64,
          required_character_classes: 4,
          description: "16–64 位，四类字符全部必须出现，不能包含用户名",
        }),
      )
    const { router, wrapper } = await reachChange(fetch)

    expect(router.currentRoute.value.path).toBe("/login")
    expect(wrapper.text()).toContain("这是你第一次登录，需要先设置一个新密码")
    const rules = wrapper.get("[data-testid='login-password-rules']")
    expect(rules.text()).toContain("16–64 位")
    expect(rules.text()).toContain("大写、小写、数字、符号四类都要有")
    expect(rules.text()).toContain("不包含账号")
    expect(rules.text()).toContain("16–64 位，四类字符全部必须出现，不能包含用户名")
    expect(wrapper.get("[data-testid='login-password']").attributes("autocomplete")).toBe("new-password")
    expect(JSON.stringify(sessionStorage)).not.toContain("change-once")
    expect(JSON.stringify(localStorage)).not.toContain("change-once")
    expect(sessionStorage.getItem("sms_token")).toBeNull()

    await wrapper.get("[data-testid='login-password']").setValue("Qingluan#2026")
    expect(rules.findAll("li").map((li) => li.classes().includes("is-ok"))).toEqual([false, true, true])
  })

  it("新密码不满足规则或两次不一致时不调用改密接口", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(policy())
    const { wrapper } = await reachChange(fetch)

    await send(wrapper, "login-password", "short")
    expect(bubbles(wrapper).at(-1)).toContain("还差一点：12–128 位")

    await send(wrapper, "login-password", "New@Password123")
    expect(wrapper.text()).toContain("请再发送一次，确认新密码")
    await send(wrapper, "login-password", "Different@123")
    expect(bubbles(wrapper).at(-1)).toBe("两次输入的密码不一致，请重新发送新密码。")
    expect(fetch).toHaveBeenCalledTimes(3)
  })

  it("改密成功后清除令牌并请用户用新密码重新登录", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(policy())
      .mockResolvedValueOnce(response(null))
      .mockResolvedValueOnce(loginOk())
    const { router, wrapper } = await reachChange(fetch)

    await send(wrapper, "login-password", "New@Password123")
    await send(wrapper, "login-password", "New@Password123")

    expect(fetch).toHaveBeenLastCalledWith(
      "/api/v1/web/auth/password/initial",
      expect.objectContaining({
        body: JSON.stringify({ change_token: "change-once", new_password: "New@Password123" }),
      }),
    )
    expect(wrapper.text()).toContain("密码已更新。请用新密码重新登录。")
    expect(wrapper.get("[data-testid='login-password']").attributes("autocomplete")).toBe("current-password")
    expect(wrapper.html()).not.toContain("New@Password123")

    await send(wrapper, "login-password", "New@Password123")
    expect(lastBody(fetch)).toMatchObject({ username: "admin", password: "New@Password123" })
    await vi.waitFor(() => expect(router.currentRoute.value.path).toBe("/dashboard"))
  })

  it("服务端拒绝失效令牌时回到重新登录", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(policy())
      .mockResolvedValueOnce(
        response({ code: "UNAUTHORIZED", message: "改密令牌无效、已过期或已使用", detail: null }, 401),
      )
    const { wrapper } = await reachChange(fetch)

    await send(wrapper, "login-password", "New@Password123")
    await send(wrapper, "login-password", "New@Password123")

    expect(bubbles(wrapper).at(-1)).toBe("改密令牌无效、已过期或已使用")
    expect(wrapper.get("[data-testid='login-password']").attributes("placeholder")).toBe("发送密码")
  })

  it("服务端事务失败时保留改密会话，重新发送即可用同一令牌完成", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(policy())
      .mockResolvedValueOnce(response({ code: "INTERNAL_ERROR", message: "服务内部错误", detail: null }, 500))
      .mockResolvedValueOnce(response(null))
    const { wrapper } = await reachChange(fetch)

    await send(wrapper, "login-password", "New@Password123")
    await send(wrapper, "login-password", "New@Password123")
    expect(wrapper.get(".login-sys.is-warn").text()).toContain("密码修改未提交")
    expect(wrapper.get("[data-testid='login-password']").attributes("placeholder")).toBe("发送新密码")

    await send(wrapper, "login-password", "New@Password123")
    await send(wrapper, "login-password", "New@Password123")
    expect(lastBody(fetch)).toEqual({ change_token: "change-once", new_password: "New@Password123" })
    expect(wrapper.text()).toContain("密码已更新")
  })

  it("改密会话到期后立即销毁并要求重新登录", async () => {
    vi.useFakeTimers()
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(changeRequired())
      .mockResolvedValueOnce(policy())
    const { wrapper } = await reachChange(fetch)

    await vi.advanceTimersByTimeAsync(600_001)

    expect(bubbles(wrapper).at(-1)).toBe("改密会话已过期，请重新登录。")
    expect(wrapper.get("[data-testid='login-password']").attributes("placeholder")).toBe("发送密码")
  })
})
