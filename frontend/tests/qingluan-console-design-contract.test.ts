import { readFileSync } from "node:fs"
import { resolve } from "node:path"

const source = (path: string) => readFileSync(resolve(process.cwd(), path), "utf8")

describe("青鸾 Console 深色监视台设计契约", () => {
  it("使用交接包定义的深色令牌和控制台尺寸", () => {
    const theme = source("src/styles/theme.css")
    const workspace = source("src/styles/workspace.css")

    expect(theme).toContain("--bg: #101814")
    expect(theme).toContain("--panel: #18231e")
    expect(theme).toContain("--panel-2: #141d19")
    expect(theme).toContain("--sink: #0c1512")
    expect(theme).toContain("--hair: rgba(255, 255, 255, 0.08)")
    expect(theme).toContain("--tx-hi: #eaf1ed")
    expect(theme).toContain("color-scheme: dark")
    expect(workspace).not.toContain("--paper: #f6f7f5")
    expect(workspace).not.toContain("--card: #fdfefc")
    expect(workspace).toMatch(/grid-template-columns:\s*216px\s+minmax\(0,\s*1fr\)/)
    expect(workspace).toContain("max-width: 1440px")
  })

  it("保留自包含字体并声明暗色浏览器控件", () => {
    const main = source("src/main.ts")
    const html = source("index.html")
    const theme = source("src/styles/theme.css")
    const workspace = source("src/styles/workspace.css")

    expect(main).toContain('@fontsource/ibm-plex-mono/400.css')
    expect(main).toContain('@fontsource/ibm-plex-mono/500.css')
    expect(main).toContain('@fontsource/ibm-plex-mono/600.css')
    expect(html).toContain('<meta name="color-scheme" content="dark" />')
    expect(html).not.toMatch(/https?:\/\//)
    expect(theme).toContain("--el-disabled-bg-color: #2a3d37")
    expect(workspace).toMatch(/\.el-date-editor\.el-input\s+\.el-input__wrapper[^}]*background:\s*var\(--sink\)/s)
    expect(workspace).toMatch(/\.el-button--primary\.is-disabled[^}]*background:\s*#2a3d37/s)
  })

  it("外壳呈现交接定义的品牌和五组导航语义", () => {
    const app = source("src/App.vue")
    const login = source("src/views/LoginView.vue")
    const passwordChange = source("src/views/PasswordChangeView.vue")

    expect(app).toContain('class="brand-mark" aria-hidden="true">鸾')
    expect(login).toContain('class="login-seal" aria-hidden="true">鸾')
    expect(passwordChange).toContain('class="login-seal" aria-hidden="true">鸾')
    expect(app).toContain("SMS PLATFORM · XTC")
    expect(app).toContain('{ label: "回复查询", path: "/replies"')
    expect(app).toContain('{ label: "应用与密钥", path: "/apps"')
    for (const group of ["概览", "发送", "治理", "管理", "运维"]) {
      expect(app).toContain(`group: "${group}"`)
    }
  })

  it("仪表盘常驻信道监视条并以 10 秒刷新", () => {
    const dashboard = source("src/views/DashboardView.vue")
    const monitor = source("src/components/ChannelMonitor.vue")

    expect(dashboard).toContain("<ChannelMonitor")
    expect(dashboard).toContain("10_000")
    expect(monitor).toContain('data-testid="channel-monitor"')
    expect(monitor).toContain("monitor-stale")
    expect(monitor).toContain("数据暂不可用")
    expect(dashboard).toContain(":qps-rate=\"operations.channel_monitor.qps_rate\"")
    expect(dashboard).toContain(":stale=\"operations.channel_monitor.stale\"")
    expect(monitor).toContain("Redis 运行快照读取失败")
    expect(monitor).not.toContain("未接入当前 API")
    expect(monitor).not.toContain("qpsRate: 200")
    expect(monitor).not.toContain("1 格 = 40 条/秒")
    expect(monitor).toContain("transition: width 900ms")
    expect(monitor).toMatch(/grid-template-columns:\s*auto\s+1fr\s+1fr\s+190px\s+auto/)
    expect(monitor).toMatch(/\.monitor-track\s*\{[^}]*height:\s*6px/s)
    expect(monitor).toMatch(/\.token-grid i\s*\{[^}]*height:\s*14px/s)
    expect(monitor).toMatch(/\.monitor-lane strong,[\s\S]*?\.monitor-qps strong[^}]*font-size:\s*16px/s)
    expect(monitor).toMatch(/\.monitor-degraded[^}]*color:\s*var\(--tx-2\)/s)
  })

  it("仪表盘 KPI 卡片在不同内容量下始终等高对齐", () => {
    const workspace = source("src/styles/workspace.css")

    expect(workspace).toMatch(/\.metric-link\s*\{[^}]*display:\s*flex/s)
    expect(workspace).toMatch(/\.metric-card\s*\{[^}]*display:\s*flex[^}]*flex:\s*1/s)
    expect(workspace).toMatch(/\.metric-card\s+\.el-card__body\s*\{[^}]*flex:\s*1/s)
  })

  it("动态运行参数来自后端事实而不是设计稿默认值", () => {
    const dashboard = source("src/views/DashboardView.vue")
    const send = source("src/views/SendView.vue")
    const apps = source("src/views/AppManagementView.vue")

    expect(dashboard).toContain("operations.value?.balance_alert_threshold")
    expect(dashboard).toContain("告警阈值暂不可用")
    expect(dashboard).not.toContain("告警阈值 10,000")
    expect(send).toContain("result.ui_policy.test_send_max")
    expect(send).toContain("号码上限暂不可用")
    expect(send).not.toContain("最多 5 个号码")
    expect(apps).toContain('item.key === "key_grace_hours"')
    expect(apps).not.toContain("旧 Key 24h 并行有效")
    expect(dashboard).toContain("if (loading.value) return")
    expect(dashboard).toContain('v-loading="loading && !snapshot"')
  })

  it("所有运动在用户要求减少动态时关闭", () => {
    const theme = source("src/styles/theme.css")
    const workspace = source("src/styles/workspace.css")
    const styles = `${theme}\n${workspace}`

    expect(styles).toContain("@media (prefers-reduced-motion: reduce)")
    expect(styles).toMatch(/animation-duration:\s*0\.01ms\s*!important/)
    expect(styles).toMatch(/transition-duration:\s*0\.01ms\s*!important/)
  })

  it("移动断点晚于深色桌面覆盖并恢复单栏工作区", () => {
    const workspace = source("src/styles/workspace.css")
    const darkLayer = workspace.indexOf("青鸾 Console 深色监视台")
    const mobileLayer = workspace.lastIndexOf("@media (max-width: 959px)")

    expect(mobileLayer).toBeGreaterThan(darkLayer)
    expect(workspace.slice(mobileLayer)).toMatch(/\.app-shell\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
  })
})
