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

const selectedProvider = computed(() =>
  session.providers.find((provider) => provider.code === providerCode.value),
)
const providerHint = computed(() => {
  if (providerCode.value === "local") return "本地账号由管理员创建和维护，首次登录需要修改临时密码。"
  if (providerCode.value === "ad") return "使用企业 AD 目录账号；平台不会回退到其他认证源。"
  return "每次登录只使用你明确选择的认证源，不自动回退。"
})
const accountPlaceholder = computed(() =>
  providerCode.value === "ad" ? "请输入企业 AD 账号" : "请输入平台账号",
)

onMounted(async () => {
  try {
    await session.loadProviders()
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
    errorMessage.value = "请选择认证源"
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
        <span class="login-seal" aria-hidden="true">青</span>
        <div>
          <strong>青鸾</strong>
          <small>企业短信运营控制台</small>
        </div>
      </div>

      <div class="login-context">
        <p class="eyebrow">IDENTITY GATE / 身份认证</p>
        <h1 id="login-title">登录控制台</h1>
        <p>先选择认证源，再提交对应账号。登录、退出与权限变更均写入审计日志。</p>
      </div>

      <form @submit.prevent="submit">
        <fieldset class="provider-fieldset" :aria-busy="loadingProviders">
          <legend>选择认证源</legend>
          <div v-if="loadingProviders" class="provider-loading">正在读取可用认证源…</div>
          <div v-else class="provider-options">
            <button
              v-for="provider in session.providers"
              :key="provider.code"
              :class="['provider-option', { selected: providerCode === provider.code }]"
              :data-testid="`provider-${provider.code}`"
              type="button"
              :aria-pressed="providerCode === provider.code"
              @click="providerCode = provider.code"
            >
              <span>{{ provider.name }}</span>
              <small>{{ provider.code === 'local' ? '平台维护' : '企业目录' }}</small>
            </button>
          </div>
          <p class="provider-hint" aria-live="polite">{{ providerHint }}</p>
        </fieldset>

        <label for="login-username">{{ selectedProvider?.name || '账号' }}</label>
        <el-input
          id="login-username"
          v-model="username"
          data-testid="login-username"
          autocomplete="username"
          :placeholder="accountPlaceholder"
          size="large"
        />

        <label for="login-password">密码</label>
        <el-input
          id="login-password"
          v-model="password"
          data-testid="login-password"
          autocomplete="current-password"
          placeholder="请输入密码"
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
          进入控制台
        </el-button>
      </form>

      <div class="login-assurance" aria-label="会话安全说明">
        <span>显式认证源</span>
        <span>Bearer JWT</span>
        <span>操作全量审计</span>
      </div>
      <p class="login-footnote"><span aria-hidden="true"></span> 无开放注册 · 本地账号由管理员创建和维护</p>
    </article>
  </main>
</template>
