import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")
const css = read("src/styles/workspace.css")
const messageView = read("src/views/MessageView.vue")
const replyView = read("src/views/ReplyView.vue")
const auditView = read("src/views/AuditView.vue")
const userView = read("src/views/UserView.vue")
const reportView = read("src/views/ReportView.vue")
const blacklistView = read("src/views/BlacklistView.vue")
const sensitiveWordView = read("src/views/SensitiveWordView.vue")
const compactViews = ["CallbackView.vue", "TemplateView.vue", "OpsView.vue"].map(
  (name) => read(`src/views/${name}`),
)
const approvalView = read("src/views/ApprovalView.vue")
const batchView = read("src/views/BatchView.vue")
const signView = read("src/views/SignView.vue")

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

  it("上行回复使用方案 A 单行检索条", () => {
    expect(replyView).toContain("reply-filter-bar")
    expect(replyView).toContain("reply-seg")
    expect(replyView).toContain("共 {{ total }} 条 · 每页 20")
    expect(replyView).toContain("未匹配到平台批次")
    expect(replyView).not.toContain("filter-grid")
    expect(replyView).not.toContain("filter-toolbar")
    expect(replyView).not.toContain("<el-segmented")
    expect(replyView).not.toContain("↗")
    expect(replyView).not.toContain("厂商未回传 customId")
    expect(css).toMatch(/\.reply-filter-bar\s*\{[^}]*display:\s*flex/s)
    expect(css).toMatch(/\.reply-seg\s*\{[^}]*border-radius:\s*7px/s)
  })

  it("管理筛选遵循同一表单语义", () => {
    for (const source of [auditView, userView]) {
      expect(source).toContain("filter-grid")
    }
    expect(auditView).not.toMatch(/<el-form[^>]*\binline\b/)
    expect(userView).not.toMatch(/<el-form[^>]*\binline\b/)
  })

  it("号码搜索使用方案 A 单行检索条", () => {
    expect(messageView).toContain("message-filter-bar")
    expect(messageView).toContain("message-seg")
    expect(messageView).toContain("message-badge")
    expect(messageView).toContain("共 {{ total }} 条 · 每页 20")
    expect(messageView).not.toContain("filter-grid")
    expect(messageView).not.toContain("filter-toolbar")
    expect(messageView).not.toContain("<el-segmented")
    expect(messageView).not.toContain("view-switch")
    expect(css).toMatch(/\.message-filter-bar\s*\{[^}]*display:\s*flex/s)
    expect(css).toMatch(/\.message-seg\s*\{[^}]*border-radius:\s*7px/s)
  })

  it("统计报表使用方案 A 单行工具条", () => {
    expect(reportView).toContain("report-filter-bar")
    expect(reportView).toContain("含明文手机号")
    expect(reportView).not.toContain("filter-grid")
    expect(reportView).not.toContain("<el-segmented")
    expect(css).toMatch(/\.report-filter-bar\s*\{[^}]*display:\s*flex/s)
  })

  it("轻量筛选统一使用紧凑工具栏", () => {
    for (const source of compactViews) expect(source).toContain("filter-toolbar")
    expect(css).toMatch(/\.filter-toolbar\s*\{[^}]*display:\s*flex[^}]*flex-wrap:\s*wrap/s)
  })

  it("批次列表使用方案 A 单行胶囊筛选条", () => {
    expect(batchView).toContain("batch-filter-bar")
    expect(batchView).toContain("batch-seg")
    expect(batchView).toContain("更多筛选")
    expect(batchView).not.toContain("filter-toolbar")
    expect(batchView).not.toContain("filter-grid")
    expect(batchView).not.toContain("<el-segmented")
    expect(batchView).not.toContain("query-total")
    expect(batchView).toContain("batch-scope")
    expect(batchView).toContain("batch-facts")
    expect(batchView).toContain("共 {{ total }} 个批次 · 每页 20")
    expect(css).toMatch(/\.batch-filter-bar\s*\{[^}]*display:\s*flex/s)
    expect(css).toMatch(/\.batch-seg\s*\{[^}]*border-radius:\s*7px/s)
    expect(css).toMatch(/\.compose\s*\{[^}]*height:\s*5px/s)
  })

  it("审批中心使用方案 A 单行胶囊筛选条", () => {
    expect(approvalView).toContain("approval-filter-bar")
    expect(approvalView).toContain('data-testid="approval-status-seg"')
    expect(approvalView).not.toContain("filter-toolbar")
    expect(approvalView).not.toContain("filter-grid")
    expect(approvalView).not.toContain("<el-segmented")
    expect(css).toMatch(/\.approval-filter-bar\s*\{[^}]*display:\s*flex/s)
    expect(css).toMatch(/\.approval-seg\s*\{[^}]*border-radius:\s*7px/s)
  })

  it("签名管理使用方案 A 单行胶囊工具条", () => {
    expect(signView).toContain("sign-filter-bar")
    expect(signView).toContain("sign-seg")
    expect(signView).toContain("接口全量返回 · 前端过滤")
    expect(signView).toContain("共 {{ filtered.length }} 个签名")
    expect(signView).toContain("读：operator / approver / admin · 写：admin")
    expect(signView).toContain("不可变更")
    expect(signView).not.toContain("filter-toolbar")
    expect(signView).not.toContain("filter-grid")
    expect(signView).not.toContain("<el-segmented")
    expect(signView).not.toContain("<el-card")
    expect(css).toMatch(/\.sign-filter-bar\s*\{[^}]*display:\s*flex/s)
    expect(css).toMatch(/\.sign-seg\s*\{[^}]*border-radius:\s*7px/s)
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
