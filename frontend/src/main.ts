import "@fontsource/ibm-plex-mono/400.css"
import "@fontsource/ibm-plex-mono/500.css"
import "@fontsource/ibm-plex-mono/600.css"
import "@fontsource-variable/noto-sans-sc/index.css"
import "@fontsource-variable/noto-serif-sc/index.css"
import "element-plus/theme-chalk/base.css"
import "element-plus/theme-chalk/el-button.css"
import "element-plus/theme-chalk/el-config-provider.css"
import "element-plus/theme-chalk/el-icon.css"
import "element-plus/theme-chalk/el-input.css"
import "element-plus/theme-chalk/el-message.css"
import "./styles/theme.css"

import { ElButton, ElConfigProvider, ElInput } from "element-plus"
import { createPinia } from "pinia"
import { createApp } from "vue"

import App from "./App.vue"
import { initTheme } from "./lib/theme"
import router, { installAuthGuard } from "./router"
import { useSessionStore } from "./stores/session"

// index.html 内联脚本已先行写入 data-theme，这里幂等兜底（如偏好被外部改动）。
initTheme()

const pinia = createPinia()
useSessionStore(pinia).restore()
// 守卫会等待这次恢复；restoreFromCookie 内部消化所有异常，只以布尔值收敛。
// 无 Web Locks 时按 D101 跳过 Cookie 恢复，刷新后必须重新登录。
const sessionReady = useSessionStore(pinia).restoreFromCookie()
installAuthGuard(router, pinia, sessionReady)

const application = createApp(App).use(pinia).use(router)
// 公开壳（登录/首次改密）只需最小组件集；其余组件与样式随首个非公开路由
// 一次性注册（见下方守卫与 src/element-workspace.ts），登录页不背工作区组件。
for (const plugin of [ElButton, ElConfigProvider, ElInput]) application.use(plugin)
application.mount("#app")

// 认证区 Element 注册必须在任何懒视图渲染前完成：beforeEach 里 await 动态 import
// 是确定性时序——懒路由组件只在全部守卫放行后才加载。注册在挂载后调用仍有效
//（全局组件在渲染时解析）。该守卫须注册在 auth 守卫之后：未登录跳转 /login 的
// 导航会被前者取消，不会白注册一次。
let workspaceElement: Promise<void> | null = null
router.beforeEach(async (to) => {
  if (to.meta.public) return true
  workspaceElement ??= import("./element-workspace").then((module) => {
    module.registerWorkspaceElement(application)
  })
  await workspaceElement
  return true
})
