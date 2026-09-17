import { createPinia, setActivePinia } from "pinia"
import { createMemoryHistory, createRouter } from "vue-router"

import router, { deriveNavigation, installAuthGuard, resolveRouteAccess } from "../src/router"
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

  it("侧栏导航由路由元数据派生且与原手写菜单逐条一致", () => {
    // 菜单/守卫单一事实源回归：派生结果必须与原 App.vue 手写数组完全相同
    expect(deriveNavigation(router.options.routes)).toEqual([
      {
        group: "概览",
        items: [
          { label: "仪表盘", path: "/dashboard", marker: "总", roles: undefined },
          { label: "统计报表", path: "/reports", marker: "析", roles: undefined },
        ],
      },
      {
        group: "发送",
        items: [{ label: "人工发送", path: "/send", marker: "发", roles: ["operator", "admin"] }],
      },
      {
        group: "治理",
        items: [
          { label: "审批中心", path: "/approvals", marker: "审", roles: ["approver", "admin"] },
          { label: "批次列表", path: "/batches", marker: "批", roles: undefined },
          { label: "号码搜索", path: "/messages", marker: "迹", roles: undefined },
          { label: "上行回复", path: "/replies", marker: "回", roles: undefined },
        ],
      },
      {
        group: "管理",
        items: [
          { label: "模板管理", path: "/templates", marker: "模", roles: ["operator", "approver", "admin"] },
          { label: "签名管理", path: "/signs", marker: "签", roles: ["operator", "approver", "admin"] },
          { label: "应用管理", path: "/apps", marker: "应", roles: ["admin"] },
          { label: "黑名单", path: "/blacklist", marker: "黑", roles: ["admin"] },
          { label: "敏感词", path: "/sensitive-words", marker: "敏", roles: ["admin"] },
          { label: "用户与角色", path: "/users", marker: "权", roles: ["admin"] },
          { label: "系统参数", path: "/configs", marker: "参", roles: ["admin"] },
        ],
      },
      {
        group: "运维",
        items: [
          { label: "回调任务", path: "/callbacks", marker: "调", roles: ["admin"] },
          { label: "运维中心", path: "/ops", marker: "运", roles: ["admin"] },
          { label: "安全日报", path: "/security-daily", marker: "安", roles: ["admin"] },
          { label: "审计日志", path: "/audit", marker: "录", roles: ["admin"] },
        ],
      },
    ])
  })
})
