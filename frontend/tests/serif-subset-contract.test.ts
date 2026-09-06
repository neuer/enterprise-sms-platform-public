import { existsSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

const read = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")
const stat = (path: string) => statSync(resolve(process.cwd(), path))

// 字符清单是 scripts/subset_fonts.py 生成子集字体的唯一字符来源；
// `#` 开头为注释行，其余行的非空白字符全部计入。
const manifestChars = new Set(
  read("src/assets/fonts/serif-subset-glyphs.txt")
    .split("\n")
    .filter((line) => !line.startsWith("#"))
    .join("")
    .replace(/\s/g, ""),
)

// serif（var(--serif)）只渲染固定品牌文案，使用点共四处五段。每条提取规则
// 必须命中且文本非空——模板结构变化导致提取不到也算漂移，强制同步清单。
const SERIF_TEXT_SOURCES: Array<{ file: string; pattern: RegExp; note: string }> = [
  {
    file: "src/views/LoginView.vue",
    pattern: /class="login-brand-name"[^>]*>([^<]+)</,
    note: ".login-brand-name 登录品牌名",
  },
  {
    file: "src/views/PasswordChangeView.vue",
    pattern: /class="login-brand-name"[^>]*>([^<]+)</,
    note: ".login-brand-name 改密品牌名",
  },
  {
    file: "src/views/PasswordChangeView.vue",
    pattern: /class="mode-title"[^>]*>([^<]+)</,
    note: ".mode-title 首次改密标题",
  },
  {
    file: "src/App.vue",
    pattern: /class="brand-mark"[^>]*>([^<]+)</,
    note: ".brand-mark 侧栏圆形印章字",
  },
  {
    file: "src/App.vue",
    pattern: /<div class="brand"[\s\S]*?<strong>([^<]+)<\/strong>/,
    note: ".brand strong 侧栏品牌名",
  },
]

describe("Noto Serif SC 子集契约", () => {
  it("每个 serif 使用点都能提取到非空文案", () => {
    for (const { file, pattern, note } of SERIF_TEXT_SOURCES) {
      const match = read(file).match(pattern)
      expect(match?.[1]?.trim(), `${file} ${note} 提取失败`).toBeTruthy()
    }
  })

  it("serif 使用点当前文案的全部字符都在子集清单内", () => {
    const missing: string[] = []
    for (const { file, pattern, note } of SERIF_TEXT_SOURCES) {
      const text = read(file).match(pattern)?.[1] ?? ""
      for (const char of text) {
        if (/\s/.test(char)) continue
        if (!manifestChars.has(char)) missing.push(`${file} ${note}: "${char}"`)
      }
    }
    expect(missing).toEqual([])
  })

  it("子集产物已生成且非空（变更清单后须重跑 scripts/subset_fonts.py）", () => {
    for (const weight of [400, 600]) {
      const path = `src/assets/fonts/noto-serif-sc-subset-${weight}.woff2`
      expect(existsSync(resolve(process.cwd(), path)), `${path} 不存在`).toBe(true)
      expect(stat(path).size, `${path} 体积异常`).toBeGreaterThan(1024)
    }
  })

  it("入口不再引入完整 serif 字体包，theme.css 声明子集 @font-face", () => {
    expect(read("src/main.ts")).not.toContain("noto-serif-sc")
    const theme = read("src/styles/theme.css")
    expect(theme).toContain('font-family: "Noto Serif SC Variable"')
    expect(theme).toContain("noto-serif-sc-subset-400.woff2")
    expect(theme).toContain("noto-serif-sc-subset-600.woff2")
  })
})
