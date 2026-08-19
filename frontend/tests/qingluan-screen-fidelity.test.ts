import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const source = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

describe("青鸾 Console 17 屏结构保真", () => {
  it("路由覆盖交接包的 17 个业务屏", () => {
    const router = source("src/router/index.ts")
    const paths = [
      "/dashboard", "/send", "/batches", "/messages", "/approvals", "/ops",
      "/templates", "/signs", "/replies", "/apps", "/blacklist", "/sensitive-words",
      "/users", "/configs", "/reports", "/audit", "/login",
    ]
    for (const path of paths) expect(router).toContain(`path: "${path}"`)
  })

  it("人工发送使用三类卡并根据预检切换主动作", () => {
    const view = source("src/views/SendView.vue")
    expect(view).toContain('data-testid="category-verify"')
    expect(view).toMatch(/data-testid="category-verify"[\s\S]*?disabled/)
    expect(view).toContain("const submitLabel = computed")
    expect(view).toContain("提交审批")
    expect(view).toContain("立即发送")
    expect(view).toContain("send-preview precheck send-rail")
  })

  it("批次详情使用 560px 抽屉和状态统计环", () => {
    const view = source("src/views/BatchView.vue")
    expect(view).toContain('size="min(560px, 92vw)"')
    expect(view).toContain('class="batch-donut"')
    expect(view).toContain("送达 / 失败 / 未知")
  })

  it("审批中心使用单一列表组件并按需解密正文", () => {
    const view = source("src/views/ApprovalView.vue")
    const list = source("src/components/ApprovalList.vue")
    expect(view).toContain("ApprovalList")
    expect(view).toContain("getApproval")
    expect(view).toContain("按需解密 · 本次查看已写敏感读审计")
    expect(view).toContain("approval-decide-box")
    expect(list).toContain('class="approval-rows"')
    expect(list).toContain("approval-cd")
    expect(list).toContain("本人提交 · 按规则回避")
    expect(list).toContain("approval-dest")
    expect(view).not.toContain("approval-mobile-list")
    expect(view).not.toContain("approval-queue")
  })

  it("运维中心在标签页上方呈现熔断恢复横幅", () => {
    const view = source("src/views/OpsView.vue")
    expect(view).toContain("circuit-banner")
    expect(view).toContain("实时队列已恢复")
    expect(view).toContain("禁止自动重发")
  })

  it("应用与密钥使用三列卡片而非主表格", () => {
    const view = source("src/views/AppManagementView.vue")
    expect(view).toContain('class="app-card-grid"')
    expect(view).toContain("managed-app-card")
    expect(view).toContain("keyGraceHours")
  })
})
