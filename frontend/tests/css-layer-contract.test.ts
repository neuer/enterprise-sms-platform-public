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

/** pages 层页面分片 → 使用它的路由视图；shared.css 承载跨页共享组。 */
const PAGE_VIEWS: Record<string, string[]> = {
  "src/styles/workspace/dashboard.css": ["DashboardView"],
  "src/styles/workspace/report.css": ["ReportView"],
  "src/styles/workspace/send.css": ["SendView"],
  "src/styles/workspace/approval.css": ["ApprovalView"],
  "src/styles/workspace/template-sign.css": ["TemplateView", "SignView"],
  "src/styles/workspace/blacklist-sensitive.css": ["BlacklistView", "SensitiveWordView"],
  "src/styles/workspace/message-batch.css": ["BatchView", "MessageView"],
  "src/styles/workspace/user.css": ["UserView"],
  "src/styles/workspace/ops.css": ["OpsView"],
  "src/styles/workspace/config.css": ["ConfigView"],
  "src/styles/workspace/vendor.css": ["ConfigView"],
  "src/styles/workspace/audit.css": ["AuditView"],
  "src/styles/workspace/apps.css": ["AppManagementView"],
  "src/styles/workspace/security-daily.css": ["SecurityDailyView"],
}
const SHARED_PAGES_CSS = "src/styles/workspace/shared.css"
const CONSOLE_DARK_CSS = "src/styles/workspace/console-dark.css"
/** 壳规则（侧栏、顶栏、main、小字对比度）仍留在页面分片中的存量，只减不增。 */
const SHELL_RULES_IN_PAGES: Record<string, number> = {
  "src/styles/workspace/blacklist-sensitive.css": 18,
  "src/styles/workspace/security-daily.css": 10,
}

type CssRule = { media: string; selector: string; properties: string[] }

/** 展开为「媒体条件 + 单个选择器 + 属性列表」；@keyframes 步进不计入。 */
function cssRules(css: string): CssRule[] {
  const rules: CssRule[] = []
  const stack: string[] = []
  let buffer = ""
  for (const char of stripComments(css)) {
    if (char === "{") {
      stack.push(buffer.trim())
      buffer = ""
    } else if (char === "}") {
      const prelude = stack.pop() ?? ""
      const media = stack.filter((item) => item.startsWith("@media")).join(" & ")
      if (prelude && !prelude.startsWith("@") && !stack.some((item) => item.startsWith("@keyframes"))) {
        const properties = [...buffer.matchAll(/([\w-]+)\s*:/g)].map((match) => match[1])
        for (const selector of splitTopLevel(prelude))
          rules.push({ media, selector: selector.replace(/\s+/g, " "), properties })
      }
      buffer = ""
    } else if (char === ";" && !stack.length) {
      buffer = ""
    } else {
      buffer += char
    }
  }
  return rules
}

