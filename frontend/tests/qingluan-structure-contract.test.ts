import { readFileSync } from "node:fs"
import { resolve } from "node:path"

import { readThemeCss, readWorkspaceCss } from "./workspace-css"

const source = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

/**
 * 结构级契约补集：只断言规范（docs/ui-design.md §6/§8/§8a）中的稳定锚点，
 * 行为级断言见 governance-admin-views / query-views / callback-view / login-view。
 */
describe("黑名单 / 敏感词 qingluan 结构契约（规范 §8/§8a）", () => {
  const blacklist = source("src/views/BlacklistView.vue")
  const sensitive = source("src/views/SensitiveWordView.vue")

  it("黑名单：单行检索条含来源 seg 与关键词 + 查询/重置", () => {
    expect(blacklist).toContain('class="blacklist-filter-bar"')
    expect(blacklist).toContain('data-testid="blacklist-source-seg"')
    expect(blacklist).toContain('data-testid="blacklist-filter-keyword"')
    expect(blacklist).toContain('data-testid="blacklist-search"')
    expect(blacklist).toContain('data-testid="blacklist-reset"')
    // 隐私说明沉条底：服务端 ILIKE 仅匹配掩码与备注
    expect(blacklist).toContain("blacklist-privacy")
    expect(blacklist).toContain("ILIKE")
  })

  it("黑名单：底栏「共 N 条 · 每页 20」（ListPagination 单点承载）与审计 toast 句式", () => {
    expect(blacklist).toContain('testid="blacklist-pagination"')
    // 计数文案与默认每页 20 由 ListPagination 单点渲染
    expect(source("src/components/ListPagination.vue")).toContain("每页 {{ pageSize }}")
    expect(source("src/components/ListPagination.vue")).toContain("pageSize: DEFAULT_PAGE_SIZE")
    expect(blacklist).toContain("已移出黑名单 · 本次操作已记入审计")
    // 添加 toast 分账：新增 N 个（· 已存在并更新 M 个）· 本次操作已记入审计
    expect(blacklist).toMatch(/新增 \$\{result\.added\} 个.*本次操作已记入审计/s)
  })

  it("敏感词：命中策略 seg + 单行检索条 + 查询/重置", () => {
    expect(sensitive).toContain('class="sensitive-filter-bar"')
    expect(sensitive).toContain('label="命中策略"')
    // 策略 seg 按钮 testid 由 FilterSeg 按前缀派生：sensitive-policy-${value}
    expect(sensitive).toContain('button-testid-prefix="sensitive-policy"')
    expect(sensitive).toContain('data-testid="sensitive-filter-keyword"')
    expect(sensitive).toContain('data-testid="sensitive-search"')
    expect(sensitive).toContain('data-testid="sensitive-reset"')
    // 命中策略点选即写 sensitive_hit_action，失败回退原值
    expect(sensitive).toContain('key: "sensitive_hit_action"')
  })

  it("敏感词：结果面板为词条墙（tooltip 收添加时间），底栏「共 N 条 · 每页 60」", () => {
    expect(sensitive).toContain('data-testid="sensitive-wall"')
    expect(sensitive).toContain('class="sensitive-tile"')
    expect(sensitive).toContain("<el-tooltip")
    expect(sensitive).toContain("每页 60")
    // 词条墙为单一 DOM，不再表格/卡片双渲染
    expect(sensitive).not.toContain("<el-table")
  })

  it("敏感词：审计 toast 句式（删除与添加分账）", () => {
    expect(sensitive).toContain("已删除敏感词 · 本次操作已记入审计")
    expect(sensitive).toMatch(/已添加 \$\{result\.added\} 个.*本次操作已记入审计/s)
  })
})

describe("行点击开 Drawer 结构契约（el-table @row-click）", () => {
  it("BatchView / AuditView / CallbackView 表格声明 @row-click", () => {
    expect(source("src/views/BatchView.vue")).toMatch(/<el-table[^>]*@row-click="openBatch"/s)
    expect(source("src/views/AuditView.vue")).toMatch(/<el-table[^>]*@row-click="detail"/s)
    expect(source("src/views/CallbackView.vue")).toMatch(/<el-table[^>]*@row-click="openDetail"/s)
  })
})

describe("登录页结构契约", () => {
  const login = source("src/views/LoginView.vue")
  const theme = readThemeCss()

  it("桌面左白鹭右 480px 会话窗口，860px 以下收起白鹭并由对话名片承载", () => {
    expect(theme).toMatch(/\.login-screen\s*\{[^}]*grid-template-columns:\s*1fr 480px 8vw/s)
    expect(theme).toMatch(/@media \(max-width: 860px\)\s*\{[^@]*\.login-hero\s*\{\s*display:\s*none/s)
    expect(login).toContain('class="login-intro"')
    expect(login).toContain('role="log"')
  })

  it("认证源以快捷回复显式选择，目录固定画出 local / AD，并记住上次显式选择", () => {
    expect(login).toContain("`provider-${chip.action.code}`")
    expect(login).toContain('"本地账号"')
    expect(login).toContain('"AD 账号"')
    expect(login).toContain("rememberLoginProvider(code)")
    expect(login).toContain("recallLoginProvider()")
  })

  it("账号与密码同处一个表单且停放而非移除，密码气泡固定圆点不暴露长度", () => {
    expect(login).toContain('autocomplete="username"')
    expect(login).toMatch(/'new-password' : 'current-password'/)
    expect(theme).toMatch(/\.login-field input\.is-parked\s*\{[^}]*opacity:\s*0/s)
    expect(login).toContain('const SECRET_DOTS = "••••••••"')
  })
})

describe("动效白名单防回退（规范 §6）", () => {
  it("信道条深度轨 900ms / 登录卡入场 480ms / reduced-motion 全关", () => {
    const monitor = source("src/components/ChannelMonitor.vue")
    const theme = readThemeCss()
    const workspace = readWorkspaceCss()
    const styles = `${theme}\n${workspace}`

    expect(monitor).toContain("transition: width 900ms")
    expect(theme).toContain("identity-gate-enter 480ms")
    expect(styles).toContain("@media (prefers-reduced-motion: reduce)")
    expect(styles).toMatch(/animation-duration:\s*0\.01ms\s*!important/)
    expect(styles).toMatch(/transition-duration:\s*0\.01ms\s*!important/)
  })
})
