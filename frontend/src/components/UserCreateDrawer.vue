<script setup lang="ts">
import { ElMessage } from "element-plus"
import { computed, reactive, ref } from "vue"

import type { PasswordPolicy, UserRole } from "../api/auth"
import { createLocalUser } from "../api/users"
import type { AdminStepUpController } from "../composables/useAdminStepUp"
import { errorText } from "../lib/error"
import { ROLE_LABELS } from "../lib/labels"
import {
  ROLE_DESCRIPTIONS,
  passwordPrechecks,
  passwordProblem,
  usernameProblem,
  type Precheck,
} from "../lib/localAccount"
import { formatDateTime } from "../lib/time"

/**
 * 创建本地账号抽屉：临时密码只存在于本组件表单，随本次请求提交后立即清空，不回显、不外传。
 * 创建管理员走 step-up；成功后发出 created，由用户页刷新台账。
 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{ policy: PasswordPolicy; stepUp: AdminStepUpController }>()
const emit = defineEmits<{ created: [] }>()

const saving = ref(false)
const createForm = reactive({
  username: "",
  display_name: "",
  dept: "",
  role: "viewer" as UserRole,
  temporary_password: "",
})

const createChecks = computed<Precheck[]>(() => [
  {
    key: "username",
    label: "用户名 3–64 位合规",
    ok: createForm.username ? usernameProblem(createForm.username) === null : null,
  },
  ...passwordPrechecks(createForm.temporary_password, createForm.username, props.policy),
])

const createError = computed(() => {
  if (!createForm.username.trim() && !createForm.temporary_password) return ""
  const issue =
    usernameProblem(createForm.username) ??
    (createForm.temporary_password
      ? passwordProblem(createForm.temporary_password, createForm.username, props.policy)
      : null)
  return issue ?? ""
})

const canCreate = computed(
  () =>
    !saving.value &&
    !usernameProblem(createForm.username) &&
    Boolean(createForm.display_name.trim()) &&
    !passwordProblem(createForm.temporary_password, createForm.username, props.policy),
)

function resetCreateForm(): void {
  createForm.username = ""
  createForm.display_name = ""
  createForm.dept = ""
  createForm.role = "viewer"
  createForm.temporary_password = ""
}

function closeCreate(): void {
  open.value = false
  resetCreateForm()
}

async function saveLocalUser(): Promise<void> {
  const username = createForm.username.trim()
  const displayName = createForm.display_name.trim()
  const usernameIssue = usernameProblem(username)
  if (usernameIssue) {
    ElMessage.warning(usernameIssue)
    return
  }
  if (!displayName) {
    ElMessage.warning("请输入显示名称")
    return
  }
  const passwordIssue = passwordProblem(createForm.temporary_password, username, props.policy)
  if (passwordIssue) {
    ElMessage.warning(passwordIssue)
    return
  }
  saving.value = true
  try {
    const payload = {
      username,
      display_name: displayName,
      dept: createForm.dept.trim(),
      role: createForm.role,
      temporary_password: createForm.temporary_password,
    }
    let expiresAt: string | null = null
    try {
      if (payload.role === "admin") {
        const parameters = {
          username: payload.username,
          display_name: payload.display_name,
          dept: payload.dept,
          role: payload.role,
        }
        const saved = await props.stepUp.run(
          { operation: "user_create_admin", target_id: "new", parameters },
          `创建管理员 ${username}`,
          (token) => createLocalUser(payload, token),
        )
        if (!saved) return
        expiresAt = saved.temporary_password_expires_at
      } else {
        expiresAt = (await createLocalUser(payload)).temporary_password_expires_at
      }
    } finally {
      payload.temporary_password = ""
    }
    closeCreate()
    ElMessage.success(
      `本地账号已创建，临时密码有效至 ${formatDateTime(expiresAt)}；首次登录须修改，过期需管理员重置 · 本次操作已记入审计`,
    )
    emit("created")
  } catch (error) {
    ElMessage.error(errorText(error, "本地账号创建失败"))
  } finally {
    saving.value = false
  }
}

defineExpose({ prepare: resetCreateForm })
</script>

<template>
  <el-drawer v-model="open" size="min(440px, 92vw)" :teleported="false" class="user-drawer" @closed="resetCreateForm">
    <template #header>
      <div class="user-drawer-head">
        <div class="user-drawer-title">创建本地账号</div>
        <code>POST /api/v1/web/admin/users/local · 临时密码不回显</code>
      </div>
    </template>
    <el-form label-position="top" class="user-form" @submit.prevent="saveLocalUser">
      <el-form-item label="用户名" required>
        <el-input v-model="createForm.username" data-testid="create-username" autocomplete="off" maxlength="64" />
        <small class="field-rule">3–64 位 ASCII 字母、数字、点、下划线或短横线；不区分大小写，创建后不可修改。</small>
      </el-form-item>
      <el-form-item label="显示名称" required>
        <el-input v-model="createForm.display_name" data-testid="create-display-name" maxlength="128" />
      </el-form-item>
      <el-form-item label="部门">
        <el-input v-model="createForm.dept" maxlength="128" />
      </el-form-item>
      <el-form-item label="角色">
        <el-select v-model="createForm.role">
          <el-option v-for="(label, value) in ROLE_LABELS" :key="value" :label="label" :value="value" />
        </el-select>
        <small class="field-rule" data-testid="create-role-permission">{{ ROLE_DESCRIPTIONS[createForm.role] }}</small>
      </el-form-item>
      <el-form-item label="临时密码" required>
        <el-input
          v-model="createForm.temporary_password"
          data-testid="create-password"
          type="password"
          show-password
          autocomplete="new-password"
          :maxlength="policy.max_length"
        />
        <small class="field-rule">{{ policy.description }}；首次登录必须修改。</small>
      </el-form-item>
    </el-form>
    <div v-if="createForm.username || createForm.temporary_password" class="user-parse" data-testid="create-precheck">
      <span
        v-for="check in createChecks"
        :key="check.key"
        class="user-chip"
        :class="{ 'user-chip-ok': check.ok === true, 'user-chip-bad': check.ok === false }"
        >{{ check.label }}</span
      >
    </div>
    <p v-if="createError" class="user-parse-error">{{ createError }}</p>
    <template #footer>
      <div class="user-editor-foot">
        <small>创建行为与操作人写入审计日志；临时密码只随本次请求提交，不回显、不入库明文。</small>
        <div>
          <el-button @click="closeCreate">取消</el-button>
          <el-button
            data-testid="save-local-user"
            type="primary"
            :disabled="!canCreate"
            :loading="saving"
            @click="saveLocalUser"
            >创建账号</el-button
          >
        </div>
      </div>
    </template>
  </el-drawer>
</template>
