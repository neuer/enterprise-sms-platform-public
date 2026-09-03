<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"

import {
  AuthApiError,
  initialPasswordChangeRequest,
  passwordPolicyRequest,
  type PasswordPolicy,
} from "../api/auth"
import loginMarkUrl from "../assets/brand/login-egret-icon.png"

const props = defineProps<{ changeToken: string; expiresAt: number }>()
const emit = defineEmits<{
  completed: []
  invalid: [message: string]
}>()
const newPassword = ref("")
const confirmPassword = ref("")
const submitting = ref(false)
const errorMessage = ref("")
const policy = ref<PasswordPolicy>({
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名",
})
let expiryTimer: number | undefined

onMounted(async () => {
  const remaining = props.expiresAt - Date.now()
  if (!props.changeToken || !Number.isFinite(remaining) || remaining <= 0) {
    emit("invalid", "改密会话已过期，请重新登录")
    return
  }
  expiryTimer = window.setTimeout(
    () => emit("invalid", "改密会话已过期，请重新登录"),
    remaining,
  )
  try {
    policy.value = await passwordPolicyRequest()
  } catch {
    // 保留与服务端相同的内置规则文案；提交仍由服务端做权威校验。
  }
})
onBeforeUnmount(() => {
  if (expiryTimer !== undefined) window.clearTimeout(expiryTimer)
  newPassword.value = ""
  confirmPassword.value = ""
})

async function submit() {
  errorMessage.value = ""
  if (!props.changeToken || props.expiresAt <= Date.now()) {
    emit("invalid", "改密会话已过期，请重新登录")
    return
  }
  if (!newPassword.value || !confirmPassword.value) {
    errorMessage.value = "请输入并确认新密码"
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    errorMessage.value = "两次输入的密码不一致"
    return
  }
  submitting.value = true
  try {
    await initialPasswordChangeRequest(props.changeToken, newPassword.value)
    newPassword.value = ""
    confirmPassword.value = ""
    emit("completed")
  } catch (error) {
    if (error instanceof AuthApiError && error.status === 401) {
      emit("invalid", error.message)
      return
    }
    errorMessage.value =
      error instanceof AuthApiError && error.status >= 500
        ? "密码修改未提交，请稍后使用当前改密会话重试"
        : error instanceof Error
          ? error.message
          : "密码修改失败，请重新登录"
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <main class="login-screen">
    <article class="login-card" aria-labelledby="password-change-title">
      <div class="login-brand">
        <img class="login-mark" :src="loginMarkUrl" alt="" width="64" height="64" />
        <strong class="login-brand-name">企业短信管理平台</strong>
        <i class="login-goldline" aria-hidden="true"></i>
      </div>

      <h1 id="password-change-title" class="mode-title">设置新密码</h1>

      <form @submit.prevent="submit">
        <el-input
          id="new-password"
          v-model="newPassword"
          data-testid="new-password"
          autocomplete="new-password"
          placeholder="新密码"
          aria-label="新密码"
          size="large"
          show-password
          type="password"
        />

        <el-input
          id="confirm-password"
          v-model="confirmPassword"
          data-testid="confirm-password"
          autocomplete="new-password"
          placeholder="确认新密码"
          aria-label="确认新密码"
          size="large"
          show-password
          type="password"
        />

        <p class="password-policy">密码要求：{{ policy.description }}</p>

        <p v-if="errorMessage" class="login-error" role="alert">{{ errorMessage }}</p>

        <el-button
          class="login-submit"
          :loading="submitting"
          native-type="submit"
          size="large"
          type="primary"
        >
          确认修改
        </el-button>
      </form>
    </article>
  </main>
</template>
