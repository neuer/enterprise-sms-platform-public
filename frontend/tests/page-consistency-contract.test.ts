import { readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

import { readWorkspaceCss } from "./workspace-css"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

function listFiles(dir: string, ext: string): string[] {
  const base = resolve(process.cwd(), dir)
  return readdirSync(base).flatMap((entry) => {
    const full = `${dir}/${entry}`
    return statSync(resolve(base, entry)).isDirectory() ? listFiles(full, ext) : entry.endsWith(ext) ? [full] : []
  })
}

const views = listFiles("src/views", ".vue").map((path) => ({ path, source: read(path) }))

describe("跨页面一致性契约", () => {
  it("每张 el-table 都有两行空态：自带 #empty 插槽，或只在有数据/加载中时渲染", () => {
    const offenders: string[] = []
    for (const { path, source } of views) {
      for (const match of source.matchAll(/<el-table\b([^>]*)>([\s\S]*?)<\/el-table>/g)) {
        const before = source.slice(Math.max(0, (match.index ?? 0) - 240), match.index)
        const gated = /v-if="[^"]*(?:\.length|loading)[^"]*"[^<]*>\s*$/.test(before) || /\sv-if="/.test(match[1])
        if (!match[2].includes("#empty") && !gated) offenders.push(path)
      }
    }
    expect(offenders).toEqual([])
  })

  it("权限提示不直接展示角色代码，统一经 labels.ts 的 roleNames 输出中文角色名", () => {
    const offenders = views.filter(({ source }) =>
      /[读写]：[^<{]*\b(?:admin|operator|approver|viewer)\b/.test(source.slice(source.indexOf("\n<template>"))),
    )
    expect(offenders.map(({ path }) => path)).toEqual([])
  })

  it("页头操作按钮只用默认或实心主按钮，不混用 plain 描边", () => {
    const offenders: string[] = []
    for (const { path, source } of views) {
      const heading = /<(header|section|div)[^>]*class="[^"]*page-heading[^"]*"[\s\S]*?<\/\1>/.exec(source)
      if (!heading) continue
      for (const button of heading[0].matchAll(/<el-button\b([^>]*)>/g)) {
        if (/\splain\b/.test(button[1])) offenders.push(path)
      }
    }
    expect(offenders).toEqual([])
  })

  it("筛选条时间范围共用同一宽度，字号由 workspace/element.css 日期选择器单点承载", () => {
    const workspace = readWorkspaceCss().replace(/\/\*[\s\S]*?\*\//g, "")
    expect(workspace).toMatch(
      /\.batch-filter-dates,\s*\.message-filter-dates,\s*\.reply-filter-dates,\s*\.ops-dates,\s*\.audit-dates\s*\{\s*--el-date-editor-datetimerange-width:\s*272px;/,
    )
    expect(workspace).not.toMatch(/-dates\s+\.el-range-(?:input|separator)\s*\{[^}]*font-size/)
    for (const path of [
      "src/views/AuditView.vue",
      "src/views/ops/OpsUnmatchedTab.vue",
      "src/views/ops/OpsAlertsTab.vue",
    ]) {
      expect(read(path)).toContain('format="YYYY-MM-DD HH:mm"')
    }
  })

  it("面板标题行只给标题块设 grid，不误伤同行的 FilterSeg", () => {
    const workspace = readWorkspaceCss()
    expect(workspace).not.toMatch(/\.panel-title div\s*\{/)
    expect(workspace).toContain(".panel-title > div:not(.filter-seg)")
  })

  it("表格行悬停统一走不透明的 --row-hover 令牌，固定列不透出横向滚动内容", () => {
    const theme = read("src/styles/theme.css")
    expect(theme).toMatch(/--row-hover:\s*color-mix\(in srgb, var\(--panel\) \d+%, var\(--tx-hi\)\);/)
    const workspace = readWorkspaceCss().replace(/\/\*[\s\S]*?\*\//g, "")
    const hoverValues = [
      ...[...workspace.matchAll(/--el-table-row-hover-bg-color:\s*([^;]+);/g)].map((m) => m[1]),
      ...[...workspace.matchAll(/tr:hover\s*\{[^}]*background:\s*([^;]+);/g)].map((m) => m[1]),
    ]
    expect(hoverValues.length).toBeGreaterThan(0)
    expect(hoverValues.filter((value) => value.trim() !== "var(--row-hover)")).toEqual([])
  })

  it("el-table 加载态用 v-loading 指令，el-table 没有 loading prop", () => {
    const offenders = views.filter(({ source }) => /<el-table\b[^>]*\s:loading=/.test(source))
    expect(offenders.map(({ path }) => path)).toEqual([])
  })

  it("窄屏下状态列固定在右侧，不被横向滚动挤出视口", () => {
    for (const path of ["src/views/BatchView.vue", "src/views/AppManagementView.vue"]) {
      expect(read(path)).toMatch(/<el-table-column label="状态" width="\d+" fixed="right">/)
    }
  })

  it("el-select 占位色接入主题令牌，不沿用 Element 默认浅灰", () => {
    expect(read("src/styles/theme.css")).toContain("--el-text-color-placeholder: var(--tx-3);")
  })

  it("无可见标签的筛选控件带 aria-label，装饰性令牌格对读屏隐藏", () => {
    expect(read("src/views/ReportView.vue")).toContain('class="report-pill-select" aria-label="类别"')
    expect(read("src/views/SendView.vue")).toContain('aria-label="签名"')
    expect(read("src/views/CallbackView.vue")).toContain('aria-label="应用"')
    expect(read("src/views/AuditView.vue")).toContain('aria-label="动作"')
    const config = read("src/views/ConfigView.vue")
    expect(config.match(/:aria-label="item\.key"/g)?.length).toBe(3)
    expect(read("src/components/ChannelMonitor.vue")).toContain('<div class="token-grid" aria-hidden="true">')
  })

  it("配置保存条的计数文字样式只作用于直接子 span，不染到主按钮内部文字", () => {
    const workspace = readWorkspaceCss()
    expect(workspace).toContain(".config-savebar > span {")
    expect(workspace).not.toMatch(/\.config-savebar span\b/)
  })

  it("计数千分位只经 lib/format.ts 的 formatNumber，不随浏览器语言变化", () => {
    const offenders = [...listFiles("src", ".vue"), ...listFiles("src", ".ts")].filter(
      (path) => !path.endsWith("lib/format.ts") && /\.toLocaleString\(/.test(read(path)),
    )
    expect(offenders).toEqual([])
  })
})
