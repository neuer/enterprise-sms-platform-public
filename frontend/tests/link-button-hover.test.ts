import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const css = readFileSync(resolve(process.cwd(), "src/styles/workspace.css"), "utf8")

describe("文字按钮悬停主题契约", () => {
  it("主要文字按钮覆盖 Element Plus link 悬停变量，悬停与按下不变暗发蓝", () => {
    // Element Plus 的 is-link 按钮悬停读取 --el-button-hover-link-text-color、
    // 按下读取 --el-button-active-color；写成 --el-button-hover-text-color 不生效。
    const block = css.match(/\.el-button\.is-link\.el-button--primary\s*\{[^}]*\}/s)?.[0] ?? ""
    expect(block).toContain("--el-button-text-color: var(--verdi-l)")
    // 悬停色走 --verdi-text 令牌（深色 #71c4ad）：亮色主题下自动取亮底文字绿 #0b6a55。
    expect(block).toContain("--el-button-hover-link-text-color: var(--verdi-text)")
    expect(block).toContain("--el-button-active-color: var(--verdi-l)")
  })
})
