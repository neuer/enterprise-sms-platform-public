<script setup lang="ts">
import {
  ElAlert,
  ElButton,
  ElDialog,
  ElForm,
  ElFormItem,
  ElInput,
  ElMessage,
} from "element-plus"
import { computed, ref, watch } from "vue"

import { passwordPolicyRequest, type PasswordPolicy } from "../api/auth"
import { useSessionStore } from "../stores/session"

const props = defineProps<{ modelValue: boolean }>()
const emit = defineEmits<{
  "update:modelValue": [value: boolean]
  changed: []
}>()

const session = useSessionStore()
const currentPassword = ref("")
const newPassword = ref("")
const confirmPassword = ref("")
const submitting = ref(false)
const errorMessage = ref("")
const policy = ref<PasswordPolicy>({
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含三类字符，不能包含用户名",
})

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit("update:modelValue", value),
})

function clearSensitiveFields(): void {
  currentPassword.value = ""
  newPassword.value = ""
  confirmPassword.value = ""
  errorMessage.value = ""
}

watch(
  () => props.modelValue,
  async (opened) => {
    if (!opened) {
      clearSensitiveFields()
      return
    }
    try {
      policy.value = await passwordPolicyRequest()
    } catch {
      // 保留与后端默认规则一致的提示，提交时仍由服务端权威校验。
    }
  },
)

async function submit(): Promise<void> {
  errorMessage.value = ""
  if (!currentPassword.value || !newPassword.value || !confirmPassword.value) {
    errorMessage.value = "请完整填写当前密码和新密码"
    return
  }
  if (newPassword.value !== confirmPassword.value) {
    errorMessage.value = "两次输入的新密码不一致"
    return
  }
  if (currentPassword.value === newPassword.value) {
    errorMessage.value = "新密码不能与当前密码相同"
    return
  }

  submitting.value = true
  try {
    await session.changePassword(currentPassword.value, newPassword.value)
    clearSensitiveFields()
    visible.value = false
    ElMessage.success("密码已修改，请重新登录")
    emit("changed")
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : "密码修改失败"
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <ElDialog
    v-model="visible"
    title="修改登录密码"
    width="min(480px, calc(100vw - 32px))"
    :close-on-click-modal="false"
    :teleported="false"
    @closed="clearSensitiveFields"
  >
    <ElAlert
      :title="policy.description"
      type="info"
      :closable="false"
      show-icon
    />
    <ElForm
      data-testid="daily-password-form"
      class="daily-password-form"
      label-position="top"
      @submit.prevent="submit"
    >
      <ElFormItem label="当前密码">
        <ElInput
          v-model="currentPassword"
          data-testid="daily-current-password"
          type="password"
          autocomplete="current-password"
          show-password
        />
      </ElFormItem>
      <ElFormItem label="新密码">
        <ElInput
          v-model="newPassword"
          data-testid="daily-new-password"
          type="password"
          autocomplete="new-password"
          show-password
        />
      </ElFormItem>
      <ElFormItem label="确认新密码">
        <ElInput
          v-model="confirmPassword"
          data-testid="daily-confirm-password"
          type="password"
          autocomplete="new-password"
          show-password
        />
      </ElFormItem>
      <p v-if="errorMessage" class="daily-password-error" role="alert">{{ errorMessage }}</p>
      <button class="daily-password-native-submit" type="submit" aria-hidden="true" tabindex="-1"></button>
    </ElForm>
    <template #footer>
      <ElButton :disabled="submitting" @click="visible = false">取消</ElButton>
      <ElButton type="primary" :loading="submitting" @click="submit">确认修改</ElButton>
    </template>
  </ElDialog>
</template>

<style scoped>
.daily-password-form {
  margin-top: 18px;
}

.daily-password-error {
  margin: -4px 0 0;
  color: var(--el-color-danger);
  font-size: 13px;
}

.daily-password-native-submit {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
}
</style>
