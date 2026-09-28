import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"

const IMPORT_LINE = /^@import "(\.[^"]+)"(?: layer\([\w-]+\))?;$/

/** 递归内联相对路径 @import（含 layer(...) 形式），得到聚合入口的完整样式文本。 */
export function readCssTree(path: string): string {
  const entryPath = resolve(process.cwd(), path)
  return readFileSync(entryPath, "utf8")
    .split("\n")
    .map((line) => {
      const match = IMPORT_LINE.exec(line)
      return match ? readCssTree(resolve(dirname(entryPath), match[1])) : line
    })
    .join("\n")
}

/** 工作区样式全文：workspace.css 聚合 src/styles/workspace/ 分片。 */
export function readWorkspaceCss(): string {
  return readCssTree("src/styles/workspace.css")
}

/** 入口样式全文：theme.css 聚合 src/styles/theme/ 分片与 @font-face。 */
export function readThemeCss(): string {
  return readCssTree("src/styles/theme.css")
}
