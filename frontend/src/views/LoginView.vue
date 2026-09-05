<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { useRouter } from "vue-router"

import PasswordChangeView from "./PasswordChangeView.vue"
import { isSafeSingleTabMode, SAFE_SINGLE_TAB_MESSAGE } from "../api/refreshLock"
import loginMarkUrl from "../assets/brand/login-egret-icon.png"
import { useSessionStore } from "../stores/session"

const router = useRouter()
const session = useSessionStore()
const providerCode = ref("")
const providerHint = ref("")
const username = ref("")
const password = ref("")
const loadingProviders = ref(true)
const submitting = ref(false)
const errorMessage = ref("")
const successMessage = ref("")
const pendingChange = ref<{ token: string; expiresAt: number } | null>(null)
const safeSingleTab = isSafeSingleTabMode()
const safeSingleTabMessage = SAFE_SINGLE_TAB_MESSAGE

/** 本期固定目录：始终画出 local / AD，服务端未返回的标未开通。 */
const PROVIDER_CATALOG = [
  {
    code: "local",
    name: "本地账号",
    description: "管理员维护的平台内置账号",
    offHint: "本地账号尚未开通",
  },
  {
    code: "ad",
    name: "AD 账号",
    description: "通过企业目录验证身份",
    offHint: "企业目录尚未开通",
  },
] as const

const catalog = computed(() => {
  const enabled = new Map(session.providers.map((provider) => [provider.code, provider]))
  return PROVIDER_CATALOG.map((item) => {
    const live = enabled.get(item.code)
    return {
      code: item.code,
      name: live?.name ?? item.name,
      enabled: live !== undefined,
      description: item.description,
      offHint: item.offHint,
    }
  })
})

const accountLabel = computed(() =>
  providerCode.value === "ad" ? "企业 AD 账号" : "账号",
)

let hintTimer = 0

function isEnabled(code: string): boolean {
  return catalog.value.some((item) => item.code === code && item.enabled)
}

function clearProviderHint(): void {
  window.clearTimeout(hintTimer)
  hintTimer = 0
  providerHint.value = ""
}

/** 点未开通的认证源只提示，不改当前 provider_code。 */
function showOffHint(code: string): void {
  const item = catalog.value.find((entry) => entry.code === code)
  providerHint.value = item?.offHint ?? "该认证源尚未开通"
  window.clearTimeout(hintTimer)
  hintTimer = window.setTimeout(() => {
    providerHint.value = ""
    hintTimer = 0
  }, 1600)
}

function selectProvider(code: string): void {
  if (!isEnabled(code)) {
    showOffHint(code)
    return
  }
  clearProviderHint()
  providerCode.value = code
}

onMounted(async () => {
  try {
    await session.loadProviders()
    // 默认选中服务端返回的第一个已知认证源；提交仍只走当前选中的认证源，失败不自动回退
    const known = new Set<string>(PROVIDER_CATALOG.map((item) => item.code))
    providerCode.value = session.providers.find((provider) => known.has(provider.code))?.code ?? ""
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "认证源列表加载失败"
  } finally {
    loadingProviders.value = false
  }
})
onBeforeUnmount(() => {
  clearProviderHint()
  pendingChange.value = null
  password.value = ""
})

async function submit() {
  errorMessage.value = ""
  successMessage.value = ""
  if (!providerCode.value || !isEnabled(providerCode.value)) {
    errorMessage.value = "暂无可用的认证源，请稍后重试"
    return
  }
  if (!username.value.trim() || !password.value) {
    errorMessage.value = "请输入账号和密码"
    return
  }
  submitting.value = true
  try {
    const result = await session.login(providerCode.value, username.value.trim(), password.value)
    if (result.nextAction === "change_password") {
      pendingChange.value = {
        token: result.changeToken,
        expiresAt: result.expiresAt,
      }
      return
    }
    await router.replace("/dashboard")
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "登录失败，请稍后重试"
  } finally {
    password.value = ""
    submitting.value = false
  }
}

function finishInitialPasswordChange(): void {
  pendingChange.value = null
  username.value = ""
  password.value = ""
  successMessage.value = "密码修改成功，请使用新密码重新登录"
}

function invalidateInitialPasswordChange(message: string): void {
  pendingChange.value = null
  password.value = ""
  errorMessage.value = message
}
</script>

<template>
  <PasswordChangeView
    v-if="pendingChange"
    :change-token="pendingChange.token"
    :expires-at="pendingChange.expiresAt"
    @completed="finishInitialPasswordChange"
    @invalid="invalidateInitialPasswordChange"
  />
  <main v-else class="login-screen">
    <article class="login-card" aria-labelledby="login-title">
      <div class="login-brand">
        <img class="login-mark" :src="loginMarkUrl" alt="" width="64" height="64" />
        <h1 id="login-title" class="login-brand-name">企业短信管理平台</h1>
        <i class="login-goldline" aria-hidden="true"></i>
      </div>

      <p
        v-if="safeSingleTab"
        class="login-safe-mode"
        data-testid="login-safe-single-tab"
        role="status"
      >
        {{ safeSingleTabMessage }}
      </p>

      <form @submit.prevent="submit">
        <div class="provider-sources" :aria-busy="loadingProviders">
          <p id="login-sources-label" class="sr-only">身份来源</p>
          <span v-if="loadingProviders" class="provider-loading">正在读取认证源…</span>
          <template v-else>
            <div
              class="provider-switch"
              role="radiogroup"
              aria-labelledby="login-sources-label"
              aria-label="认证源"
            >
              <template v-for="(provider, index) in catalog" :key="provider.code">
                <span v-if="index > 0" class="provider-switch-sep" aria-hidden="true">·</span>
                <div class="provider-switch-item" :class="{ 'is-off': !provider.enabled }">
                  <button
                    :class="['provider-name', { on: providerCode === provider.code }]"
                    :data-testid="`provider-${provider.code}`"
                    type="button"
                    role="radio"
                    :aria-checked="providerCode === provider.code"
                    :aria-disabled="!provider.enabled"
                    :aria-label="provider.enabled ? provider.name : `${provider.name}，未开通`"
                    @click="selectProvider(provider.code)"
                  >
                    {{ provider.name }}
                  </button>
                  <small v-if="!provider.enabled" class="provider-off-note">未开通</small>
                </div>
              </template>
            </div>
            <p v-if="providerHint" class="provider-switch-desc is-hint">{{ providerHint }}</p>
          </template>
        </div>

        <div class="login-field">
          <label class="login-field-label" for="login-username">{{ accountLabel }}</label>
          <el-input
            id="login-username"
            v-model="username"
            data-testid="login-username"
            autocomplete="username"
            :placeholder="accountLabel"
            :aria-label="accountLabel"
            size="large"
          />
        </div>

        <div class="login-field">
          <label class="login-field-label" for="login-password">密码</label>
          <el-input
            id="login-password"
            v-model="password"
            data-testid="login-password"
            autocomplete="current-password"
            placeholder="密码"
            aria-label="密码"
            size="large"
            show-password
            type="password"
          />
        </div>

        <p v-if="errorMessage" class="login-error" role="alert">{{ errorMessage }}</p>
        <p v-if="successMessage" class="login-success" role="status">{{ successMessage }}</p>

        <el-button
          class="login-submit"
          :loading="submitting"
          native-type="submit"
          size="large"
          type="primary"
        >
          登录
        </el-button>
      </form>
    </article>
  </main>
</template>
