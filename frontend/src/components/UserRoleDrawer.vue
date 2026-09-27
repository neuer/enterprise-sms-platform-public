<script setup lang="ts">
import { ElMessage } from "element-plus"
import { ref } from "vue"

import type { UserRole } from "../api/auth"
import { updateUserRole, type ManagedUser } from "../api/users"
import type { AdminStepUpController } from "../composables/useAdminStepUp"
import { errorText } from "../lib/error"
import { ROLE_LABELS } from "../lib/labels"
import { ROLE_DESCRIPTIONS, providerLabel } from "../lib/localAccount"

/** 角色策略抽屉：保存经 step-up 并递增 security_version；成功后发出 saved，由用户页刷新台账。 */
const open = defineModel<boolean>({ default: false })
const props = defineProps<{ user: ManagedUser | null; stepUp: AdminStepUpController }>()
const emit = defineEmits<{ saved: [] }>()

const saving = ref(false)
const roleDraft = ref<UserRole>("viewer")
const overrideDraft = ref(false)

/** 按目标账号回填草稿；本地账号角色恒为人工覆盖。 */
function prepare(user: ManagedUser): void {
  roleDraft.value = user.role
  overrideDraft.value = user.provider_code === "local" ? true : user.role_override
}

async function saveRole(): Promise<void> {
  const selected = props.user
  if (!selected) return
  saving.value = true
  try {
    const roleOverride = selected.provider_code === "local" ? true : overrideDraft.value
    const accountId = selected.account_id
    const role = roleDraft.value
    const saved = await props.stepUp.run(
      {
        operation: "user_role_change",
        target_id: String(accountId),
        parameters: { role, role_override: roleOverride },
      },
      "修改账号角色策略",
      (token) => updateUserRole(accountId, role, roleOverride, token),
    )
    if (!saved) return
    open.value = false
    ElMessage.success("角色策略已更新，既有会话已失效 · 本次操作已记入审计")
    emit("saved")
  } catch (error) {
    ElMessage.error(errorText(error, "角色更新失败"))
  } finally {
    saving.value = false
  }
}

defineExpose({ prepare })
</script>

<template>
  <el-drawer v-model="open" size="min(440px, 92vw)" :teleported="false" class="user-drawer">
    <template #header>
      <div class="user-drawer-head">
        <div class="user-drawer-title">角色策略</div>
        <code>PUT /api/v1/web/admin/users/{{ user?.account_id ?? "—" }}/role · 保存即失效旧会话</code>
      </div>
    </template>
    <template v-if="user">
      <section class="role-subject">
        <span>{{ user.display_name || user.username }}</span>
        <code>{{ user.username }}</code>
        <small>{{ providerLabel(user.provider_code) }} · {{ user.dept || "未分配部门" }}</small>
      </section>
      <el-form label-position="top" class="user-form">
        <el-form-item label="目标角色">
          <el-select v-model="roleDraft" :disabled="user.provider_code === 'ad' && !overrideDraft">
            <el-option v-for="(label, value) in ROLE_LABELS" :key="value" :label="label" :value="value" />
          </el-select>
          <small class="field-rule" data-testid="role-permission">{{ ROLE_DESCRIPTIONS[roleDraft] }}</small>
        </el-form-item>
        <el-form-item label="角色来源">
          <div v-if="user.provider_code === 'ad'" class="override-control">
            <el-switch
              v-model="overrideDraft"
              data-testid="override-switch"
              inline-prompt
              active-text="人工"
              inactive-text="AD"
            />
            <p>{{
              overrideDraft ? "保存后固定为所选角色，后续目录同步不改写。" : "保存后按最近来源组和当前映射恢复角色。"
            }}</p>
          </div>
          <p v-else class="local-role-note">本地账号角色始终由平台管理员维护。</p>
        </el-form-item>
      </el-form>
      <div v-if="user.provider_code === 'ad'" class="role-source-preview">
        <span>最近来源组</span>
        <div class="source-groups">
          <el-tag v-for="group in user.source_groups" :key="group" effect="plain">{{ group }}</el-tag>
          <small v-if="!user.source_groups.length">暂无可用于恢复的目录来源组</small>
        </div>
      </div>
    </template>
    <template #footer>
      <div class="user-editor-foot">
        <small>保存即递增 security_version，该账号全部既有会话立即失效；角色变更与操作人写入审计日志。</small>
        <div>
          <el-button @click="open = false">取消</el-button>
          <el-button type="primary" :loading="saving" :disabled="!user" @click="saveRole">保存角色</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
