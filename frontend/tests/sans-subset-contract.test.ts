import { existsSync, readdirSync, readFileSync, statSync } from "node:fs"
import { resolve } from "node:path"

const abs = (path: string) => resolve(process.cwd(), path)
const read = (path: string) => readFileSync(abs(path), "utf8")

// 清单由 scripts/subset_fonts.py 从源码生成；`#` 开头为注释行。
const manifestChars = new Set(
  read("src/assets/fonts/sans-subset-glyphs.txt")
    .split("\n")
    .filter((line) => !line.startsWith("#"))
    .join("")
    .replace(/\s/g, ""),
)

const fontCss = read("src/styles/fonts-sans.css")
const faces = [
  ...fontCss.matchAll(/url\("\.\.\/assets\/fonts\/sans\/([^"]+)"\)[^;]*;\s*unicode-range:\s*([^;]+);/g),
].map(([, file, range]) => ({
  file,
  ranges: range.split(",").map((part) => {
    const [start, end] = part.trim().replace(/^U\+/i, "").split("-")
    return [parseInt(start, 16), parseInt(end ?? start, 16)] as const
  }),
}))

function frontendSourceFiles(): string[] {
  const files = readdirSync(abs("src"), { recursive: true, encoding: "utf8" })
    .filter((name) => /\.(?:vue|ts|css)$/.test(name))
    .map((name) => `src/${name}`)
  return [...files, "index.html"]
}

describe("Noto Sans SC 子集契约", () => {
  it("前端源码字符全部在子集清单内（新增文案后运行 uv run scripts/subset_fonts.py sans）", () => {
    const missing = new Set<string>()
    for (const file of frontendSourceFiles()) {
      for (const char of read(file)) {
        if (!/\s/.test(char) && !manifestChars.has(char)) missing.add(char)
      }
    }
    expect([...missing].join("")).toBe("")
  })

  it("清单内的中日韩表意文字都被某个子集分片的 unicode-range 覆盖", () => {
    const uncovered = [...manifestChars].filter((char) => {
      const cp = char.codePointAt(0) ?? 0
      if (cp < 0x4e00 || cp > 0x9fff) return false
      return !faces.some(({ ranges }) => ranges.some(([lo, hi]) => lo <= cp && cp <= hi))
    })
    expect(uncovered.join("")).toBe("")
  })

  it("每个 @font-face 引用的子集文件都存在且非空", () => {
    expect(faces.length).toBeGreaterThan(10)
    for (const { file } of faces) {
      const path = `src/assets/fonts/sans/${file}`
      expect(existsSync(abs(path)), `${path} 不存在`).toBe(true)
      expect(statSync(abs(path)).size, `${path} 体积异常`).toBeGreaterThan(256)
    }
  })

  it("入口只引入子集声明，不再拖入完整 fontsource 分片", () => {
    const main = read("src/main.ts")
    expect(main).toContain('import "./styles/fonts-sans.css"')
    expect(main).not.toContain("@fontsource-variable/noto-sans-sc")
  })
})
