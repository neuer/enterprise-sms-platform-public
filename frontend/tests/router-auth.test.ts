import { createPinia, setActivePinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"

import router, { installAuthGuard, resolveRouteAccess } from "../src/router"
import { useSessionStore } from "../src/stores/session"

const anonymous = { authenticated: false, role: null } as const
const admin = { authenticated: true, role: "admin" } as const

const sessionUser = {
  account_id: 7,
  identity_id: 11,
  provider_code: "ad",
  username: "admin01",
  display_name: "开发管理员",
  dept: "平台技术部",
  role: "admin",
} as const

function guardedRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", component: { template: "<div />" }, meta: { public: true } },
      { path: "/dashboard", component: { template: "<div />" } },
      { path: "/batches", component: { template: "<div />" } },
    ],
  })
}

describe("认证路由判定", () => {
  it("未登录访问业务页时转到登录", () => {
    expect(resolveRouteAccess({ public: false }, anonymous)).toBe("/login")
  })

  it("已登录访问登录页时转到仪表盘", () => {
    expect(resolveRouteAccess({ public: true }, admin)).toBe("/dashboard")
  })

  it("独立改密 URL 不承载令牌并固定回到登录页", () => {
    const changeRoute = router.getRoutes().find((route) => route.path === "/change-password")
    expect(changeRoute?.redirect).toBe("/login")
  })

  it("角色不匹配时回到首个公共业务页", () => {
    expect(resolveRouteAccess({ public: false, roles: ["admin"] }, { authenticated: true, role: "viewer" })).toBe(
      "/dashboard",
    )
  })

  it("整页刷新后守卫等待 Cookie 恢复完成，深链直达目标页", async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const session = useSessionStore(pinia)
    session.resetIdentity()
    // 模拟 main.ts 启动：恢复请求在途，首跳发生在恢复完成之前。
    const sessionReady = Promise.resolve().then(() => {
      session.apply("mem-token", { ...sessionUser })
    })
    const target = guardedRouter()
    installAuthGuard(target, pinia, sessionReady)

    await target.push("/batches")
    expect(target.currentRoute.value.path).toBe("/batches")
    session.clear()
  })

  it("恢复失败时守卫仍将业务深链转到登录页", async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const session = useSessionStore(pinia)
    session.resetIdentity()
    const sessionReady = Promise.resolve(false)
    const target = guardedRouter()
    installAuthGuard(target, pinia, sessionReady)

    await target.push("/batches")
    expect(target.currentRoute.value.path).toBe("/login")
  })

  it("已恢复会话直开登录页时改跳仪表盘", async () => {
    const pinia = createPinia()
    setActivePinia(pinia)
    const session = useSessionStore(pinia)
    session.resetIdentity()
    const sessionReady = Promise.resolve().then(() => {
      session.apply("mem-token", { ...sessionUser })
    })
    const target = guardedRouter()
    installAuthGuard(target, pinia, sessionReady)

    await target.push("/login")
    expect(target.currentRoute.value.path).toBe("/dashboard")
    session.clear()
  })

  it("路由元数据与后端角色矩阵一致", () => {
    const routes = Object.fromEntries(router.getRoutes().map((route) => [route.path, route.meta]))

    expect(routes["/change-password"].passwordChange).toBeUndefined()
    expect(routes["/send"].roles).toEqual(["operator", "admin"])
    expect(routes["/approvals"].roles).toEqual(["approver", "admin"])
    expect(routes["/templates"].roles).toEqual(["operator", "approver", "admin"])
    expect(routes["/signs"].roles).toEqual(["operator", "approver", "admin"])
    expect(routes["/callbacks"].roles).toEqual(["admin"])
    expect(routes["/ops"].roles).toEqual(["admin"])
    expect(routes["/apps"].roles).toEqual(["admin"])
    expect(routes["/blacklist"].roles).toEqual(["admin"])
    expect(routes["/sensitive-words"].roles).toEqual(["admin"])
  })
})
