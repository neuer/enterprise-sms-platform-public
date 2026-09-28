// 计算样式快照：样式重构前后逐元素比对，验证「行为等价」。仅本地使用，不进 CI。
//
//   node scripts/style-snapshot.mjs snap --base <预览地址> --out <文件> [--theme dark|light]
//        [--width 1440] [--touch] --password-stdin < <本地 Mock 账号密码文件>
//   node scripts/style-snapshot.mjs diff <前.json> <后.json> [--limit 12]
//
// 登录走对话式 AD 登录（admin01）；密码只从标准输入读入内存，不接受命令参数或环境变量。
// 浏览器使用本机安装的 Chrome（playwright-core 不自带浏览器）。
// 快照覆盖全部路由、主要抽屉/页签，以及输入框、按钮、下拉、分页等的悬停与聚焦状态。
import { readFileSync, writeFileSync } from "node:fs"
import { chromium } from "playwright-core"

const PROPS = [
  "color",
  "backgroundColor",
  "backgroundImage",
  "fontSize",
  "fontWeight",
  "fontFamily",
  "fontStyle",
  "lineHeight",
  "letterSpacing",
  "textAlign",
  "textDecorationLine",
  "textTransform",
  "whiteSpace",
  "borderTopColor",
  "borderRightColor",
  "borderBottomColor",
  "borderLeftColor",
  "borderTopWidth",
  "borderRightWidth",
  "borderBottomWidth",
  "borderLeftWidth",
  "borderTopStyle",
  "borderRadius",
  "paddingTop",
  "paddingRight",
  "paddingBottom",
  "paddingLeft",
  "marginTop",
  "marginRight",
  "marginBottom",
  "marginLeft",
  "boxShadow",
  "outlineColor",
  "outlineStyle",
  "outlineWidth",
  "outlineOffset",
  "opacity",
  "display",
  "position",
  "zIndex",
  "flexDirection",
  "flexWrap",
  "alignItems",
  "justifyContent",
  "gap",
  "gridTemplateColumns",
  "overflowX",
  "overflowY",
  "cursor",
  "transform",
  "minHeight",
  "minWidth",
  "width",
  "height",
  "fill",
  "stroke",
]
const ROUTES = [
  "dashboard",
  "reports",
  "send",
  "approvals",
  "batches",
  "messages",
  "replies",
  "templates",
  "signs",
  "apps",
  "blacklist",
  "sensitive-words",
  "users",
  "configs",
  "callbacks",
  "ops",
  "ops?tab=jobs",
  "security-daily",
  "audit",
]
const OVERLAYS = [
  ["apps", "apps#detail", '[data-testid^="app-detail-"]'],
  ["apps", "apps#editor", '[data-testid="new-app"]'],
  ["users", "users#create", '[data-testid="create-local-user"]'],
  ["users", "users#role", '[data-testid^="role-"]'],
  ["templates", "templates#create", '[data-testid="new-template"]'],
  ["blacklist", "blacklist#add", '[data-testid="blacklist-add-open"]'],
  ["configs", "configs#providers", "#config-tab-providers"],
  ["configs", "configs#vendor-test", "#config-tab-vendor-test"],
]
const STATE_TARGETS = {
  send: [
    "textarea",
    ".el-input__wrapper",
    ".el-select__wrapper",
    ".el-button--primary",
    ".el-button:not(.el-button--primary)",
    ".el-checkbox",
    ".el-date-editor",
  ],
  batches: [".el-input__wrapper", ".el-date-editor", ".el-pagination .btn-next", ".filter-seg button:not(.on)"],
  configs: [".el-input-number", ".el-input__wrapper", ".config-tabs button:not(.active)"],
  users: [".el-button.is-link", ".el-select__wrapper"],
  audit: [".el-input__wrapper", ".el-button"],
}

function parseArgs(argv) {
  const flags = {}
  const rest = []
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (!arg.startsWith("--")) rest.push(arg)
    else if (["--touch", "--password-stdin"].includes(arg)) flags[arg.slice(2)] = true
    else flags[arg.slice(2)] = argv[(i += 1)]
  }
  return { flags, rest }
}