const baseName = (path: string) => path.replace(/^.*\//, "").replace(/\.vue$/, "")

const words = (text: string, hyphenOnly: boolean) =>
  [...text.matchAll(/'([^'\\\n]*)'|"([^"\\\n]*)"|`([^`]*)`/g)]
    .flatMap((match) => (match[1] ?? match[2] ?? match[3]).match(/(?<![\w-])[a-zA-Z][\w-]*[a-zA-Z0-9](?![\w-])/g) ?? [])
    .filter((word) => !hyphenOnly || word.includes("-"))

/** 可能作为类名出现的词：模板里的引号串全部计入；脚本里只取带连字符的词，避免撞上普通单词。 */
function vueClassWords(text: string): string[] {
  const template = /<template>([\s\S]*)<\/template>/.exec(text)?.[1] ?? ""
  return [...words(template, false), ...words(text.replace(template, ""), true)]
}

/** 类名 → 使用它的路由视图；由 App.vue、公共脚本等非页面位置产出的记为 "*"。 */
function classUsage(): (className: string) => Set<string> {
  const vueFiles = new Map(listFiles("src", ".vue").map((path) => [baseName(path), path]))
  const routes = [...new Set([...read("src/router/index.ts").matchAll(/views\/(\w+)\.vue/g)].map((match) => match[1]))]
  const pagesOf = new Map<string, Set<string>>()
  for (const route of routes) {
    const stack = [route]
    const seen = new Set<string>()
    while (stack.length) {
      const name = stack.pop()!
      if (seen.has(name)) continue
      seen.add(name)
      pagesOf.set(name, (pagesOf.get(name) ?? new Set()).add(route))
      for (const [, imported] of read(vueFiles.get(name)!).matchAll(/["']([^"']+\.vue)["']/g)) {
        if (vueFiles.has(baseName(imported))) stack.push(baseName(imported))
      }
    }
  }
  const usage = new Map<string, Set<string>>()
  const add = (word: string, pages: Set<string>) => {
    const entry = usage.get(word) ?? new Set<string>()
    pages.forEach((page) => entry.add(page))
    usage.set(word, entry)
  }
  for (const [name, path] of vueFiles) {
    for (const word of vueClassWords(read(path))) add(word, pagesOf.get(name) ?? new Set(["*"]))
  }
  for (const path of listFiles("src", ".ts").filter((file) => !/^src\/(api|router)\//.test(file))) {
    for (const word of words(read(path), true)) add(word, new Set(["*"]))
  }
  return (className) => usage.get(className) ?? new Set()
}

/** 参与归属判定的类：去掉 :not() 内的类与 Element 的 el-/is- 类。 */
function ownClasses(selector: string): string[] {
  const outer = selector.replace(/:not\((?:[^()]|\([^()]*\))*\)/g, "")
  return (outer.match(/\.[a-zA-Z][\w-]*/g) ?? [])
    .map((token) => token.slice(1))
    .filter((name) => !/^(el|is)-/.test(name))
}

describe("页面分片归属契约", () => {
  const usage = classUsage()
  const shellClasses = new Set(vueClassWords(read("src/App.vue")))
  const isShellSelector = (selector: string) => ownClasses(selector).every((name) => shellClasses.has(name))
  const pageFiles = [...partLayers()].filter(([, layer]) => layer === "pages").map(([path]) => path)

  it("pages 层每个分片都登记了所属页面", () => {
    expect(pageFiles[0], "shared.css 须是 pages 层第一个分片").toBe(SHARED_PAGES_CSS)
    expect(pageFiles.filter((path) => path !== SHARED_PAGES_CSS && path !== CONSOLE_DARK_CSS).sort()).toEqual(
      Object.keys(PAGE_VIEWS).sort(),
    )
  })

  it("页面分片的每个选择器都锚定到本页独有的类；跨页规则放 shared.css", () => {
    const shellCounts: Record<string, number> = {}
    for (const [path, views] of Object.entries(PAGE_VIEWS)) {
      const own = new Set(views)
      const offenders: string[] = []
      for (const { media, selector } of cssRules(read(path))) {
        const known = ownClasses(selector)
          .map((name) => [name, usage(name)] as const)
          .filter(([, pages]) => pages.size)
        if (known.some(([, pages]) => !pages.has("*") && [...pages].every((page) => own.has(page)))) continue
        if (isShellSelector(selector)) {
          shellCounts[path] = (shellCounts[path] ?? 0) + 1
          continue
        }
        const elsewhere = [...new Set(known.flatMap(([, pages]) => [...pages].filter((page) => !own.has(page))))]
        offenders.push(`${media} ${selector} → ${elsewhere.length ? elsewhere.join("/") : "全站"}`.trim())
      }
      expect(offenders, `${path} 含不属于本页的规则`).toEqual([])
    }
    expect(shellCounts).toEqual(SHELL_RULES_IN_PAGES)
  })

  it("页面分片之间不重复声明同一选择器的同一属性，胜负不依赖导入先后", () => {
    const owners = new Map<string, Set<string>>()
    for (const path of pageFiles.filter((file) => file !== CONSOLE_DARK_CSS)) {
      for (const { media, selector, properties } of cssRules(read(path))) {
        if (isShellSelector(selector)) continue
        for (const property of properties) {
          const key = `${media} ${selector} { ${property} }`.trim()
          owners.set(key, (owners.get(key) ?? new Set()).add(path))
        }
      }
    }
    const duplicated = [...owners]
      .filter(([, paths]) => paths.size > 1)
      .map(([key, paths]) => `${key}: ${[...paths].join(", ")}`)
    expect(duplicated).toEqual([])
  })
})
