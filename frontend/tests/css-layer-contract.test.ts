import { readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")
const stripComments = (css: string) => css.replace(/\/\*[\s\S]*?\*\//g, "")

function listFiles(dir: string, ext: string): string[] {
  const base = resolve(process.cwd(), dir)
  return readdirSync(base).flatMap((entry) => {
    const full = `${dir}/${entry}`
    return statSync(resolve(base, entry)).isDirectory() ? listFiles(full, ext) : entry.endsWith(ext) ? [full] : []
  })
}

/** 层顺序：先声明的层优先级最低（docs/ui-design.md「样式分层」）。 */
const LAYERS = ["reset", "element", "tokens", "shell", "components", "pages", "theme-light", "touch"]
const AGGREGATORS: Record<string, string> = {
  "src/styles/theme.css": "src/styles/theme/",
  "src/styles/workspace.css": "src/styles/workspace/",
  "src/styles/element-entry.css": "",
  "src/styles/element-workspace.css": "",
}

/** 聚合入口里分片 → 所属层。 */
function partLayers(): Map<string, string> {
  const map = new Map<string, string>()
  for (const [entry, dir] of Object.entries(AGGREGATORS)) {
    for (const [, path, layer] of stripComments(read(entry)).matchAll(/^@import "([^"]+)" layer\(([\w-]+)\);$/gm)) {
      map.set(path.startsWith("./") ? `${dir}${path.replace(/^\.\/[\w-]+\//, "")}` : path, layer)
    }
  }
  return map
}

function vueStyles(): Array<{ path: string; css: string }> {
  return [...listFiles("src", ".vue")].flatMap((path) =>
    [...read(path).matchAll(/<style[^>]*>([\s\S]*?)<\/style>/g)].map((match) => ({ path, css: match[1] })),
  )
}

function splitTopLevel(list: string): string[] {
  const out: string[] = []
  let depth = 0
  let current = ""
  for (const char of list) {
    if (char === "(") depth += 1
    if (char === ")") depth -= 1
    if (char === "," && depth === 0) {
      out.push(current.trim())
      current = ""
    } else {
      current += char
    }
  }
  return [...out, current.trim()].filter(Boolean)
}

/** 主体只含 .el-* / .is-* 类、祖先只含全局容器或 Element 类的选择器：纯组件级覆写。 */
function isComponentLevelElementSelector(selector: string): boolean {
  const compounds = selector.trim().split(/\s*[\s>+~]\s*(?![^(]*\))/)
  const subject = compounds.at(-1) ?? ""
  const subjectClasses = subject.match(/[.#][\w-]+/g) ?? []
  if (!subjectClasses.some((token) => token.startsWith(".el-"))) return false
  if (!subjectClasses.every((token) => token.startsWith(".el-") || token.startsWith(".is-"))) return false
  const ancestorTokens = compounds.slice(0, -1).flatMap((compound) => compound.match(/[.#][\w-]+|\[[^\]]+\]/g) ?? [])
  return ancestorTokens.every(
    (token) =>
      token.startsWith(".el-") || token.startsWith(".is-") || token === ".workspace" || token.startsWith("[data-theme"),
  )
}

describe("级联层契约", () => {
  it("layers.css 是唯一的层顺序声明", () => {
    expect(stripComments(read("src/styles/layers.css")).trim()).toBe(`@layer ${LAYERS.join(", ")};`)
  })

  it("聚合入口只含带已登记层的 @import，每个层都有样式承载", () => {
    const used = new Set<string>()
    for (const entry of Object.keys(AGGREGATORS)) {
      const imports = stripComments(read(entry))
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.startsWith("@import"))
      expect(imports.length, `${entry} 没有 @import`).toBeGreaterThan(0)
      for (const line of imports) {
        const match = /^@import "[^"]+" layer\(([\w-]+)\);$/.exec(line)
        expect(match, `${entry} 的导入必须声明层：${line}`).not.toBeNull()
        expect(LAYERS, `${entry} 使用了未登记的层：${line}`).toContain(match![1])
        used.add(match![1])
      }
    }
    if (vueStyles().length) used.add("components")
    expect([...used].sort()).toEqual([...LAYERS].sort())
  })

  it("分片不自行声明层或再导入；组件内样式整块进 components 层", () => {
    const owners = new Set(["src/styles/layers.css", ...Object.keys(AGGREGATORS)])
    for (const path of listFiles("src/styles", ".css").filter((file) => !owners.has(file))) {
      const css = stripComments(read(path))
      expect(css, `${path} 的层归属只由聚合入口声明`).not.toMatch(/@layer\b/)
      expect(css, `${path} 不得再 @import`).not.toMatch(/@import\b/)
    }
    for (const { path, css } of vueStyles()) {
      const body = stripComments(css).trim()
      expect(
        body.startsWith("@layer components {") && body.endsWith("}"),
        `${path} 的 <style> 须整块包在 @layer components 内`,
      ).toBe(true)
      expect(body.match(/@layer\b/g)?.length, `${path} 只允许一个 @layer components`).toBe(1)
    }
  })

  it("组件级 Element 覆写只放 element 层，与 Element 同层比较特异性", () => {
    // 放在更高的层会无视特异性压过 Element 的 :hover / .is-* 状态规则。
    const layers = partLayers()
    for (const [path, layer] of layers) {
      if (!["shell", "pages", "theme-light"].includes(layer)) continue
      for (const [, selectorList] of stripComments(read(path)).matchAll(/([^{}]+)\{[^{}]*\}/g)) {
        if (selectorList.trim().startsWith("@")) continue
        const offenders = splitTopLevel(selectorList).filter(isComponentLevelElementSelector)
        expect(offenders, `${path}（${layer} 层）含组件级 Element 规则，应移入 element 层分片`).toEqual([])
      }
    }
  })

  it("!important 只在白名单内", () => {
    const allowed: Record<string, number> = {
      // 减少动效：reset 层的 !important 反而最强，压过任何上层。
      "src/styles/theme/reset.css": 4,
      // 窄屏抽屉/弹窗宽度压内联样式、分页跳页与日期弹层定位压组件内联定位。
      "src/styles/workspace/element.css": 9,
      "src/styles/workspace/audit.css": 2,
      "src/styles/workspace/vendor.css": 1,
    }
    const actual: Record<string, number> = {}
    for (const path of [...listFiles("src/styles", ".css"), ...listFiles("src", ".vue")]) {
      const count = stripComments(read(path)).match(/!important/g)?.length ?? 0
      if (count) actual[path] = count
    }
    expect(actual).toEqual(allowed)
  })

  it("touch 层只放命中区尺寸与触屏排布，不承载视觉样式", () => {
    const allowedProperty =
      /^(?:(?:min-|max-)?(?:width|height)|(?:padding|margin)(?:-\w+)?|display|flex(?:-wrap|-direction)?|justify-content|align-items|(?:row-|column-)?gap|font-size|line-height|overflow-[xy]|overscroll-behavior-x|scrollbar-width|text-align|white-space)$/
    for (const [path, layer] of partLayers()) {
      if (layer !== "touch") continue
      const properties = [...stripComments(read(path)).matchAll(/([\w-]+)\s*:[^;{}]+;/g)].map((match) => match[1])
      expect(properties.length).toBeGreaterThan(0)
      expect(
        properties.filter((property) => !allowedProperty.test(property)),
        path,
      ).toEqual([])
    }
  })
})
