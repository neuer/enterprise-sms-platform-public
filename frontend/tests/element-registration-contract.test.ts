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
// main.ts 以 for (const plugin of [ElAlert, ...]) application.use(plugin) 形式按需注册；
// 从注册数组中提取组件名，作为生产环境真实可用的组件全集。
const registryMatch = mainTs.match(/for \(const plugin of \[([\s\S]*?)\]\) application\.use\(plugin\)/)
const registered = new Set(
  registryMatch ? (registryMatch[1].match(/El[A-Za-z]+/g) ?? []) : [],
)

const vueFiles = [...listVueFiles("src/views"), ...listVueFiles("src/components"), "src/App.vue"]

describe("Element Plus 组件注册契约", () => {
  it("main.ts 采用按需注册并可被解析", () => {
    expect(registryMatch).not.toBeNull()
    expect(registered.size).toBeGreaterThan(10)
  })

  it("模板中出现的每个 <el-*> 组件都已在 main.ts 注册", () => {
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
})
