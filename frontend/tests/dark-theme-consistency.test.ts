import { readdirSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const source = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

describe("深色主题一致性守卫（前端打磨回归）", () => {
  it("深色层收编按卡片定制的浅色表头变量", () => {
    const workspace = source("src/styles/workspace.css")
    const darkLayer = workspace.slice(workspace.indexOf("青鸾 Console 深色监视台"))
    expect(darkLayer).toMatch(
      /\.sign-card \.el-table\s*\{[^}]*--el-table-header-bg-color:\s*var\(--panel-2\)/s,
    )
  })

  it("图表组件使用共享深色调色板，不残留浅色主题色值", () => {
    const chartTheme = source("src/lib/chartTheme.ts")
    expect(chartTheme).toContain('green: "#2fa184"')
    for (const path of ["src/components/ReportTrendChart.vue", "src/components/BalanceChart.vue"]) {
      const component = source(path)
      expect(component).toContain('from "../lib/chartTheme"')
      expect(component).toContain("CHART_TOOLTIP_STYLE")
      for (const lightHex of ["#0e7a63", "#a8650b", "#5b6862", "#d3d8d1", "#e9ece8"]) {
        expect(component).not.toContain(lightHex)
      }
    }
  })

  it("工作区页面不自带 main 包装：骨架只由 App.vue 提供", () => {
    // 登录与首次改密渲染在 public-shell 下，允许自带 <main class="login-screen">。
    const publicViews = new Set(["LoginView.vue", "PasswordChangeView.vue"])
    for (const file of readdirSync(resolve(process.cwd(), "src/views"))) {
      if (publicViews.has(file)) continue
      expect(source(`src/views/${file}`), `${file} 不应嵌套 <main>`).not.toContain("<main")
    }
  })

  it("全部日期选择器声明青鸾深色弹层", () => {
    for (const file of readdirSync(resolve(process.cwd(), "src/views"))) {
      const view = source(`src/views/${file}`)
      const pickers = view.match(/<el-date-picker/g)?.length ?? 0
      const styled = view.match(/popper-class="qingluan-date-popper"/g)?.length ?? 0
      expect(styled, `${file} 存在未声明 qingluan-date-popper 的日期选择器`).toBe(pickers)
    }
  })

  it("登录页成功提示具备样式定义", () => {
    expect(source("src/views/LoginView.vue")).toContain('class="login-success"')
    expect(source("src/styles/theme.css")).toMatch(/\.login-success\s*\{[^}]*color:/s)
  })

  it("路由存在未知路径回退", () => {
    expect(source("src/router/index.ts")).toContain('path: "/:pathMatch(.*)*"')
  })
})
