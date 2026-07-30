import { flushPromises, mount } from "@vue/test-utils"
import ElementPlus from "element-plus"
import { afterEach, beforeEach, vi } from "vitest"

import PasswordChangeView from "../src/views/PasswordChangeView.vue"

function response(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    json: async () => body,
  }
}

async function mountView() {
  const wrapper = mount(PasswordChangeView, {
    props: {
      changeToken: "change-once",
      expiresAt: Date.now() + 600_000,
    },
    global: { plugins: [ElementPlus] },
  })
  await flushPromises()
  return { wrapper }
}

describe("首次登录修改密码", () => {
  beforeEach(() => {
    sessionStorage.clear()
    localStorage.clear()
    vi.unstubAllGlobals()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it("持续显示服务端密码规则与单用途说明", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        response({
          min_length: 12,
          max_length: 128,
          required_character_classes: 3,
          forbid_username: true,
          description: "12–128 位，至少包含三类字符，不能包含用户名",
        }),
      ),
    )

    const { wrapper } = await mountView()

    expect(wrapper.text()).toContain("首次登录必须修改密码")
    expect(wrapper.text()).toContain("12–128 位")
    expect(wrapper.text()).toContain("至少包含三类字符")
    expect(wrapper.text()).toContain("不能包含用户名")
    expect(wrapper.text()).toContain("完成后请使用新密码重新登录")
  })

  it("确认密码不一致时不调用改密接口", async () => {
    const fetch = vi.fn().mockResolvedValue(
      response({
        min_length: 12,
        max_length: 128,
        required_character_classes: 3,
        forbid_username: true,
        description: "12–128 位，至少包含三类字符，不能包含用户名",
      }),
    )
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountView()

    await wrapper.get("[data-testid='new-password']").setValue("New@Password123")
    await wrapper.get("[data-testid='confirm-password']").setValue("Different@123")
    await wrapper.get("form").trigger("submit")

    expect(wrapper.get("[role='alert']").text()).toContain("两次输入的密码不一致")
    expect(fetch).toHaveBeenCalledTimes(1)
  })

  it("改密成功后清除令牌并返回登录页", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          min_length: 12,
          max_length: 128,
          required_character_classes: 3,
          forbid_username: true,
          description: "12–128 位，至少包含三类字符，不能包含用户名",
        }),
      )
      .mockResolvedValueOnce(response(null))
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountView()

    await wrapper.get("[data-testid='new-password']").setValue("New@Password123")
    await wrapper.get("[data-testid='confirm-password']").setValue("New@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(fetch).toHaveBeenLastCalledWith(
      "/api/v1/web/auth/password/initial",
      expect.objectContaining({
        body: JSON.stringify({
          change_token: "change-once",
          new_password: "New@Password123",
        }),
      }),
    )
    expect(sessionStorage.getItem("sms_change_token")).toBeNull()
    expect(wrapper.emitted("completed")).toHaveLength(1)
  })

  it("服务端拒绝失效令牌时清除改密状态并返回登录页", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          min_length: 12,
          max_length: 128,
          required_character_classes: 3,
          forbid_username: true,
          description: "12–128 位，至少包含三类字符，不能包含用户名",
        }),
      )
      .mockResolvedValueOnce(
        response(
          { code: "UNAUTHORIZED", message: "改密令牌无效、已过期或已使用", detail: null },
          401,
        ),
      )
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountView()

    await wrapper.get("[data-testid='new-password']").setValue("New@Password123")
    await wrapper.get("[data-testid='confirm-password']").setValue("New@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(sessionStorage.getItem("sms_change_token")).toBeNull()
    expect(sessionStorage.getItem("sms_change_token_expires_at")).toBeNull()
    expect(wrapper.emitted("invalid")?.[0]).toEqual([
      "改密令牌无效、已过期或已使用",
    ])
  })

  it("数据库事务失败时保留令牌并明确提示可重试", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(
        response({
          min_length: 12,
          max_length: 128,
          required_character_classes: 3,
          forbid_username: true,
          description: "12–128 位，至少包含三类字符，不能包含用户名",
        }),
      )
      .mockResolvedValueOnce(
        response({ code: "INTERNAL_ERROR", message: "服务内部错误", detail: null }, 500),
      )
    vi.stubGlobal("fetch", fetch)
    const { wrapper } = await mountView()

    await wrapper.get("[data-testid='new-password']").setValue("New@Password123")
    await wrapper.get("[data-testid='confirm-password']").setValue("New@Password123")
    await wrapper.get("form").trigger("submit")
    await flushPromises()

    expect(wrapper.get("[role='alert']").text()).toContain("密码修改未提交")
    expect(JSON.stringify(sessionStorage)).not.toContain("change-once")
    expect(wrapper.emitted("completed")).toBeUndefined()
    expect(wrapper.emitted("invalid")).toBeUndefined()
  })

  it("改密会话到期后立即销毁并要求重新登录", async () => {
    vi.useFakeTimers()
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      min_length: 12,
      max_length: 128,
      required_character_classes: 3,
      forbid_username: true,
      description: "12–128 位，至少包含三类字符，不能包含用户名",
    })))
    const { wrapper } = await mountView()

    await vi.advanceTimersByTimeAsync(600_001)

    expect(wrapper.emitted("invalid")?.[0]).toEqual(["改密会话已过期，请重新登录"])
    expect(JSON.stringify(sessionStorage)).not.toContain("change-once")
  })
})
