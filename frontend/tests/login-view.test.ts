import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { createPinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"
import { beforeEach, vi } from "vitest"

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

function mountLogin() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", component: LoginView, meta: { public: true } },
      { path: "/change-password", component: { template: "<div>修改密码</div>" } },
      { path: "/dashboard", component: { template: "<div>仪表盘</div>" } },
    ],
  })
  return router.push("/login").then(async () => {
    await router.isReady()
    const wrapper = mount(LoginView, {
      global: { plugins: [createPinia(), router, ElementPlus] },
    })
    await flushPromises()
    return { router, wrapper }
  })
}

describe("登录页", () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    resetAccessSessionModule()
    vi.unstubAllGlobals()
  })

  it("始终显示两种认证源，未启用 AD 标未开通", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response([localProvider])))

    const { wrapper } = await mountLogin()

    expect(wrapper.findAll("main")).toHaveLength(1)
    expect(wrapper.get("main.login-screen").find(".login-card").exists()).toBe(true)
    expect(wrapper.find("[role='radiogroup'][aria-label='认证源']").exists()).toBe(true)
    expect(wrapper.find(".provider-switch.solo").exists()).toBe(false)
    expect(wrapper.text()).toContain("本地账号")
    expect(wrapper.text()).toContain("AD 账号")
    expect(wrapper.text()).toContain("未开通")
    expect(wrapper.text()).toContain("管理员维护的平台内置账号")
    expect(wrapper.text()).not.toContain("当前唯一可用认证源")
    expect(wrapper.text()).not.toContain("LOCAL")
    expect(wrapper.find(".provider-lane").exists()).toBe(false)
    expect(wrapper.get("[data-testid='provider-local']").classes()).toContain("on")
    expect(wrapper.get("[data-testid='provider-local']").attributes("aria-disabled")).toBe("false")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-disabled")).toBe("true")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-label")).toBe("AD 账号，未开通")

    await wrapper.get("[data-testid='provider-ad']").trigger("click")
    expect(wrapper.get("[data-testid='provider-local']").classes()).toContain("on")
    expect(wrapper.get("[data-testid='provider-ad']").classes()).not.toContain("on")
    expect(wrapper.text()).toContain("企业目录尚未开通")
    expect(wrapper.get(".login-field-label").text()).toBe("账号")
  })

  it("默认选中第一个认证源，未切换时按默认源提交", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(
        response({
          token: "jwt",
          expires_in: 900,
          refresh_expires_in: 604800,
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

    expect(wrapper.get("[data-testid='provider-local']").classes()).toContain("on")
    expect(wrapper.get("[data-testid='provider-local']").attributes("aria-checked")).toBe("true")
    expect(wrapper.get("[data-testid='provider-local']").attributes("aria-disabled")).toBe("false")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-disabled")).toBe("false")
    expect(wrapper.find(".provider-switch-sep").exists()).toBe(true)
    expect(wrapper.find(".provider-off-note").exists()).toBe(false)
    expect(wrapper.text()).toContain("管理员维护的平台内置账号")
    expect(wrapper.text()).not.toContain("LOCAL")
    expect(wrapper.text()).not.toContain("未开通")
    await wrapper.get("[data-testid='login-username']").setValue("admin")
    await wrapper.get("[data-testid='login-password']").setValue("Temp@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))).toMatchObject({
      provider_code: "local",
      username: "admin",
      password: "Temp@Password123",
    })
    expect(router.currentRoute.value.path).toBe("/dashboard")
  })

  it("把显式认证源与凭据提交给服务端并进入仪表盘", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(
        response({
          token: "jwt",
          expires_in: 900,
          refresh_expires_in: 604800,
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

    await wrapper.get("[data-testid='provider-local']").trigger("click")
    await wrapper.get("[data-testid='login-username']").setValue("admin")
    await wrapper.get("[data-testid='login-password']").setValue("Temp@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(fetch).toHaveBeenLastCalledWith(
      "/api/v1/web/auth/login",
      expect.objectContaining({
        method: "POST",
      }),
    )
    expect(JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))).toMatchObject({
      provider_code: "local",
      username: "admin",
      password: "Temp@Password123",
      tab_id: expect.stringMatching(/^[0-9a-f]{32}$/),
    })
    expect(router.currentRoute.value.path).toBe("/dashboard")
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_refresh_token")).toBeNull()
    expect(localStorage.getItem("sms_token")).toBeNull()
  })

  it("仅 AD 开通时本地标未开通且默认走 AD", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([adProvider]))
      .mockResolvedValueOnce(
        response({
          token: "jwt",
          expires_in: 900,
          refresh_expires_in: 604800,
          user: {
            account_id: 8,
            identity_id: 18,
            provider_code: "ad",
            username: "operator01",
            display_name: "目录用户",
            dept: "平台部",
            role: "operator",
          },
        }),
      )
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()

    expect(wrapper.get("[data-testid='provider-ad']").classes()).toContain("on")
    expect(wrapper.get("[data-testid='provider-local']").attributes("aria-disabled")).toBe("true")
    expect(wrapper.get(".login-field-label").text()).toBe("企业 AD 账号")
    expect(wrapper.text()).toContain("通过企业目录验证身份")
    await wrapper.get("[data-testid='provider-local']").trigger("click")
    expect(wrapper.get("[data-testid='provider-ad']").classes()).toContain("on")
    expect(wrapper.text()).toContain("本地账号尚未开通")
    await wrapper.get("[data-testid='login-username']").setValue("operator01")
    await wrapper.get("[data-testid='login-password']").setValue("Temp@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(JSON.parse(String(fetch.mock.calls.at(-1)?.[1]?.body))).toMatchObject({
      provider_code: "ad",
      username: "operator01",
    })
    expect(router.currentRoute.value.path).toBe("/dashboard")
  })

  it("首次登录把改密令牌仅保留在登录组件内并原位显示改密表单", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider]))
      .mockResolvedValueOnce(
        response({
          change_token: "change-once",
          expires_in: 600,
          next_action: "change_password",
        }),
      )
    vi.stubGlobal("fetch", fetch)
    const { router, wrapper } = await mountLogin()

    await wrapper.get("[data-testid='provider-local']").trigger("click")
    await wrapper.get("[data-testid='login-username']").setValue("admin")
    await wrapper.get("[data-testid='login-password']").setValue("Temp@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(router.currentRoute.value.path).toBe("/login")
    expect(wrapper.text()).toContain("设置新密码")
    expect(wrapper.find("[data-testid='new-password']").exists()).toBe(true)
    expect(JSON.stringify(sessionStorage)).not.toContain("change-once")
    expect(JSON.stringify(localStorage)).not.toContain("change-once")
    expect(sessionStorage.getItem("sms_token")).toBeNull()
    expect(sessionStorage.getItem("sms_user")).toBeNull()
  })

  it("切换认证源后更新账号占位并呈现服务端中文错误", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(response([localProvider, adProvider]))
      .mockResolvedValueOnce(response({ message: "账号已锁定，请稍后重试" }, 423))
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountLogin()

    await wrapper.get("[data-testid='provider-ad']").trigger("click")
    expect(wrapper.get("[data-testid='provider-ad']").classes()).toContain("on")
    expect(wrapper.get("[data-testid='provider-ad']").attributes("aria-checked")).toBe("true")
    expect(wrapper.text()).toContain("通过企业目录验证身份")
    expect(wrapper.get(".login-field-label").text()).toBe("企业 AD 账号")
    expect(wrapper.get("[data-testid='login-username']").attributes("placeholder")).toBe(
      "企业 AD 账号",
    )
    await wrapper.get("[data-testid='login-username']").setValue("operator01")
    await wrapper.get("[data-testid='login-password']").setValue("wrong")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(wrapper.get("[role='alert']").text()).toContain("账号已锁定，请稍后重试")
    expect(wrapper.get("[data-testid='login-password']").element).toHaveProperty("value", "")
  })
})
