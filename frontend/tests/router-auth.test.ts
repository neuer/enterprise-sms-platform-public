import router, { resolveRouteAccess } from "../src/router"

const anonymous = { authenticated: false, role: null } as const
const admin = { authenticated: true, role: "admin" } as const

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
    expect(
      resolveRouteAccess(
        { public: false, roles: ["admin"] },
        { authenticated: true, role: "viewer" },
      ),
    ).toBe("/dashboard")
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
