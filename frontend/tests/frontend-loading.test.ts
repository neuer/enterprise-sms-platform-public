import mainSource from "../src/main.ts?raw"
import workspaceElementSource from "../src/element-workspace.ts?raw"
import routerSource from "../src/router/index.ts?raw"
import appSource from "../src/App.vue?raw"
import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

import { readWorkspaceCss } from "./workspace-css"

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

  it("workspace.css 由 App.vue 全局引入单点承载，视图不再重复 import", () => {
    expect(mainSource).toContain('import "./styles/theme.css"')
    expect(mainSource).not.toContain("workspace.css")
    expect(appSource).toContain('import "./styles/workspace.css"')

    for (const view of lazyViews) {
      const source = readFileSync(resolve(process.cwd(), `src/views/${view}.vue`), "utf8")
      expect(source).not.toContain("workspace.css")
    }
  })

  it("workspace.css 拆分为纯 @import 聚合入口，分片齐全且明亮覆写层在末位", () => {
    const entry = readFileSync(resolve(process.cwd(), "src/styles/workspace.css"), "utf8")
    // 除注释与空行外只允许 @import 行（顺序即级联顺序）
    const lines = entry
      .replaceAll(/\/\*[\s\S]*?\*\//g, "")
      .split("\n")
      .map((line) => line.trim())
      .filter(Boolean)
    const imports = lines.filter((line) => line.startsWith("@import"))
    expect(lines.length).toBe(imports.length)
    expect(imports.length).toBeGreaterThan(10)
    for (const line of imports) {
      const match = /^@import "(\.\/workspace\/[^"]+)";$/.exec(line)
      expect(match, `聚合入口只允许引入 styles/workspace/ 分片：${line}`).not.toBeNull()
      expect(existsSync(resolve(process.cwd(), "src/styles", match![1])), `${line} 目标分片缺失`).toBe(true)
    }
    // 明亮模式覆写层必须保持在末位（级联依赖顺序）
    expect(imports.at(-1)).toContain("overrides-light.css")
    // 聚合入口内联展开后仍含壳骨架与覆写层规则（防空切片/漏引入）
    const full = readWorkspaceCss()
    expect(full).toContain(".app-shell")
    expect(full).toContain("明亮模式覆写")
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
    // 工作区组件样式随 element-workspace.ts 在首个非公开路由前一次性加载
    expect(workspaceElementSource).toContain('import "element-plus/theme-chalk/el-date-picker-panel.css"')
    expect(workspaceElementSource).toContain('import "element-plus/theme-chalk/el-time-picker.css"')
  })

  it("Element Plus 组件使用平台统一的简体中文区域配置", () => {
    expect(appSource).toContain('import zhCn from "element-plus/es/locale/lang/zh-cn"')
    expect(appSource).toContain('<el-config-provider :locale="zhCn">')
  })
})