/** 记录 root（缺省为 body）下全部可见元素的计算样式；键为「标签.类名:序号」结构路径。 */
function collect({ props, rootSelector }) {
  const result = {}
  const path = (el) => {
    const parts = []
    for (let n = el; n && n.nodeType === 1 && n !== document.documentElement; n = n.parentElement) {
      const cls = [...n.classList]
        .filter((c) => !/^(is-|el-popper|hover|focus|el-tooltip|data-v-)/.test(c))
        .sort()
        .join(".")
      const idx = n.parentElement ? [...n.parentElement.children].indexOf(n) : 0
      parts.unshift(`${n.tagName.toLowerCase()}${cls ? `.${cls}` : ""}:${idx}`)
    }
    return parts.join(">")
  }
  const read = (cs) => {
    const v = {}
    for (const p of props) v[p] = p === "width" || p === "height" ? String(Math.round(parseFloat(cs[p]) || 0)) : cs[p]
    return v
  }
  const root = rootSelector ? document.querySelector(rootSelector) : document.body
  if (!root) return result
  const nodes = rootSelector
    ? [root.parentElement ?? root, root, ...root.querySelectorAll("*")]
    : root.querySelectorAll("*")
  for (const el of nodes) {
    if (["SCRIPT", "STYLE", "LINK", "META"].includes(el.tagName)) continue
    const rect = el.getBoundingClientRect()
    if (rect.width === 0 && rect.height === 0) continue
    const cs = getComputedStyle(el)
    if (cs.visibility === "hidden") continue
    const key = path(el)
    result[key] = read(cs)
    for (const pseudo of ["::before", "::after"]) {
      const ps = getComputedStyle(el, pseudo)
      if (ps.content && ps.content !== "none" && ps.content !== "normal") result[`${key}${pseudo}`] = read(ps)
    }
    if (el.matches("input[placeholder], textarea[placeholder]")) {
      const ps = getComputedStyle(el, "::placeholder")
      result[`${key}::placeholder`] = { color: ps.color, fontSize: ps.fontSize }
    }
  }
  return result
}

async function readPassword() {
  const chunks = []
  for await (const chunk of process.stdin) chunks.push(chunk)
  return Buffer.concat(chunks)
    .toString("utf8")
    .replace(/\r?\n$/, "")
}

