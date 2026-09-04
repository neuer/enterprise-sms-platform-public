<script setup lang="ts">
import "./styles/workspace.css"

import { ElConfigProvider, ElMessage } from "element-plus"
import zhCn from "element-plus/es/locale/lang/zh-cn"
import { computed, onBeforeMount, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { useRoute, useRouter } from "vue-router"
import type { UserRole } from "./api/auth"
import { getDashboard } from "./api/dashboard"
import DailyPasswordChangeDialog from "./components/DailyPasswordChangeDialog.vue"
import { getTheme, toggleTheme, type ThemeMode } from "./lib/theme"
import { useApprovalBadgeStore } from "./stores/approvalBadge"
import { invalidateSessionGeneration } from "./api/sessionGeneration"
import { SESSION_CLEAR_SIGNAL_KEY, useSessionStore } from "./stores/session"
import egretMarkUrl from "./assets/brand/login-egret-watermark.png"

const route = useRoute()
const router = useRouter()
const session = useSessionStore()
const approvalBadge = useApprovalBadgeStore()
const publicRoute = computed(() => Boolean(route.meta.public))
const pageTitle = computed(() => String(route.meta.title || "仪表盘"))
const pageGroup = computed(() => String(route.meta.group || "概览"))
const navigationOpen = ref(false)
const currentBalance = ref<number | null>(null)
const passwordDialogOpen = ref(false)
const themeMode = ref<ThemeMode>(getTheme())

/** 一键切换深色/明亮模式；令牌在 theme.css，图表监听 sms:theme-change 重建配色。 */
function switchTheme(): void {
  themeMode.value = toggleTheme()
}
const authenticatedShell = computed(() => !publicRoute.value && session.isAuthenticated)
const dashboardRoute = computed(() => route.path === "/dashboard")
const approverRole = computed(() => session.role === "approver" || session.role === "admin")
const balanceLabel = computed(() =>
  currentBalance.value === null
    ? "厂商余额暂无数据"
    : `厂商余额 ${currentBalance.value.toLocaleString()} 计费条`,
)
let balanceRefreshTimer: number | undefined

interface NavigationItem {
  label: string
  path: string
  marker: string
  roles?: UserRole[]
}

const navigation: Array<{ group: string; items: NavigationItem[] }> = [
  { group: "概览", items: [
    { label: "仪表盘", path: "/dashboard", marker: "总" },
    { label: "统计报表", path: "/reports", marker: "析" },
  ] },
  { group: "发送", items: [{ label: "人工发送", path: "/send", marker: "发", roles: ["operator", "admin"] }] },
  {
    group: "治理",
    items: [
      { label: "审批中心", path: "/approvals", marker: "审", roles: ["approver", "admin"] },
      { label: "批次列表", path: "/batches", marker: "批" },
      { label: "号码搜索", path: "/messages", marker: "迹" },
      { label: "上行回复", path: "/replies", marker: "回" },
    ],
  },
  {
    group: "管理",
    items: [
      { label: "模板管理", path: "/templates", marker: "模", roles: ["operator", "approver", "admin"] },
      { label: "签名管理", path: "/signs", marker: "签", roles: ["operator", "approver", "admin"] },
      { label: "应用管理", path: "/apps", marker: "应", roles: ["admin"] },
      { label: "黑名单", path: "/blacklist", marker: "黑", roles: ["admin"] },
      { label: "敏感词", path: "/sensitive-words", marker: "敏", roles: ["admin"] },
      { label: "用户与角色", path: "/users", marker: "权", roles: ["admin"] },
      { label: "系统参数", path: "/configs", marker: "参", roles: ["admin"] },
    ],
  },
  {
    group: "运维",
    items: [
      { label: "回调任务", path: "/callbacks", marker: "调", roles: ["admin"] },
      { label: "运维中心", path: "/ops", marker: "运", roles: ["admin"] },
      { label: "安全日报", path: "/security-daily", marker: "安", roles: ["admin"] },
      { label: "审计日志", path: "/audit", marker: "录", roles: ["admin"] },
    ],
  },
]

const visibleNavigation = computed(() =>
  navigation
    .map((section) => ({
      ...section,
      items: section.items.filter(
        (item) => !item.roles || Boolean(session.role && item.roles.includes(session.role)),
      ),
    }))
    .filter((section) => section.items.length > 0),
)

function handleUnauthorized(): void {
  session.clearAllTabs()
  if (route.path !== "/login") void router.replace("/login")
}

function handleReauthenticationRequired(): void {
  session.clearAllTabs()
  ElMessage.warning("AD 会话已到期，请重新登录")
  if (route.path !== "/login") void router.replace("/login")
}

function handleSessionStorageSignal(event: StorageEvent): void {
  if (event.key !== SESSION_CLEAR_SIGNAL_KEY) return
  // 信号只含无凭据时间戳。先推进本页代际并取消在途 Refresh，再清状态。
  invalidateSessionGeneration()
  session.clear()
  if (route.path !== "/login") void router.replace("/login")
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

function handleKeydown(event: KeyboardEvent): void {
  if (event.key === "Escape") closeNavigation()
}

function handleDashboardBalance(event: Event): void {
  const balance = (event as CustomEvent<{ currentBalance?: number | null }>).detail?.currentBalance
  if (authenticatedShell.value && (balance === null || Number.isFinite(balance))) {
    currentBalance.value = balance ?? null
  }
}

async function refreshBalance(): Promise<void> {
  try {
    const snapshot = await getDashboard()
    if (authenticatedShell.value) {
      currentBalance.value = snapshot.operations?.current_balance ?? null
    }
  } catch {
    currentBalance.value = null
  }
}

function stopBalancePolling(): void {
  if (balanceRefreshTimer !== undefined) window.clearInterval(balanceRefreshTimer)
  balanceRefreshTimer = undefined
}

function startBalancePolling(): void {
  stopBalancePolling()
  void refreshBalance()
  balanceRefreshTimer = window.setInterval(() => void refreshBalance(), 60_000)
}

function syncBalancePolling(): void {
  stopBalancePolling()
  if (!authenticatedShell.value) {
    currentBalance.value = null
    return
  }
  if (!dashboardRoute.value) startBalancePolling()
}

function syncApprovalBadgePolling(): void {
  if (authenticatedShell.value && approverRole.value) {
    approvalBadge.start()
  } else {
    approvalBadge.stop()
  }
}

watch(() => route.fullPath, closeNavigation)
watch([authenticatedShell, dashboardRoute], syncBalancePolling, { immediate: true })
watch([authenticatedShell, approverRole], syncApprovalBadgePolling, { immediate: true })

onBeforeMount(() => {
  window.addEventListener("sms:dashboard-balance", handleDashboardBalance)
})

onMounted(() => {
  window.addEventListener("sms:unauthorized", handleUnauthorized)
  window.addEventListener("sms:reauth-required", handleReauthenticationRequired)
  window.addEventListener("sms:session-refreshed", handleSessionRefreshed)
  window.addEventListener("storage", handleSessionStorageSignal)
  window.addEventListener("pageshow", handlePageShow)
  window.addEventListener("keydown", handleKeydown)
})
onBeforeUnmount(() => {
  window.removeEventListener("sms:unauthorized", handleUnauthorized)
  window.removeEventListener("sms:reauth-required", handleReauthenticationRequired)
  window.removeEventListener("sms:session-refreshed", handleSessionRefreshed)
  window.removeEventListener("storage", handleSessionStorageSignal)
  window.removeEventListener("pageshow", handlePageShow)
  window.removeEventListener("keydown", handleKeydown)
  window.removeEventListener("sms:dashboard-balance", handleDashboardBalance)
  stopBalancePolling()
  approvalBadge.stop()
})

async function logout() {
  try {
    await session.logout()
  } catch {
    ElMessage.warning("本地会话已清除，但服务端撤销未确认；请勿继续使用当前浏览器")
  } finally {
    await router.replace("/login")
  }
}

async function handlePasswordChanged(): Promise<void> {
  passwordDialogOpen.value = false
  await router.replace("/login")
}
</script>

<template>
  <el-config-provider :locale="zhCn">
    <div v-if="publicRoute" class="public-shell">
      <!-- 身份门背景：光带（桌面纵贯曲线 / 移动端上半弧光）+ 白鹭水印 -->
      <svg class="login-flow login-flow-desktop" viewBox="0 0 1440 900" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="loginFlow" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0" stop-color="#E0CA76" stop-opacity="0" />
            <stop offset=".2" stop-color="#E0CA76" stop-opacity=".9" />
            <stop offset=".6" stop-color="#E0CA76" stop-opacity=".75" />
            <stop offset="1" stop-color="#E0CA76" stop-opacity="0" />
          </linearGradient>
          <filter id="loginFlowBlurA" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="32" />
          </filter>
          <filter id="loginFlowBlurB" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="12" />
          </filter>
        </defs>
        <path d="M -30 845 C 170 795, 280 700, 420 620 C 560 540, 720 455, 920 355 C 1075 270, 1210 155, 1470 45" fill="none" stroke="url(#loginFlow)" stroke-width="210" opacity=".1" filter="url(#loginFlowBlurA)" />
        <path d="M -30 845 C 170 795, 280 700, 420 620 C 560 540, 720 455, 920 355 C 1075 270, 1210 155, 1470 45" fill="none" stroke="url(#loginFlow)" stroke-width="72" opacity=".12" filter="url(#loginFlowBlurB)" />
      </svg>
      <svg class="login-flow login-flow-mobile" viewBox="0 0 390 844" preserveAspectRatio="none" aria-hidden="true">
        <defs>
          <linearGradient id="loginFlowM" x1="0" y1="1" x2="1" y2="0">
            <stop offset="0" stop-color="#E0CA76" stop-opacity="0" />
            <stop offset=".3" stop-color="#E0CA76" stop-opacity=".9" />
            <stop offset=".7" stop-color="#E0CA76" stop-opacity=".65" />
            <stop offset="1" stop-color="#E0CA76" stop-opacity="0" />
          </linearGradient>
          <filter id="loginFlowBlurC" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="26" />
          </filter>
          <filter id="loginFlowBlurD" x="-30%" y="-30%" width="160%" height="160%">
            <feGaussianBlur stdDeviation="10" />
          </filter>
        </defs>
        <path d="M -20 430 C 80 400, 150 340, 210 270 C 270 200, 320 120, 420 60" fill="none" stroke="url(#loginFlowM)" stroke-width="140" opacity=".1" filter="url(#loginFlowBlurC)" />
        <path d="M -20 430 C 80 400, 150 340, 210 270 C 270 200, 320 120, 420 60" fill="none" stroke="url(#loginFlowM)" stroke-width="48" opacity=".12" filter="url(#loginFlowBlurD)" />
      </svg>
      <img class="login-wm" :src="egretMarkUrl" alt="" aria-hidden="true" />
      <router-view />
    </div>
    <div v-else :class="['app-shell', { 'navigation-open': navigationOpen }]">
      <aside id="primary-navigation" class="sidebar" aria-label="应用导航">
        <div class="brand" data-testid="brand">
          <span class="brand-mark" aria-hidden="true">鸾</span>
          <div>
            <strong>青鸾</strong>
            <small>SMS PLATFORM · XTC</small>
          </div>
          <button class="navigation-close" type="button" aria-label="关闭导航" @click="closeNavigation">×</button>
        </div>

        <nav aria-label="主导航">
          <section v-for="section in visibleNavigation" :key="section.group" class="nav-section">
            <h2>{{ section.group }}</h2>
            <template v-for="item in section.items" :key="item.path">
              <router-link
                :to="item.path"
                class="nav-link"
                active-class="active"
                @click="closeNavigation"
              >
                <span class="nav-marker" aria-hidden="true">{{ item.marker }}</span>
                {{ item.label }}
                <span
                  v-if="item.path === '/approvals' && approvalBadge.pending > 0"
                  class="nav-badge"
                  :aria-label="`${approvalBadge.pending} 条待审批`"
                >{{ approvalBadge.pending }}</span>
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
        @click="closeNavigation"
      ></button>

      <div class="workspace">
        <header class="topbar">
          <button
            class="navigation-toggle"
            data-testid="navigation-toggle"
            type="button"
            aria-label="打开主导航"
            aria-controls="primary-navigation"
            :aria-expanded="navigationOpen"
            @click="navigationOpen = !navigationOpen"
          >
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
            <span aria-hidden="true"></span>
          </button>
          <p class="breadcrumb"><span>{{ pageGroup }}</span><b>/</b> {{ pageTitle }}</p>

          <div class="operator">
            <button
              class="toolbar-action theme-toggle"
              data-testid="theme-toggle"
              type="button"
              :aria-pressed="themeMode === 'light'"
              :title="themeMode === 'light' ? '切换到深色模式' : '切换到明亮模式'"
              @click="switchTheme"
            >{{ themeMode === 'light' ? '☾ 深色' : '☀ 明亮' }}</button>
            <span class="balance" :aria-label="balanceLabel">余额 <strong>{{ currentBalance?.toLocaleString() ?? '—' }}</strong></span>
            <span class="operator-name">{{ session.displayName }} <small>{{ session.roleLabel }}</small></span>
            <button
              v-if="session.providerCode === 'local'"
              class="toolbar-action"
              data-testid="change-password"
              type="button"
              @click="passwordDialogOpen = true"
            >修改密码</button>
            <button class="logout-button" data-testid="logout" type="button" @click="logout">退出</button>
          </div>
        </header>

        <main>
          <router-view />
        </main>
      </div>
      <DailyPasswordChangeDialog
        v-model="passwordDialogOpen"
        @changed="handlePasswordChanged"
      />
    </div>
  </el-config-provider>
</template>
