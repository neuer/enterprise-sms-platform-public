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

const accountLabel = computed(() =>
  providerCode.value === "ad" ? "企业 AD 账号" : "账号",
)
const singleProvider = computed(() => session.providers.length === 1)

/** 当前认证源一行说明；仅一个认证源时改为只读确认，不假装还能再选。 */
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
          <p id="login-sources-label" class="sr-only">身份来源</p>
          <span v-if="loadingProviders" class="provider-loading">正在读取认证源…</span>
          <template v-else>
            <div
              class="provider-switch"
              :class="{ solo: singleProvider }"
              role="radiogroup"
              aria-labelledby="login-sources-label"
              aria-label="认证源"
            >
              <template v-for="(provider, index) in session.providers" :key="provider.code">
                <span v-if="index > 0" class="provider-switch-sep" aria-hidden="true">·</span>
                <button
                  :class="['provider-name', { on: providerCode === provider.code }]"
                  :data-testid="`provider-${provider.code}`"
                  type="button"
                  role="radio"
                  :aria-checked="providerCode === provider.code"
                  :aria-disabled="singleProvider"
                  @click="selectProvider(provider.code)"
                >
                  {{ provider.name }}
                </button>
              </template>
            </div>
            <p class="provider-switch-desc">{{ providerDescription(providerCode) }}</p>
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
