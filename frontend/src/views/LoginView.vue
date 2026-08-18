<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue"
import { useRouter } from "vue-router"

import PasswordChangeView from "./PasswordChangeView.vue"
import { useSessionStore } from "../stores/session"

const router = useRouter()
const session = useSessionStore()
const providerCode = ref("")
const username = ref("")
const password = ref("")
const loadingProviders = ref(true)
const submitting = ref(false)
const errorMessage = ref("")
const successMessage = ref("")
const pendingChange = ref<{ token: string; expiresAt: number } | null>(null)

const accountPlaceholder = computed(() =>
  providerCode.value === "ad" ? "企业 AD 账号" : "账号",
)
const singleProvider = computed(() => session.providers.length === 1)

/** 登录来源条左侧标记，与系统配置页 LOCAL/AD 语言一致。 */
function providerMark(code: string): string {
  if (code === "local") return "LOCAL"
  if (code === "ad") return "AD"
  return code.toUpperCase()
}

/** 来源条一行区别；仅一个认证源时改为只读确认，不假装还能再选。 */
function providerDescription(code: string): string {
  if (session.providers.length === 1) return "当前唯一可用认证源"
  if (code === "local") return "管理员维护的平台内置账号"
  if (code === "ad") return "通过企业目录验证身份"
  return ""
}

function selectProvider(code: string): void {
  if (singleProvider.value) return
  providerCode.value = code
}

onMounted(async () => {
  try {
    await session.loadProviders()
    // 默认选中服务端返回的第一个认证源；提交仍只走当前选中的认证源，失败不自动回退
    providerCode.value = session.providers[0]?.code ?? ""
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "认证源列表加载失败"
  } finally {
    loadingProviders.value = false
  }
})
onBeforeUnmount(() => {
  pendingChange.value = null
  password.value = ""
})

async function submit() {
  errorMessage.value = ""
  successMessage.value = ""
  if (!providerCode.value) {
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
      <h1 id="login-title" class="sr-only">登录青鸾控制台</h1>
      <div class="login-brand">
        <span class="login-seal" aria-hidden="true">鸾</span>
        <strong>青鸾</strong>
      </div>

      <form @submit.prevent="submit">
        <div class="provider-sources" :aria-busy="loadingProviders">
          <p id="login-sources-label" class="provider-sources-label">身份来源</p>
          <span v-if="loadingProviders" class="provider-loading">正在读取认证源…</span>
          <div
            v-else
            role="radiogroup"
            aria-labelledby="login-sources-label"
            aria-label="认证源"
          >
            <button
              v-for="provider in session.providers"
              :key="provider.code"
              :class="['provider-lane', provider.code, { on: providerCode === provider.code }]"
              :data-testid="`provider-${provider.code}`"
              type="button"
              role="radio"
              :aria-checked="providerCode === provider.code"
              :aria-disabled="singleProvider"
              @click="selectProvider(provider.code)"
            >
              <span class="provider-lane-mark" aria-hidden="true">{{ providerMark(provider.code) }}</span>
              <span class="provider-lane-copy">
                <strong>{{ provider.name }}</strong>
                <small>{{ providerDescription(provider.code) }}</small>
              </span>
              <span class="provider-lane-radio" aria-hidden="true"></span>
            </button>
          </div>
        </div>

        <el-input
          id="login-username"
          v-model="username"
          data-testid="login-username"
          autocomplete="username"
          :placeholder="accountPlaceholder"
          aria-label="账号"
          size="large"
        />

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
