import { readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

import { documentTitle } from "../src/router"

import { readThemeCss } from "./workspace-css"

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

const themeCss = readThemeCss()
const templates = [...listFiles("src/views", ".vue"), ...listFiles("src/components", ".vue")].map(read).join("\n")

describe("Element 懒加载样式级联契约", () => {
  it("Element 样式只经 layer(element) 聚合入口导入，层顺序先于一切样式声明", () => {
    const main = read("src/main.ts")
    expect([...main.matchAll(/^import "([^"]+\.css)"$/gm)].map((match) => match[1])).toEqual([
      "./styles/layers.css",
      "./styles/fonts-sans.css",
      "./styles/element-entry.css",
      "./styles/theme.css",
    ])
    for (const path of [...listFiles("src", ".ts"), ...listFiles("src", ".vue")]) {
      expect(read(path), `${path} 直接导入 Element 样式会落在层外、压过全部覆写`).not.toContain("theme-chalk/")
    }
    const lazy = [...read("src/styles/element-workspace.css").matchAll(/^@import "([^"]+)" layer\(element\);$/gm)]
    expect(lazy.length).toBeGreaterThan(20)
    for (const entry of ["src/styles/element-entry.css", "src/styles/element-workspace.css"]) {
      const lines = read(entry)
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .split("\n")
        .filter((line) => line.trim())
      expect(
        lines.every((line) => /^@import "element-plus\/theme-chalk\/[\w.-]+\.css" layer\(element\);$/.test(line)),
      ).toBe(true)
    }
  })

  it("懒加载组件覆写与 Element 同在 element 层，theme.css 不再以 :root 提权", () => {
    const themeSelectors = cssRules(themeCss).flatMap((rule) => rule.selectors)
    expect(themeSelectors.filter((selector) => selector.startsWith(":root "))).toEqual([])
    expect(
      themeSelectors.filter((selector) =>
        /\.el-(?:tag|alert|pagination|date-editor|range-|picker)|qingluan-date-popper/.test(selector),
      ),
    ).toEqual([])
    // 入口组件（按钮、toast）的样式先于 theme.css 加载，覆写留在入口
    expect(themeSelectors).toContain(".el-message--success")
    expect(themeSelectors).toContain(".el-button--primary.is-plain")

    const workspaceEntry = read("src/styles/workspace.css")
    expect(workspaceEntry).toContain(`@import "./workspace/element.css" layer(element);`)
    expect(workspaceEntry).not.toMatch(/element-(?:dark|light)\.css/)
    const elementSelectors = cssRules(read("src/styles/workspace/element.css")).flatMap((rule) => rule.selectors)
    expect(elementSelectors.filter((selector) => selector.startsWith(":root"))).toEqual([])
    for (const required of [
      ".el-tag.el-tag--success",
      ".el-tag.el-tag--danger",
      ".el-tag.el-tag--dark.el-tag--danger",
      ".el-alert--error",
      ".el-pagination",
      ".el-date-editor .el-range-input",
    ]) {
      expect(elementSelectors).toContain(required)
    }
    // 实心危险标签的压暗底白字在明亮模式下同样生效：明亮重混对 el-tag--dark 让出
    expect(read("src/styles/workspace/element.css")).toContain(
      '[data-theme="light"] .el-tag.el-tag--danger:not(.el-tag--dark)',
    )
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
