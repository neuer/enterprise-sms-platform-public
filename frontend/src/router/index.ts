import { createRouter, createWebHistory } from "vue-router"
import type { Pinia } from "pinia"
import type { Router } from "vue-router"

import type { UserRole } from "../api/auth"
import { useSessionStore } from "../stores/session"
import LoginView from "../views/LoginView.vue"

interface RouteAccess {
  public?: boolean
  roles?: UserRole[]
}

interface SessionAccess {
  authenticated: boolean
  role: UserRole | null
}

export function resolveRouteAccess(route: RouteAccess, session: SessionAccess): string | undefined {
  if (route.public) return session.authenticated ? "/dashboard" : undefined
  if (!session.authenticated) return "/login"
  if (route.roles?.length && (!session.role || !route.roles.includes(session.role))) {
    return "/dashboard"
  }
  return undefined
}

export function installAuthGuard(target: Router, pinia: Pinia): void {
  target.beforeEach((to) => {
    const session = useSessionStore(pinia)
    return resolveRouteAccess(
      {
        public: Boolean(to.meta.public),
        roles: Array.isArray(to.meta.roles) ? (to.meta.roles as UserRole[]) : undefined,
      },
      {
        authenticated: session.isAuthenticated,
        role: session.role,
      },
    )
  })
}

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: "/login",
      name: "login",
      component: LoginView,
      meta: { title: "登录", public: true },
    },
    {
      path: "/change-password",
      redirect: "/login",
    },
    { path: "/", redirect: "/dashboard" },
    {
      path: "/dashboard",
      name: "dashboard",
      component: () => import("../views/DashboardView.vue"),
      meta: { title: "仪表盘" },
    },
    {
      path: "/reports",
      name: "reports",
      component: () => import("../views/ReportView.vue"),
      meta: { title: "统计报表", group: "概览" },
    },
    {
      path: "/users",
      name: "users",
      component: () => import("../views/UserView.vue"),
      meta: { title: "用户与角色", group: "管理", roles: ["admin"] },
    },
    {
      path: "/configs",
      name: "configs",
      component: () => import("../views/ConfigView.vue"),
      meta: { title: "系统参数", group: "管理", roles: ["admin"] },
    },
    {
      path: "/audit",
      name: "audit",
      component: () => import("../views/AuditView.vue"),
      meta: { title: "审计日志", group: "运维", roles: ["admin"] },
    },
    {
      path: "/send",
      name: "send",
      component: () => import("../views/SendView.vue"),
      meta: { title: "人工发送", group: "发送", roles: ["operator", "admin"] },
    },
    {
      path: "/approvals",
      name: "approvals",
      component: () => import("../views/ApprovalView.vue"),
      meta: { title: "审批中心", group: "治理", roles: ["approver", "admin"] },
    },
    {
      path: "/replies",
      name: "replies",
      component: () => import("../views/ReplyView.vue"),
      meta: { title: "上行回复", group: "治理" },
    },
    {
      path: "/batches",
      name: "batches",
      component: () => import("../views/BatchView.vue"),
      meta: { title: "批次列表", group: "治理" },
    },
    {
      path: "/messages",
      name: "messages",
      component: () => import("../views/MessageView.vue"),
      meta: { title: "号码搜索", group: "治理" },
    },
    {
      path: "/callbacks",
      name: "callbacks",
      component: () => import("../views/CallbackView.vue"),
      meta: { title: "回调任务", group: "运维", roles: ["admin"] },
    },
    {
      path: "/ops",
      name: "ops",
      component: () => import("../views/OpsView.vue"),
      meta: { title: "运维中心", group: "运维", roles: ["admin"] },
    },
    {
      path: "/security-daily",
      name: "security-daily",
      component: () => import("../views/SecurityDailyView.vue"),
      meta: { title: "安全日报", group: "运维", roles: ["admin"] },
    },
    {
      path: "/templates",
      name: "templates",
      component: () => import("../views/TemplateView.vue"),
      meta: { title: "模板管理", group: "管理", roles: ["operator", "approver", "admin"] },
    },
    {
      path: "/signs",
      name: "signs",
      component: () => import("../views/SignView.vue"),
      meta: { title: "签名管理", group: "管理", roles: ["operator", "approver", "admin"] },
    },
    {
      path: "/apps",
      name: "apps",
      component: () => import("../views/AppManagementView.vue"),
      meta: { title: "应用管理", group: "管理", roles: ["admin"] },
    },
    {
      path: "/blacklist",
      name: "blacklist",
      component: () => import("../views/BlacklistView.vue"),
      meta: { title: "黑名单", group: "管理", roles: ["admin"] },
    },
    {
      path: "/sensitive-words",
      name: "sensitive-words",
      component: () => import("../views/SensitiveWordView.vue"),
      meta: { title: "敏感词", group: "管理", roles: ["admin"] },
    },
  ],
})

export default router
