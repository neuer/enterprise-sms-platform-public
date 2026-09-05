import { readFileSync } from "node:fs"
import { dirname, resolve } from "node:path"

/**
 * 读取壳样式全文：workspace.css 拆分后为 @import 聚合入口（src/styles/workspace/ 分片），
 * 契约测试按引入顺序内联各分片，得到与拆分前单文件等价的完整样式文本。
 */
export function readWorkspaceCss(): string {
  const entryPath = resolve(process.cwd(), "src/styles/workspace.css")
  const entry = readFileSync(entryPath, "utf8")
  return entry
    .split("\n")
    .map((line) => {
      const match = /^@import "(\.[^"]+)";$/.exec(line)
      return match ? readFileSync(resolve(dirname(entryPath), match[1]), "utf8") : line
    })
    .join("\n")
}
