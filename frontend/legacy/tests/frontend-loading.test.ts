import mainSource from "../src/main.ts?raw"
import routerSource from "../src/router/index.ts?raw"
import appSource from "../src/App.vue?raw"
import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const lazyViews = [
  "DashboardView", "ReportView", "UserView", "ConfigView", "AuditView", "SendView",
  "ApprovalView", "ReplyView", "BatchView", "MessageView", "CallbackView", "OpsView",
  "TemplateView", "SignView", "AppManagementView", "BlacklistView", "SensitiveWordView",
]

describe("前端加载边界", () => {
  it("业务页面全部通过路由懒加载", () => {
    for (const view of ["ApprovalView", "SendView", "TemplateView", "SignView", "ReplyView", "CallbackView", "BatchView", "MessageView"]) {
      expect(routerSource).not.toContain(`import ${view} from`)
      expect(routerSource).toContain(`import(\"../views/${view}.vue\")`)
    }
  })

  it("入口仅加载基础主题且业务样式跟随懒加载页面", () => {
    expect(mainSource).toContain('import "./styles/theme.css"')
    expect(mainSource).not.toContain("workspace.css")

    for (const view of lazyViews) {
      const source = readFileSync(resolve(process.cwd(), `src/views/${view}.vue`), "utf8")
      expect(source).toContain('import "../styles/workspace.css"')
    }
  })

  it("基础主题不再携带路由页面的大段样式", () => {
    const theme = readFileSync(resolve(process.cwd(), "src/styles/theme.css"), "utf8")
    for (const selector of [".dashboard-metrics", ".send-workbench", ".ops-panel", ".query-table-card"]) {
      expect(theme).not.toContain(selector)
    }
  })

  it("不再整库注册 Element Plus", () => {
    expect(mainSource).not.toContain('import ElementPlus from "element-plus"')
    expect(mainSource).not.toContain(".use(ElementPlus)")
    expect(mainSource).not.toContain('import "element-plus/dist/index.css"')
  })

  it("日期选择器按需加载完整的面板和时间结构样式", () => {
    expect(mainSource).toContain('import "element-plus/theme-chalk/el-date-picker-panel.css"')
    expect(mainSource).toContain('import "element-plus/theme-chalk/el-time-picker.css"')
  })

  it("Element Plus 组件使用平台统一的简体中文区域配置", () => {
    expect(appSource).toContain('import zhCn from "element-plus/es/locale/lang/zh-cn"')
    expect(appSource).toContain('<el-config-provider :locale="zhCn">')
  })
})
