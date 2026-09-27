<script setup lang="ts">
import { ElMessage } from "element-plus"
import { computed, ref } from "vue"

import type { PasswordPolicy } from "../api/auth"
import { resetLocalPassword, type ManagedUser } from "../api/users"
import type { AdminStepUpController } from "../composables/useAdminStepUp"
import { useConfirmActions } from "../lib/confirm"
import { errorText } from "../lib/error"
import { passwordPrechecks, passwordProblem, type Precheck } from "../lib/localAccount"
import { formatDateTime } from "../lib/time"

/**
 * 重置本地密码抽屉：新临时密码只存在于本组件草稿，确认并提交后立即清空，不回显。
 * 重置经审计确认与 step-up；成功后发出 reset，由用户页刷新台账。
 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{ user: ManagedUser | null; policy: PasswordPolicy; stepUp: AdminStepUpController }>()
const emit = defineEmits<{ reset: [] }>()
const { confirmAuditedAction } = useConfirmActions()

const saving = ref(false)
const resetPasswordDraft = ref("")

const resetChecks = computed<Precheck[]>(() =>
  passwordPrechecks(resetPasswordDraft.value, props.user?.username ?? "", props.policy),
)

const canReset = computed(
  () =>
    !saving.value &&
    Boolean(resetPasswordDraft.value) &&
    !passwordProblem(resetPasswordDraft.value, props.user?.username ?? "", props.policy),
)

function clearDraft(): void {
  resetPasswordDraft.value = ""
}

function closePasswordReset(): void {
  open.value = false
  clearDraft()
}

async function confirmPasswordReset(): Promise<void> {
  const target = props.user
  if (!target) return
  const draft = resetPasswordDraft.value
  const passwordIssue = passwordProblem(draft, target.username, props.policy)
  if (passwordIssue) {
    ElMessage.warning(passwordIssue)
    return
  }
  if (
    !(await confirmAuditedAction({
      isCurrent: () => props.user === target && resetPasswordDraft.value === draft && open.value,
      title: "确认重置密码",
      body: `将重置 ${target.display_name || target.username} 的本地密码，并立即吊销现有会话；用户下次登录必须修改密码。`,
      auditNote: "重置行为、操作人与对象 account_id 将写入审计日志；临时密码不回显。",
      confirmText: "确认重置",
    }))
  )
    return
  try {
    saving.value = true
    const accountId = target.account_id
    let password = draft
    let expiresAt: string | null = null
    try {
      const saved = await props.stepUp.run(
        { operation: "user_password_reset", target_id: String(accountId), parameters: { action: "reset" } },
        "重置账号临时密码",
        (token) => resetLocalPassword(accountId, password, token),
      )
      if (!saved) return
      expiresAt = saved.temporary_password_expires_at
    } finally {
      password = ""
    }
    closePasswordReset()
    ElMessage.success(
      `临时密码已重置，有效至 ${formatDateTime(expiresAt)}；首次登录须修改，过期需管理员重置 · 本次操作已记入审计`,
    )
    emit("reset")
  } catch (error) {
    ElMessage.error(errorText(error, "密码重置失败"))
  } finally {
    saving.value = false
  }
}

defineExpose({ prepare: clearDraft })
</script>

<template>
  <el-drawer v-model="open" size="min(440px, 92vw)" :teleported="false" class="user-drawer" @closed="clearDraft">
    <template #header>
      <div class="user-drawer-head">
        <div class="user-drawer-title">重置本地密码</div>
        <code>POST /api/v1/web/admin/users/{{ user?.account_id ?? "—" }}/password/reset · 重置即吊销全部会话</code>
      </div>
    </template>
    <template v-if="user">
      <section class="role-subject">
        <span>{{ user.display_name || user.username }}</span>
        <code>{{ user.username }}</code>
        <small>重置后立即吊销全部会话；用户下次登录必须修改密码。</small>
      </section>
      <el-form label-position="top" class="user-form" @submit.prevent="confirmPasswordReset">
        <el-form-item label="新临时密码" required>
          <el-input
            v-model="resetPasswordDraft"
            data-testid="reset-password-input"
            type="password"
            show-password
            autocomplete="new-password"
            :maxlength="policy.max_length"
          />
          <small class="field-rule">{{ policy.description }}。</small>
        </el-form-item>
      </el-form>
      <div v-if="resetPasswordDraft" class="user-parse" data-testid="reset-precheck">
        <span
          v-for="check in resetChecks"
          :key="check.key"
          class="user-chip"
          :class="{ 'user-chip-ok': check.ok === true, 'user-chip-bad': check.ok === false }"
          >{{ check.label }}</span
        >
      </div>
    </template>
    <template #footer>
      <div class="user-editor-foot">
        <small>重置行为与操作人写入审计日志；临时密码不回显。</small>
        <div>
          <el-button @click="closePasswordReset">取消</el-button>
          <el-button
            data-testid="confirm-password-reset"
            type="danger"
            :disabled="!canReset"
            :loading="saving"
            @click="confirmPasswordReset"
            >重置密码</el-button
          >
        </div>
      </div>
    </template>
  </el-drawer>
</template>
