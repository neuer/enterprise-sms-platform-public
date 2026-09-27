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

  it("筛选条时间范围共用同一宽度，字号由 theme.css 日期选择器单点承载", () => {
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
})
