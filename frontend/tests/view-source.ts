import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"

/** 页面拆分后，结构契约沿显式组件依赖检查实际模板，避免绑定单一大文件。 */
export function readViewSource(path: string, seen = new Set<string>()): string {
  const full = resolve(process.cwd(), path)
  if (seen.has(full)) return ""
  seen.add(full)
  const source = readFileSync(full, "utf8")
  if (!full.endsWith(".vue")) return source
  const children = [...source.matchAll(/from\s+["'](\.[^"']+\.vue)["']/g)]
  const owned = children.filter((match) =>
    /\/ops\/Ops|\/(?:ApiDemoDialog|SecurityDailyConfigDialog)\.vue$/.test(match[1]),
  )
  return [source, ...owned.map((match) => readViewSource(resolve(dirname(full), match[1]), seen))].join("\n")
}
