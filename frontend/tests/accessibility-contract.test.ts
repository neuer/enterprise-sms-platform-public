import { existsSync, readFileSync } from "node:fs"
import { resolve } from "node:path"

const themePath = resolve(process.cwd(), "src/styles/theme.css")
const workspacePath = resolve(process.cwd(), "src/styles/workspace.css")
const css = [
  readFileSync(themePath, "utf8"),
  existsSync(workspacePath) ? readFileSync(workspacePath, "utf8") : "",
].join("\n")
const datePickerViews = [
  "BatchView.vue",
  "ReportView.vue",
  "AuditView.vue",
  "SendView.vue",
  "ReplyView.vue",
  "MessageView.vue",
].map((name) => readFileSync(resolve(process.cwd(), "src/views", name), "utf8"))

function channel(value: number): number {
  const normalized = value / 255
  return normalized <= 0.04045
    ? normalized / 12.92
    : ((normalized + 0.055) / 1.055) ** 2.4
}

function luminance(color: string): number {
  const [red, green, blue] = color
    .slice(1)
    .match(/.{2}/g)!
    .map((value) => channel(Number.parseInt(value, 16)))
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

function contrast(foreground: string, background: string): number {
  const lighter = Math.max(luminance(foreground), luminance(background))
  const darker = Math.min(luminance(foreground), luminance(background))
  return (lighter + 0.05) / (darker + 0.05)
}

describe("无障碍样式契约", () => {
  it("所有全局 CSS 变量都有定义", () => {
    const definitions = new Set(
      [...css.matchAll(/(--[a-z0-9-]+)\s*:/gi)].map((match) => match[1]),
    )
    const usages = new Set(
      [...css.matchAll(/var\((--[a-z0-9-]+)/gi)].map((match) => match[1]),
    )

    expect([...usages].filter((name) => name !== "--load" && !definitions.has(name))).toEqual([])
  })

  it("类别三色遵循 verify、notice、market 的固定映射", () => {
    expect(css).toContain("--slate: #4574a3")
    expect(css).toContain("--verm: #e46a4f")
    expect(css).toMatch(/\.category-strip \.verify\s*\{\s*background:\s*var\(--verdi\)/)
    expect(css).toMatch(/\.category-strip \.notice\s*\{\s*background:\s*var\(--slate\)/)
    expect(css).toMatch(/\.category-strip \.market\s*\{\s*background:\s*var\(--amber\)/)
  })

  it("辅助说明在页面、卡片和下沉背景上均满足普通文本对比度", () => {
    expect(css).toContain("--tx-2: #8b978f")
    for (const background of ["#101814", "#18231e", "#141d19", "#0c1512"]) {
      expect(contrast("#8b978f", background)).toBeGreaterThanOrEqual(4.5)
    }
  })

  it("深色控制台的小号正文与文字操作不直接使用低对比度填充色", () => {
    expect(css).toMatch(/\.nav-section h2[^}]*color:\s*var\(--tx-2\)/s)
    expect(css).toMatch(/\.workspace :is\(small, time, dt\)[^}]*color:\s*var\(--tx-2\)/s)
    expect(css).toMatch(/\.el-table th\.el-table__cell[^}]*color:\s*var\(--tx-2\)/s)
    expect(css).toMatch(/\.el-button\.is-link\.el-button--primary[^}]*--el-button-text-color:\s*var\(--verdi-l\)/s)
    expect(css).toMatch(/\.el-tabs__item\.is-active[^}]*color:\s*var\(--verdi-l\)/s)
    expect(css).toMatch(/\.metric-card span,[\s\S]*?\.metric-card small[^}]*color:\s*var\(--tx-2\)/s)
    expect(css).toMatch(/\.apps-cell-sub[^}]*color:\s*var\(--tx-2\)/s)
    expect(css).toMatch(/\.empty-state p[^}]*color:\s*var\(--tx-2\)/s)
    for (const background of ["#101814", "#18231e", "#141d19"]) {
      expect(contrast("#2fa184", background)).toBeGreaterThanOrEqual(4.5)
    }
  })

  it("在粗指针或移动视口扩大主要交互控件到至少 44px", () => {
    expect(css).toContain("@media (pointer: coarse), (max-width: 760px)")
    expect(css).toContain("min-height: 44px")
    expect(css).toContain(".nav-link")
    expect(css).toContain(".el-pagination button")
    expect(css).toMatch(/\.vendor-test-actions \.el-button[^}]*min-height:\s*44px/s)
  })

  it("真实联调控制台在窄屏退化为单列且保留全部安全操作", () => {
    expect(css).toMatch(/@media \(max-width: 760px\)[\s\S]*\.vendor-test-layout[^}]*grid-template-columns:\s*1fr/s)
    expect(css).toMatch(/@media \(max-width: 760px\)[\s\S]*\.vendor-test-actions[^}]*grid-template-columns:\s*1fr/s)
  })

  it("尊重用户的 reduced-motion 偏好", () => {
    expect(css).toContain("@media (prefers-reduced-motion: reduce)")
  })

  it("移动筛选器和日期范围可收缩且仅运维标签保留横向滚动", () => {
    expect(css).toMatch(/\.filter-grid[^}]*min-width:\s*0/s)
    expect(css).toMatch(/\.filter-grid \.el-date-editor[^}]*width:\s*100%[^}]*min-width:\s*0/s)
    expect(css).not.toMatch(/\.(?:query-filter-card|message-search-card|audit-filter-card|reply-filter-card)[^{]*\{[^}]*overflow-x:\s*auto/s)
    expect(css).toContain(".ops-tabs .el-tabs__nav-wrap { overflow-x: auto; }")
  })

  it("移动治理列表在加载期间保留可见高度", () => {
    expect(css).toMatch(
      /\.approval-rows,[\s\S]*\.sign-mobile-list,[\s\S]*\.blacklist-mobile-list\s*\{[^}]*min-height:\s*120px/s,
    )
  })

  it("审批列表为单一标记，断点只折叠布局不切换 DOM", () => {
    const view = readFileSync(resolve(process.cwd(), "src/views/ApprovalView.vue"), "utf8")
    const list = readFileSync(resolve(process.cwd(), "src/components/ApprovalList.vue"), "utf8")
    expect(view).not.toContain("approval-mobile-list")
    expect(list).not.toContain("approval-mobile-list")
    expect(css).toMatch(/@media \(max-width: 959px\)[\s\S]*\.approval-table thead\s*\{[^}]*display:\s*none/s)
  })

  it("窄屏长文本、浮层和分页不会撑破视口", () => {
    expect(css).toMatch(/\.el-table \.cell,[^{]*\{[^}]*overflow-wrap:\s*anywhere/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.el-drawer[^}]*width:\s*100%\s*!important/)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.el-pagination[^}]*flex-wrap:\s*wrap/)
  })

  it("移动端复选框长文案允许收缩换行", () => {
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.el-checkbox\s*\{[^}]*height:\s*auto[^}]*align-items:\s*flex-start/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.el-checkbox__label\s*\{[^}]*min-width:\s*0[^}]*white-space:\s*normal/s)
  })

  it("移动导航关闭时退出无障碍树和键盘焦点序列", () => {
    expect(css).toMatch(/@media \(max-width: 959px\)[\s\S]*\.sidebar\s*\{[^}]*visibility:\s*hidden/s)
    expect(css).toMatch(/\.navigation-open \.sidebar\s*\{[^}]*visibility:\s*visible/s)
    expect(css).toMatch(/\.navigation-backdrop\s*\{[^}]*visibility:\s*hidden/s)
    expect(css).toMatch(/\.navigation-open \.navigation-backdrop\s*\{[^}]*visibility:\s*visible/s)
  })

  it("所有日期选择器都挂载统一弹层主题", () => {
    const pickerCount = datePickerViews.reduce(
      (total, source) => total + (source.match(/<el-date-picker\b/g)?.length ?? 0),
      0,
    )
    const themedPickerCount = datePickerViews.reduce(
      (total, source) => total + (source.match(/popper-class="qingluan-date-popper"/g)?.length ?? 0),
      0,
    )

    expect(pickerCount).toBe(7)
    expect(themedPickerCount).toBe(pickerCount)
  })

  it("日期选择器使用统一的输入、范围和选中态", () => {
    expect(css).toContain(".el-picker__popper.qingluan-date-popper")
    expect(css).toMatch(/\.el-date-editor[^}]*box-shadow:\s*0 0 0 1px var\(--line-2\) inset/s)
    expect(css).toMatch(/\.el-date-table td\.in-range[^}]*background-color:\s*color-mix\(in srgb, var\(--verdi\) 10%, var\(--card\)\)/s)
    expect(css).toMatch(/\.el-date-table td\.current:not\(\.disabled\) \.el-date-table-cell__text[^}]*background:\s*var\(--verdi\)/s)
  })

  it("日期选择弹层在窄屏内纵向排布", () => {
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper\.el-picker__popper[^}]*position:\s*fixed\s*!important[^}]*inset:\s*8px 8px auto\s*!important/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper \.el-date-range-picker__content[^}]*display:\s*block[^}]*width:\s*100%/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper \.el-date-range-picker__time-header[^}]*display:\s*grid/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper \.el-date-range-picker__editors-wrap[^}]*display:\s*grid/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper \.el-picker-panel__body[^}]*width:\s*100%[^}]*min-width:\s*0/s)
    expect(css).toMatch(/@media \(max-width: 640px\)[\s\S]*\.qingluan-date-popper \.el-picker-panel__footer[^}]*position:\s*sticky[^}]*bottom:\s*0/s)
  })
})
