import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")
const css = read("src/styles/workspace.css")
const standardQueryViews = ["BatchView.vue", "MessageView.vue", "ReplyView.vue"].map((name) =>
  read(`src/views/${name}`),
)
const auditView = read("src/views/AuditView.vue")
const userView = read("src/views/UserView.vue")
const reportView = read("src/views/ReportView.vue")
const blacklistView = read("src/views/BlacklistView.vue")
const sensitiveWordView = read("src/views/SensitiveWordView.vue")
const compactViews = ["CallbackView.vue", "ApprovalView.vue", "TemplateView.vue", "OpsView.vue"].map(
  (name) => read(`src/views/${name}`),
)

describe("全站筛选布局契约", () => {
  it("使用共享十二列网格与语义跨度", () => {
    expect(css).toMatch(/\.filter-grid\s*\{[^}]*display:\s*grid[^}]*repeat\(12,/s)
    for (const span of [2, 3, 4, 6]) {
      expect(css).toContain(`.filter-span-${span}`)
    }
    expect(css).toMatch(/\.filter-actions[^}]*align-self:\s*end/s)
  })

  it("中等宽度重排且手机端单列", () => {
    expect(css).toMatch(/@media \(max-width: 1100px\)[\s\S]*\.filter-grid/s)
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*\.filter-grid[^}]*grid-template-columns:\s*1fr/s,
    )
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*\.filter-grid \.el-form-item[^}]*min-width:\s*0/s,
    )
  })

  it("标准查询表单不再启用 Element Plus inline 布局", () => {
    for (const source of standardQueryViews) {
      expect(source).toContain("filter-grid")
      expect(source).not.toMatch(/<el-form[^>]*\binline\b/)
    }
  })

  it("管理与报表筛选遵循同一表单语义", () => {
    for (const source of [auditView, userView, reportView]) {
      expect(source).toContain("filter-grid")
    }
    expect(auditView).not.toMatch(/<el-form[^>]*\binline\b/)
    expect(userView).not.toMatch(/<el-form[^>]*\binline\b/)
    expect(reportView).toContain("<el-segmented")
  })

  it("轻量筛选统一使用紧凑工具栏", () => {
    for (const source of compactViews) expect(source).toContain("filter-toolbar")
    expect(css).toMatch(/\.filter-toolbar\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap/s)
  })

  it("治理录入表单使用统一操作区并在手机端提供满宽主按钮", () => {
    for (const source of [blacklistView, sensitiveWordView]) {
      expect(source).toContain("governance-entry-form")
      expect(source).toContain("governance-entry-actions")
    }
    expect(blacklistView).toContain("blacklist-entry-footer")
    expect(css).toMatch(/\.governance-entry-actions\s*\{[^}]*justify-content:\s*flex-end/s)
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*\.blacklist-entry-footer[^}]*grid-template-columns:\s*1fr/s,
    )
    expect(css).toMatch(
      /@media \(max-width: 760px\)[\s\S]*\.governance-entry-actions \.el-button[^}]*width:\s*100%/s,
    )
  })

  it("不再保留会覆盖共享栅格的旧筛选补丁", () => {
    expect(css).not.toContain("margin-bottom: -18px")
    expect(css).not.toMatch(
      /\.query-filter,\s*\.message-search,\s*\.audit-filter-card \.el-form\s*\{/s,
    )
  })
})
