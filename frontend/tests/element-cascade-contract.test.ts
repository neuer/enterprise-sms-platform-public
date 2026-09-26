import { readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

import { documentTitle } from "../src/router"
import { readWorkspaceCss } from "./workspace-css"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

function listFiles(dir: string, ext: string): string[] {
  const base = resolve(process.cwd(), dir)
  return readdirSync(base).flatMap((entry) => {
    const full = `${dir}/${entry}`
    return statSync(resolve(base, entry)).isDirectory() ? listFiles(full, ext) : entry.endsWith(ext) ? [full] : []
  })
}

/** 展开成「选择器项 → 声明块」对；@media 等外壳被 [^{}] 自然跳过，只留最内层规则。 */
function cssRules(css: string): Array<{ selectors: string[]; body: string }> {
  const stripped = css.replace(/\/\*[\s\S]*?\*\//g, "")
  return [...stripped.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((match) => ({
    selectors: match[1].split(",").map((item) => item.trim()),
    body: match[2],
  }))
}

const workspaceCss = readWorkspaceCss()
const themeCss = read("src/styles/theme.css")
const templates = [...listFiles("src/views", ".vue"), ...listFiles("src/components", ".vue")].map(read).join("\n")

describe("Element 懒加载样式级联契约", () => {
  it("el-select / el-date-picker 上的定宽不写成单类选择器（会被后加载的 Element 默认宽度覆盖）", () => {
    // workspace.css 须排在 element-workspace.ts 的 el-*.css 之前；el-select 的 width:100% 与
    // el-date-editor 的 220px 都在其后加载，同为单类时页面宽度失效，须写成 .el-select.X 等复合形式。
    const classes = new Set(
      [...templates.matchAll(/<el-(?:select|date-picker)\b[^>]*?\sclass="([^"]+)"/g)].flatMap((match) =>
        match[1].split(/\s+/),
      ),
    )
    expect(classes.size).toBeGreaterThan(0)
    const offenders: string[] = []
    for (const rule of cssRules(workspaceCss)) {
      const widths = [
        ...rule.body.matchAll(/(?:^|[;\s])(width|--el-select-width|--el-date-editor-width)\s*:\s*([^;]+)/g),
      ]
      if (!widths.some((declaration) => declaration[2].trim() !== "100%")) continue
      for (const selector of rule.selectors) {
        const single = /^\.([\w-]+)$/.exec(selector)
        if (single && classes.has(single[1])) offenders.push(selector)
      }
    }
    expect(offenders).toEqual([])
  })

  it("深色重混的 tag / alert / message / 分页 / 描边主按钮覆写带 :root 前缀，压过后加载的组件样式", () => {
    const selectors = cssRules(themeCss).flatMap((rule) => rule.selectors)
    const unprefixed = selectors.filter((selector) =>
      /^\.el-(?:tag|alert|message)\b|^\.el-pagination$|^\.el-button--primary\.is-plain/.test(selector),
    )
    expect(unprefixed).toEqual([])
    for (const required of [
      ":root .el-tag.el-tag--success",
      ":root .el-tag.el-tag--danger",
      ":root .el-tag.el-tag--dark.el-tag--danger",
      ":root .el-alert--error",
      ":root .el-message--success",
      ":root .el-pagination",
    ]) {
      expect(selectors).toContain(required)
    }
  })

  it("语义色派生阶按面板色重混，toast 与标签不再使用 Element 预混的近白底", () => {
    for (const type of ["success", "warning", "danger", "error", "info"]) {
      for (const step of ["light-3", "light-5", "light-7", "light-8", "light-9", "dark-2"]) {
        expect(themeCss).toContain(`--el-color-${type}-${step}:`)
      }
    }
    expect(themeCss).toMatch(/--el-color-error:\s*var\(--verm\)/)
  })

  it("文字色不直接使用品牌填充绿 --verdi（深色面板上仅约 3:1），改用 --verdi-text", () => {
    const sources = [...listFiles("src/styles", ".css").map(read), templates].join("\n")
    expect(sources).not.toMatch(/(?:^|[;{\s])color:\s*var\(--verdi\)/m)
  })
})

describe("浏览器标签页标题", () => {
  it("页面名在前、站点名在后；无标题时回落到站点名", () => {
    expect(documentTitle("批次列表")).toBe("批次列表 · 青鸾 · 企业短信管理平台")
    expect(documentTitle(undefined)).toBe("青鸾 · 企业短信管理平台")
    expect(documentTitle("")).toBe("青鸾 · 企业短信管理平台")
  })
})
