<script setup lang="ts">
import { formatNumber } from "./lib/format"
import { ElConfigProvider, ElMessage } from "element-plus"
import zhCn from "element-plus/es/locale/lang/zh-cn"
import { computed, defineAsyncComponent, nextTick, onBeforeMount, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { SESSION_CLEARING_EVENT } from "./api/sessionEvents"
import { useRoute, useRouter } from "vue-router"
import { getBalance } from "./api/dashboard"
import { usePolling } from "./composables/usePolling"
import { useLatestRead } from "./composables/useLatestRead"
import { getTheme, toggleTheme, type ThemeMode } from "./lib/theme"
import appRouter, { deriveNavigation } from "./router"
import { useApprovalBadgeStore } from "./stores/approvalBadge"
import { applyIncomingSessionSignal, redirectToLoginIfCleared, runAppLogout } from "./api/sessionNavigation"
import { useSessionStore } from "./stores/session"

// 日常改密弹窗只在认证壳内点开：异步加载，登录首屏不背 ElDialog/ElForm 的组件代码。
const DailyPasswordChangeDialog = defineAsyncComponent(() => import("./components/DailyPasswordChangeDialog.vue"))

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const approvalBadge = useApprovalBadgeStore()
const publicRoute = computed(() => Boolean(route.meta.public))
const pageTitle = computed(() => String(route.meta.title || "仪表盘"))
const pageGroup = computed(() => String(route.meta.group || "概览"))
const navigationOpen = ref(false)
const drawerQuery = typeof window.matchMedia === "function" ? window.matchMedia("(max-width: 959px)") : null
const mobileDrawer = ref(drawerQuery?.matches ?? false)
const sidebarRoot = ref<HTMLElement | null>(null)
const navCloseButton = ref<HTMLButtonElement | null>(null)
const navToggleButton = ref<HTMLButtonElement | null>(null)
let navTrigger: HTMLElement | null = null
const lockWorkspace = computed(() => mobileDrawer.value && navigationOpen.value)
const lockSidebar = computed(() => mobileDrawer.value && !navigationOpen.value)
const currentBalance = ref<number | null>(null)
const passwordDialogOpen = ref(false)
const themeMode = ref<ThemeMode>(getTheme())

/** 一键切换深色/明亮模式；令牌在 theme.css，图表监听 sms:theme-change 重建配色。 */
function switchTheme(): void {
  themeMode.value = toggleTheme()
}
const authenticatedShell = computed(() => !publicRoute.value && session.isAuthenticated)
const dashboardRoute = computed(() => route.path === "/dashboard")
const approvalRoute = computed(() => route.path === "/approvals")
const approverRole = computed(() => session.canDecrypt)
const balanceLabel = computed(() =>
  currentBalance.value === null ? "厂商余额暂无数据" : `厂商余额 ${formatNumber(currentBalance.value)} 计费条`,
)
// 侧栏导航派生自路由元数据（router/index.ts 的 meta.nav/group/title/roles），
// 菜单与路由守卫共用单一事实源，新增页面只改路由表。
const navigation = deriveNavigation(appRouter.options.routes)

const visibleNavigation = computed(() =>
  navigation
    .map((section) => ({
      ...section,
      items: section.items.filter((item) => !item.roles || Boolean(session.role && item.roles.includes(session.role))),
    }))
    .filter((section) => section.items.length > 0),
)

const resourceLoadFailed = ref(false)
function handleResourceLoadFailed(): void {
  resourceLoadFailed.value = true
}
function handleResourceLoadRecovered(): void {
  resourceLoadFailed.value = false
}
function reloadResources(): void {
  window.location.reload()
}

function handleUnauthorized(): void {
  if (!session.isAuthenticated && route.path !== "/login") void router.replace("/login")
}

function handleReauthenticationRequired(): void {
  if (session.isAuthenticated) return
  ElMessage.warning("AD 会话已到期，请重新登录")
  if (route.path !== "/login") void router.replace("/login")
}

function handleSessionStorageSignal(event: StorageEvent): void {
  const changed = applyIncomingSessionSignal(session, event)
  if (changed && route.path !== "/login") void router.replace("/login")
}

function handleSessionRefreshed(): void {
  session.restore()
}

function handlePageShow(event: PageTransitionEvent): void {
  if (!event.persisted) return
  void session.revalidateOnResume().then((ok) => {
    if (!ok && route.path !== "/login") void router.replace("/login")
  })
}

function closeNavigation(): void {
  navigationOpen.value = false
}

function focusPageHeading(): void {
  const heading = document.querySelector<HTMLElement>("main h1")
  if (!heading) return
  heading.tabIndex = -1
  heading.focus()
}

function closeNavigationFromUser(): void {
  const trigger = navTrigger
  navigationOpen.value = false
  void nextTick(() => {
    if (
      trigger &&
      trigger.isConnected &&
      !trigger.hasAttribute("disabled") &&
      !trigger.closest("[inert]") &&
      mobileDrawer.value
    ) {
      trigger.focus()
      return
    }
    focusPageHeading()
  })
}

function openNavigation(event: MouseEvent): void {
  if (navigationOpen.value) {
    closeNavigationFromUser()
    return
  }
  navTrigger = event.currentTarget instanceof HTMLElement ? event.currentTarget : navToggleButton.value
  navigationOpen.value = true
}

function handleDrawerChange(event: MediaQueryListEvent): void {
  mobileDrawer.value = event.matches
  if (!event.matches) navigationOpen.value = false
}

function handleNavSessionClear(): void {
  navigationOpen.value = false
  navTrigger = null
  approvalBadge.clear()
}

function focusableNavItems(): HTMLElement[] {
  const root = sidebarRoot.value
  if (!root) return []
  return [...root.querySelectorAll<HTMLElement>("a[href], button:not([disabled])")].filter(
    (element) => !element.hasAttribute("disabled") && element.tabIndex !== -1 && !element.closest("[inert]"),
  )
}

function visibleOverlayOpen(): boolean {
  return [...document.querySelectorAll<HTMLElement>(".el-overlay")].some((overlay) => {
    if (overlay.getAttribute("aria-hidden") === "true") return false
    return overlay.style.display !== "none" && overlay.style.visibility !== "hidden"
  })
}

function handleKeydown(event: KeyboardEvent): void {
  if (!navigationOpen.value) return
  if (event.key === "Escape") {
    if (visibleOverlayOpen()) return
    event.preventDefault()
    closeNavigationFromUser()
    return
  }
  if (!mobileDrawer.value || event.key !== "Tab") return
  const items = focusableNavItems()
  if (!items.length) return
  const first = items[0]
  const last = items[items.length - 1]
  const active = document.activeElement
  const root = sidebarRoot.value
  if (event.shiftKey && (active === first || !root?.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || !root?.contains(active))) {
    event.preventDefault()
    first.focus()
  }
}

function handleDashboardBalance(event: Event): void {
  const balance = (event as CustomEvent<{ currentBalance?: number | null }>).detail?.currentBalance
  if (authenticatedShell.value && session.role === "admin" && (balance === null || Number.isFinite(balance))) {
    currentBalance.value = balance ?? null
  }
}

const balanceRead = useLatestRead()

async function refreshBalance(): Promise<void> {
  const signal = balanceRead.start()
  try {
    const snapshot = await getBalance(signal)
    if (!signal.aborted && authenticatedShell.value && session.role === "admin") {
      currentBalance.value = snapshot.current_balance
    }
  } catch {
    if (!signal.aborted) currentBalance.value = null
  }
}

/** 顶栏余额轮询：未登录或在仪表盘路由（仪表盘自带 10s 快照）时暂停，恢复时立即补刷一次。 */
const balancePolling = usePolling(refreshBalance, {
  intervalMs: 60_000,
  immediate: true,
  enabled: computed(() => authenticatedShell.value && session.role === "admin" && !dashboardRoute.value),
})

function syncApprovalBadgePolling(): void {
  // 审批页自己刷新角标；全局轮询在该路由暂停。筛选列表不得把局部计数写进导航。
  if (authenticatedShell.value && approverRole.value && !approvalRoute.value) {
    approvalBadge.start()
  } else {
    approvalBadge.stop()
    if (!authenticatedShell.value || !approverRole.value) approvalBadge.clear()
  }
}

watch([authenticatedShell, dashboardRoute, () => session.role], () => {
  if (!authenticatedShell.value || session.role !== "admin" || dashboardRoute.value) balanceRead.cancel()
  if (!authenticatedShell.value || session.role !== "admin") currentBalance.value = null
})
watch(
  () => route.fullPath,
  async () => {
    const wasOpen = navigationOpen.value && mobileDrawer.value
    closeNavigation()
    navTrigger = null
    if (!wasOpen) return
    await nextTick()
    focusPageHeading()
  },
)
watch(lockWorkspace, async (locked) => {
  if (!locked) return
  await nextTick()
  navCloseButton.value?.focus()
})
watch(
  authenticatedShell,
  (authed) => {
    if (!authed) currentBalance.value = null
  },
  { immediate: true },
)
watch([authenticatedShell, approverRole, approvalRoute], syncApprovalBadgePolling, { immediate: true })

onBeforeMount(() => {
  window.addEventListener("sms:dashboard-balance", handleDashboardBalance)
})

onMounted(() => {
  window.addEventListener("sms:workspace-load-error", handleResourceLoadFailed)
  window.addEventListener("sms:workspace-load-recovered", handleResourceLoadRecovered)
  window.addEventListener("sms:unauthorized", handleUnauthorized)
  window.addEventListener("sms:reauth-required", handleReauthenticationRequired)
  window.addEventListener("sms:session-refreshed", handleSessionRefreshed)
  window.addEventListener("storage", handleSessionStorageSignal)
  window.addEventListener("pageshow", handlePageShow)
  window.addEventListener("keydown", handleKeydown, true)
  window.addEventListener(SESSION_CLEARING_EVENT, handleNavSessionClear)
  drawerQuery?.addEventListener("change", handleDrawerChange)
  balancePolling.start()
})
onBeforeUnmount(() => {
  window.removeEventListener("sms:workspace-load-error", handleResourceLoadFailed)
  window.removeEventListener("sms:workspace-load-recovered", handleResourceLoadRecovered)
  window.removeEventListener("sms:unauthorized", handleUnauthorized)
  window.removeEventListener("sms:reauth-required", handleReauthenticationRequired)
  window.removeEventListener("sms:session-refreshed", handleSessionRefreshed)
  window.removeEventListener("storage", handleSessionStorageSignal)
  window.removeEventListener("pageshow", handlePageShow)
  window.removeEventListener("keydown", handleKeydown, true)
  window.removeEventListener(SESSION_CLEARING_EVENT, handleNavSessionClear)
  drawerQuery?.removeEventListener("change", handleDrawerChange)
  window.removeEventListener("sms:dashboard-balance", handleDashboardBalance)
  approvalBadge.stop()
})

async function logout() {
  await runAppLogout({
    logout: () => session.logout(),
    isAuthenticated: () => session.isAuthenticated,
    redirectToLogin: () => redirectToLoginIfCleared(true, router),
    onUnconfirmedRevoke: () => {
      ElMessage.warning("本地会话已清除，但服务端撤销未确认；请勿继续使用当前浏览器")
    },
  })
}

async function handlePasswordChanged(): Promise<void> {
  passwordDialogOpen.value = false
  await router.replace("/login")
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div v-if="resourceLoadFailed" role="alert" class="resource-recovery">
      <p>页面资源加载失败。重新加载后可重试，未提交的内容将被清空。</p>
      <el-button @click="reloadResources">重新加载页面</el-button>
    </div>
    <div v-if="publicRoute" class="public-shell">
      <router-view />
    </div>
    <div v-else :class="['app-shell', { 'navigation-open': navigationOpen }]">
      <aside
        id="primary-navigation"
        ref="sidebarRoot"
        class="sidebar"
        aria-label="应用导航"
        :inert="lockSidebar ? true : undefined"
        :aria-hidden="lockSidebar ? 'true' : undefined"
      >
        <div class="brand" data-testid="brand">
          <span class="brand-mark" aria-hidden="true">鸾</span>
          <div>
            <strong>青鸾</strong>
            <small>SMS PLATFORM · XTC</small>
          </div>
          <button
            ref="navCloseButton"
            class="navigation-close"
            data-testid="navigation-close"
            type="button"
            aria-label="关闭导航"
            @click="closeNavigationFromUser"
            >×</button
          >
        </div>

        <nav aria-label="主导航">
          <section v-for="section in visibleNavigation" :key="section.group" class="nav-section">
            <h2>{{ section.group }}</h2>
            <template v-for="item in section.items" :key="item.path">
              <router-link :to="item.path" class="nav-link" active-class="active" @click="closeNavigation">
                <span class="nav-marker" aria-hidden="true">{{ item.marker }}</span>
                {{ item.label }}
                <span
                  v-if="item.path === '/approvals' && approvalBadge.pending > 0"
                  class="nav-badge"
                  :aria-label="`${approvalBadge.pending} 条待审批`"
                  >{{ approvalBadge.pending }}</span
                >
              </router-link>
            </template>
          </section>
        </nav>

        <p class="sidebar-foot">v1.6 · 安全运行</p>
      </aside>

      <button
        class="navigation-backdrop"
        type="button"
        aria-label="关闭导航"
        tabindex="-1"
        @click="closeNavigationFromUser"
      ></button>

      <div
        class="workspace"
        :inert="lockWorkspace ? true : undefined"
        :aria-hidden="lockWorkspace ? 'true' : undefined"
      >
        <header class="topbar">
          <button
            ref="navToggleButton"
            class="navigation-toggle"
            data-testid="navigation-toggle"
            type="button"
            aria-label="打开主导航"
            aria-controls="primary-navigation"
            :aria-expanded="navigationOpen"
            @click="openNavigation"
          >
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </button>
          <p class="breadcrumb"
            ><span>{{ pageGroup }}</span
            ><b>/</b> {{ pageTitle }}</p
          >

          <div class="operator">
            <button
              class="toolbar-action theme-toggle"
              data-testid="theme-toggle"
              type="button"
              :aria-pressed="themeMode === 'light'"
              :title="themeMode === 'light' ? '切换到深色模式' : '切换到明亮模式'"
              @click="switchTheme"
              >{{ themeMode === "light" ? "☾ 深色" : "☀ 明亮" }}</button
            >
            <span class="balance" :aria-label="balanceLabel"
              >余额 <strong>{{ formatNumber(currentBalance) }}</strong></span
            >
            <span class="operator-name"
              >{{ session.displayName }} <small>{{ session.roleLabel }}</small></span
            >
            <button
              v-if="session.providerCode === 'local'"
              class="toolbar-action"
              data-testid="change-password"
              type="button"
              @click="passwordDialogOpen = true"
              >修改密码</button
            >
            <button class="logout-button" data-testid="logout" type="button" @click="logout">退出</button>
          </div>
        </header>

        <p
          v-if="session.sessionMode === 'access_only'"
          class="access-only-banner"
          data-testid="access-only-banner"
          role="status"
        >
          当前浏览器为短会话模式，页面刷新或会话到期需重新登录。
        </p>
        <main>
          <router-view />
        </main>
      </div>
      <DailyPasswordChangeDialog v-model="passwordDialogOpen" @changed="handlePasswordChanged" />
    </div>
  </el-config-provider>
</template>

<style scoped>
@layer components {
  .resource-recovery {
    position: fixed;
    inset: 12px 12px auto;
    z-index: 10000;
    padding: 16px;
    color: var(--ink);
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 8px;
  }
}
</style>
