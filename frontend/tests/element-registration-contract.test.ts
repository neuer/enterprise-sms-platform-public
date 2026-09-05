import { readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

function listVueFiles(dir: string): string[] {
  const base = resolve(process.cwd(), dir)
  const out: string[] = []
  for (const entry of readdirSync(base)) {
    const full = resolve(base, entry)
    if (statSync(full).isDirectory()) {
      for (const nested of listVueFiles(`${dir}/${entry}`)) out.push(nested)
    } else if (entry.endsWith(".vue")) {
      out.push(`${dir}/${entry}`)
    }
  }
  return out
}

const toComponentName = (tag: string) =>
  `El${tag
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join("")}`

const mainTs = read("src/main.ts")
const workspaceTs = read("src/element-workspace.ts")

// 两级注册：main.ts 保留公开壳最小集，element-workspace.ts 承载认证工作区组件，
// 由路由守卫在首个非公开路由渲染前动态 import 完成注册（app.use 挂载后仍有效）。
// 两处均以 for (const plugin of [ElXxx, ...]) …use(plugin) 形式注册，可被静态解析。
function extractRegistered(source: string): Set<string> {
  const match = source.match(/for \(const plugin of \[([\s\S]*?)\]\)\s*\w+\.use\(plugin\)/)
  return new Set(match ? (match[1].match(/El[A-Za-z]+/g) ?? []) : [])
}

const entryRegistered = extractRegistered(mainTs)
const workspaceRegistered = extractRegistered(workspaceTs)
const registered = new Set([...entryRegistered, ...workspaceRegistered])

// 拆分前的完整注册全集：两级拆分只允许搬家，不允许漏注册或重复注册。
const FULL_REGISTRY = [
  "ElAlert", "ElButton", "ElCard", "ElCheckbox", "ElCheckboxGroup", "ElConfigProvider",
  "ElDatePicker", "ElDescriptions", "ElDescriptionsItem", "ElDialog", "ElDrawer",
  "ElForm", "ElFormItem", "ElInput", "ElInputNumber", "ElLoading", "ElOption",
  "ElPagination", "ElPopover", "ElRadioButton", "ElRadioGroup", "ElSegmented",
  "ElSelect", "ElSkeleton", "ElSwitch", "ElTabPane", "ElTable", "ElTableColumn",
  "ElTabs", "ElTag", "ElTooltip", "ElUpload",
]

// 公开壳（App.vue + LoginView + PasswordChangeView）模板实际用到的最小组件集。
const ENTRY_REGISTRY = ["ElButton", "ElConfigProvider", "ElInput"]

const vueFiles = [...listVueFiles("src/views"), ...listVueFiles("src/components"), "src/App.vue"]

describe("Element Plus 组件注册契约", () => {
  it("两级注册模块均可解析，最小集精确且与工作区集互不重叠", () => {
    expect([...entryRegistered].sort()).toEqual([...ENTRY_REGISTRY].sort())
    expect(workspaceRegistered.size).toBeGreaterThan(20)
    const overlap = [...entryRegistered].filter((name) => workspaceRegistered.has(name))
    expect(overlap).toEqual([])
  })

  it("最小集与工作区集的并集等于拆分前的完整注册全集", () => {
    expect([...registered].sort()).toEqual([...FULL_REGISTRY].sort())
  })

  it("模板中出现的每个 <el-*> 组件都已注册（入口或工作区）", () => {
    // 未注册的组件在运行时退化为原生自定义元素：默认插槽被内联渲染、
    // 具名插槽（如 popover 的 #reference）被丢弃——本用例防该类回归。
    const missing: string[] = []
    for (const file of vueFiles) {
      const source = read(file)
      const tags = new Set(source.match(/<el-[a-z][a-z-]*/g) ?? [])
      for (const tag of tags) {
        const name = toComponentName(tag.slice(4))
        if (!registered.has(name)) missing.push(`${file}: <${tag}> (${name} 未注册)`)
      }
    }
    expect(missing).toEqual([])
  })

  it("main.ts 的注册守卫只在非公开路由前动态加载工作区注册模块", () => {
    expect(mainTs).toContain('import("./element-workspace")')
    expect(mainTs).toContain("registerWorkspaceElement")
    expect(mainTs).toContain("router.beforeEach")
    // 工作区注册模块不得被静态引入入口 chunk
    expect(mainTs).not.toContain('from "./element-workspace"')
  })
})