async function snap(flags) {
  if (!flags.base || !flags.out || !flags["password-stdin"]) {
    throw new Error("snap 需要 --base、--out 与 --password-stdin")
  }
  const theme = flags.theme ?? "dark"
  const width = Number(flags.width ?? 1440)
  let secret = await readPassword()
  const browser = await chromium.launch({ channel: "chrome" })
  const context = await browser.newContext({
    viewport: { width, height: flags.touch ? 844 : 900 },
    reducedMotion: "reduce",
    hasTouch: Boolean(flags.touch),
    isMobile: Boolean(flags.touch),
  })
  await context.addInitScript((t) => localStorage.setItem("sms-theme", t), theme)
  const page = await context.newPage()
  const take = (rootSelector) => page.evaluate(collect, { props: PROPS, rootSelector })
  const go = async (route, wait = 2200) => {
    await page.evaluate((r) => {
      window.history.pushState({}, "", `/${r}`)
      window.dispatchEvent(new PopStateEvent("popstate"))
    }, route)
    await page.waitForTimeout(wait)
  }
  const data = {}
  try {
    await page.goto(`${flags.base}/login`)
    await page.waitForTimeout(1500)
    data.login = await take()
    await page.getByTestId("provider-ad").click()
    const username = page.getByTestId("login-username")
    await username.fill("admin01")
    await username.press("Enter")
    const password = page.getByTestId("login-password")
    await password.waitFor({ state: "visible" })
    await page.waitForFunction(
      () => !document.querySelector('[data-testid="login-password"]')?.classList.contains("is-parked"),
    )
    await password.fill(secret)
    secret = ""
    await password.press("Enter")
    await page.waitForURL(/dashboard/, { timeout: 20000 })

    for (const route of ROUTES) {
      await go(route)
      data[route] = await take()
    }
    for (const [route, name, selector] of OVERLAYS) {
      await go(route, 1800)
      try {
        await page.locator(selector).first().click({ timeout: 3000 })
        await page.waitForTimeout(900)
        data[name] = await take()
      } catch (error) {
        data[name] = { error: String(error).slice(0, 160) }
      }
      await page.keyboard.press("Escape")
      await page.waitForTimeout(400)
    }
    for (const [route, selectors] of Object.entries(STATE_TARGETS)) {
      await go(route)
      for (const selector of selectors) {
        const target = page.locator(`${selector} >> visible=true`).first()
        if (!(await target.count())) continue
        await target.evaluate((el) => el.setAttribute("data-style-snap", ""))
        try {
          await target.hover({ timeout: 2000 })
          await page.waitForTimeout(150)
          data[`${route}@hover ${selector}`] = await take("[data-style-snap]")
          await target.evaluate((el) => (el.querySelector("input, textarea, button") ?? el).focus())
          await page.mouse.move(0, 0)
          await page.waitForTimeout(150)
          data[`${route}@focus ${selector}`] = await take("[data-style-snap]")
        } catch (error) {
          data[`${route}@state ${selector}`] = { error: String(error).slice(0, 160) }
        }
        await target.evaluate((el) => {
          el.removeAttribute("data-style-snap")
          document.activeElement?.blur?.()
        })
      }
    }
  } finally {
    await browser.close()
  }
  writeFileSync(flags.out, JSON.stringify(data))
  const errors = Object.entries(data).filter(([, v]) => v.error)
  console.log(`snap: ${Object.keys(data).length} 段，${errors.length} 段出错 → ${flags.out}`)
  for (const [name, v] of errors) console.log(`  ! ${name}: ${v.error}`)
}

function diff(rest, flags) {
  const [before, after] = rest.map((file) => JSON.parse(readFileSync(file, "utf8")))
  const limit = Number(flags.limit ?? 12)
  let total = 0
  const summary = new Map()
  for (const section of [...new Set([...Object.keys(before), ...Object.keys(after)])].sort()) {
    const a = before[section] ?? {}
    const b = after[section] ?? {}
    if (a.error || b.error) {
      console.log(`## ${section}: error before=${a.error ?? "-"} after=${b.error ?? "-"}`)
      continue
    }
    const missing = Object.keys(a).filter((k) => !(k in b))
    const added = Object.keys(b).filter((k) => !(k in a))
    const changes = []
    for (const key of Object.keys(a)) {
      if (!(key in b)) continue
      for (const [prop, va] of Object.entries(a[key])) {
        if (b[key][prop] !== va) {
          changes.push(`${key.slice(-110)} ${prop}: ${va} -> ${b[key][prop]}`)
          const tally = `${prop}: ${va} -> ${b[key][prop]}`
          summary.set(tally, (summary.get(tally) ?? 0) + 1)
        }
      }
    }
    if (missing.length || added.length || changes.length) {
      console.log(`## ${section}: -${missing.length} +${added.length} ~${changes.length}`)
      for (const k of missing.slice(0, 3)) console.log(`   - ${k.slice(-140)}`)
      for (const k of added.slice(0, 3)) console.log(`   + ${k.slice(-140)}`)
      for (const line of changes.sort().slice(0, limit)) console.log(`   ~ ${line}`)
    }
    total += missing.length + added.length + changes.length
  }
  console.log(`TOTAL ${total}`)
  const top = [...summary.entries()].sort((x, y) => y[1] - x[1]).slice(0, 12)
  for (const [tally, n] of top) console.log(`  ${n}× ${tally}`)
  process.exitCode = total ? 1 : 0
}

const [command, ...argv] = process.argv.slice(2)
const { flags, rest } = parseArgs(argv)
if (command === "snap") await snap(flags)
else if (command === "diff") diff(rest, flags)
else {
  console.error("用法：style-snapshot.mjs snap|diff …（见文件头注释）")
  process.exitCode = 2
}
