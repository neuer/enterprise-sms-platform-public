<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue"

import {
  AuthApiError,
  initialPasswordChangeRequest,
  passwordPolicyRequest,
  type PasswordPolicy,
} from "../api/auth"

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
  <main class="login-screen password-change-screen">
    <article class="login-card password-change-card" aria-labelledby="password-change-title">
      <div class="login-brand">
        <span class="login-seal" aria-hidden="true">鸾</span>
        <div>
          <strong>青鸾</strong>
          <small>LOCAL CREDENTIAL / 本地凭据</small>
        </div>
      </div>

      <div class="login-context">
        <p class="eyebrow">FIRST SIGN-IN / 首次登录</p>
        <h1 id="password-change-title">首次登录必须修改密码</h1>
        <p>临时密码仅用于建立这次改密会话，不能访问控制台其他功能。</p>
      </div>

      <section class="password-policy" aria-labelledby="password-policy-title">
        <p id="password-policy-title">密码规则</p>
        <strong>{{ policy.description }}</strong>
        <ul>
          <li>长度 {{ policy.min_length }}–{{ policy.max_length }} 位</li>
          <li>至少包含三类字符：大写、小写、数字、特殊字符</li>
          <li v-if="policy.forbid_username">不能包含用户名</li>
        </ul>
      </section>

      <form @submit.prevent="submit">
        <label for="new-password">新密码</label>
        <el-input
          id="new-password"
          v-model="newPassword"
          data-testid="new-password"
          autocomplete="new-password"
          placeholder="请输入符合规则的新密码"
          size="large"
          show-password
          type="password"
        />

        <label for="confirm-password">确认新密码</label>
        <el-input
          id="confirm-password"
          v-model="confirmPassword"
          data-testid="confirm-password"
          autocomplete="new-password"
          placeholder="再次输入新密码"
          size="large"
          show-password
          type="password"
        />

        <p v-if="errorMessage" class="login-error" role="alert">{{ errorMessage }}</p>

        <el-button
          class="login-submit"
          :loading="submitting"
          native-type="submit"
          size="large"
          type="primary"
        >
          保存新密码
        </el-button>
      </form>

      <p class="login-footnote"><span aria-hidden="true"></span> 完成后请使用新密码重新登录</p>
    </article>
  </main>
</template>
