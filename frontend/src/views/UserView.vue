<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

import { passwordPolicyRequest, type PasswordPolicy } from "../api/auth"
import {
  createLocalUser,
  listUsers,
  resetLocalPassword,
  revokeUserSessions,
  updateUserRole,
  updateUserStatus,
  type ManagedUser,
  type UserRole,
  type UserSyncStatus,
} from "../api/users"
import EmptyState from "../components/EmptyState.vue"

const users = ref<ManagedUser[]>([])
const total = ref(0)
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")
const selected = ref<ManagedUser | null>(null)
const createDrawerOpen = ref(false)
const roleDrawerOpen = ref(false)
const resetDrawerOpen = ref(false)
const roleDraft = ref<UserRole>("viewer")
const overrideDraft = ref(false)
const resetPasswordDraft = ref("")
const filters = reactive({
  keyword: "",
  providerCode: "",
  role: "" as UserRole | "",
  status: "" as 0 | 1 | "",
  page: 1,
  pageSize: 20,
})
const createForm = reactive({
  username: "",
  display_name: "",
  dept: "",
  role: "viewer" as UserRole,
  temporary_password: "",
})
const passwordPolicy = ref<PasswordPolicy>({
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名",
})

const activeCount = computed(() => users.value.filter((user) => user.status === 1).length)
const localCount = computed(() => users.value.filter((user) => user.provider_code === "local").length)

const roleLabels: Record<UserRole, string> = {
  admin: "系统管理员",
  approver: "审批人",
  operator: "操作员",
  viewer: "只读用户",
}
const roleTag: Record<UserRole, "danger" | "warning" | "primary" | "info"> = {
  admin: "danger",
  approver: "warning",
  operator: "primary",
  viewer: "info",
}
const syncLabels: Record<UserSyncStatus, string> = {
  local: "本地维护",
  synced: "已同步",
  pending: "待同步",
  disabled: "已停用",
}

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function roleLabel(role: UserRole): string {
  return roleLabels[role]
}

function roleTagType(role: UserRole): "danger" | "warning" | "primary" | "info" {
  return roleTag[role]
}

function providerLabel(providerCode: string): string {
  if (providerCode === "local") return "本地账号"
  if (providerCode === "ad") return "AD 账号"
  return providerCode.toUpperCase()
}

function credentialLabel(user: ManagedUser): string {
  if (user.credential_status === "must_change") return "首次登录待改密"
  if (user.credential_status === "active") return "密码有效"
  return "目录认证"
}

function roleOrigin(user: ManagedUser): string {
  if (user.provider_code === "local") return "本地固定"
  return user.role_override ? "人工覆盖" : "跟随 AD"
}

function syncLabel(status: UserSyncStatus): string {
  return syncLabels[status]
}

function localTime(value: string | null): string {
  if (!value) return "—"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  })
    .format(new Date(value))
    .replaceAll("/", "-")
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    const page = await listUsers(filters)
    users.value = page.items
    total.value = page.total
  } catch (error) {
    errorMessage.value = errorText(error, "用户台账加载失败")
  } finally {
    loading.value = false
  }
}

async function loadPolicy(): Promise<void> {
  try {
    passwordPolicy.value = await passwordPolicyRequest()
  } catch {
    // 后端仍会执行同一密码策略；规则接口短暂不可用时保留当前版本的安全缺省文案。
  }
}

function search(): void {
  filters.page = 1
  void load()
}

function resetCreateForm(): void {
  createForm.username = ""
  createForm.display_name = ""
  createForm.dept = ""
  createForm.role = "viewer"
  createForm.temporary_password = ""
}

function openCreate(): void {
  resetCreateForm()
  createDrawerOpen.value = true
}

function closeCreate(): void {
  createDrawerOpen.value = false
  resetCreateForm()
}

async function saveLocalUser(): Promise<void> {
  saving.value = true
  try {
    await createLocalUser({
      username: createForm.username.trim(),
      display_name: createForm.display_name.trim(),
      dept: createForm.dept.trim(),
      role: createForm.role,
      temporary_password: createForm.temporary_password,
    })
    closeCreate()
    ElMessage.success("本地账号已创建，用户首次登录必须修改临时密码")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "本地账号创建失败"))
  } finally {
    saving.value = false
  }
}

function openRole(user: ManagedUser): void {
  selected.value = user
  roleDraft.value = user.role
  overrideDraft.value = user.provider_code === "local" ? true : user.role_override
  roleDrawerOpen.value = true
}

async function saveRole(): Promise<void> {
  if (!selected.value) return
  saving.value = true
  try {
    const roleOverride = selected.value.provider_code === "local" ? true : overrideDraft.value
    await updateUserRole(selected.value.account_id, roleDraft.value, roleOverride)
    roleDrawerOpen.value = false
    ElMessage.success("角色策略已更新，既有会话已失效")
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, "角色更新失败"))
  } finally {
    saving.value = false
  }
}

function openPasswordReset(user: ManagedUser): void {
  selected.value = user
  resetPasswordDraft.value = ""
  resetDrawerOpen.value = true
}

function closePasswordReset(): void {
  resetDrawerOpen.value = false
  resetPasswordDraft.value = ""
}

async function confirmPasswordReset(): Promise<void> {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      `将重置 ${selected.value.display_name || selected.value.username} 的本地密码，并立即吊销现有会话。`,
      "确认重置密码",
      { confirmButtonText: "确认重置", cancelButtonText: "取消", type: "warning" },
    )
    saving.value = true
    await resetLocalPassword(selected.value.account_id, resetPasswordDraft.value)
    closePasswordReset()
    ElMessage.success("临时密码已重置，用户下次登录必须修改密码")
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(errorText(error, "密码重置失败"))
    }
  } finally {
    saving.value = false
  }
}

async function changeStatus(user: ManagedUser): Promise<void> {
  const nextStatus: 0 | 1 = user.status === 1 ? 0 : 1
  const action = nextStatus === 1 ? "启用" : "停用"
  try {
    await ElMessageBox.confirm(
      nextStatus === 0
        ? `停用 ${user.display_name || user.username} 后，该账号将无法登录且现有会话立即失效。`
        : `将重新允许 ${user.display_name || user.username} 登录平台。`,
      `确认${action}账号`,
      { confirmButtonText: action, cancelButtonText: "取消", type: nextStatus === 0 ? "warning" : "info" },
    )
    await updateUserStatus(user.account_id, nextStatus)
    ElMessage.success(`账号已${action}`)
    await load()
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(errorText(error, `账号${action}失败`))
    }
  }
}

async function forceLogout(user: ManagedUser): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `将立即吊销 ${user.display_name || user.username} 的全部现有会话。`,
      "确认强制下线",
      { confirmButtonText: "强制下线", cancelButtonText: "取消", type: "warning" },
    )
    await revokeUserSessions(user.account_id)
    ElMessage.success("用户已强制下线")
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(errorText(error, "强制下线失败"))
    }
  }
}

onMounted(() => {
  void load()
  void loadPolicy()
})
</script>

<template>
  <section class="page-heading user-heading">
    <div>
      <p class="eyebrow">IDENTITY LEDGER / 身份台账</p>
      <h1>用户与角色</h1>
      <p>本地账号由管理员维护；AD 账号在成功登录后进入台账，并按目录组映射角色。</p>
    </div>
    <div class="user-heading-actions">
      <div class="user-pulse" aria-label="当前页账号摘要">
        <span><i class="online"></i>有效 {{ activeCount }}/{{ users.length }}</span>
        <span><i class="local"></i>本地 {{ localCount }}</span>
      </div>
      <el-button data-testid="create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
    </div>
  </section>

  <aside class="account-rules" aria-label="账号与密码规则">
    <div><span>用户名规则</span><p>本地用户名：3–64 位 ASCII 字母、数字、点、下划线或短横线；不区分大小写。</p></div>
    <div><span>密码规则</span><p>{{ passwordPolicy.description }}</p></div>
  </aside>

  <el-card shadow="never" class="user-filter-card">
    <el-form class="user-filter filter-grid" label-position="top" @submit.prevent="search">
      <el-form-item class="filter-span-4" label="搜索用户">
        <el-input v-model="filters.keyword" clearable placeholder="用户名、姓名或部门" />
      </el-form-item>
      <el-form-item class="filter-span-2" label="认证源">
        <el-select v-model="filters.providerCode" clearable placeholder="全部来源">
          <el-option label="本地账号" value="local" />
          <el-option label="AD 账号" value="ad" />
        </el-select>
      </el-form-item>
      <el-form-item class="filter-span-2" label="当前角色">
        <el-select v-model="filters.role" clearable placeholder="全部角色">
          <el-option v-for="(label, value) in roleLabels" :key="value" :label="label" :value="value" />
        </el-select>
      </el-form-item>
      <el-form-item class="filter-span-2" label="账号状态">
        <el-select v-model="filters.status" clearable placeholder="全部状态">
          <el-option label="有效" :value="1" />
          <el-option label="停用" :value="0" />
        </el-select>
      </el-form-item>
      <el-form-item class="user-filter-action filter-actions filter-span-2">
        <el-button type="primary" :loading="loading" native-type="submit">查询</el-button>
      </el-form-item>
    </el-form>
  </el-card>

  <el-alert v-if="errorMessage" :title="errorMessage" type="error" show-icon :closable="false" class="user-error">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

  <el-card shadow="never" class="user-ledger-card">
    <template v-if="users.length || loading">
      <el-table v-loading="loading" :data="users" row-key="account_id" class="user-table">
        <el-table-column label="账号" min-width="200">
          <template #default="{ row }">
            <strong>{{ row.display_name || row.username }}</strong>
            <code>{{ row.username }}</code>
            <div class="identity-tags">
              <el-tag size="small" effect="plain">{{ providerLabel(row.provider_code) }}</el-tag>
              <el-tag size="small" :type="row.credential_status === 'must_change' ? 'warning' : 'info'">{{ credentialLabel(row) }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="部门 / 来源组" min-width="230">
          <template #default="{ row }">
            <span>{{ row.dept || '未分配部门' }}</span>
            <div class="source-groups">
              <el-tag v-for="group in row.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
              <small v-if="!row.source_groups.length">{{ row.provider_code === 'local' ? '本地维护，无目录来源组' : '暂无同步记录' }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="角色策略" min-width="155">
          <template #default="{ row }">
            <el-tag :type="roleTagType(row.role)">{{ roleLabel(row.role) }}</el-tag>
            <small class="role-origin">{{ roleOrigin(row) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="账号 / 同步" min-width="160">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 1 ? 'success' : 'danger'">{{ row.status === 1 ? '账号有效' : '账号停用' }}</el-tag>
            <span class="sync-state" :class="row.sync_status"><i></i>{{ syncLabel(row.sync_status) }}</span>
            <time>{{ localTime(row.last_synced_at) }}</time>
          </template>
        </el-table-column>
        <el-table-column label="操作" min-width="250" fixed="right">
          <template #default="{ row }">
            <div class="user-row-actions">
              <el-button :data-testid="`role-${row.account_id}`" link type="primary" @click="openRole(row)">角色</el-button>
              <el-button v-if="row.provider_code === 'local'" :data-testid="`reset-password-${row.account_id}`" link type="primary" @click="openPasswordReset(row)">重置密码</el-button>
              <el-button :data-testid="`status-${row.account_id}`" link :type="row.status === 1 ? 'danger' : 'success'" @click="changeStatus(row)">{{ row.status === 1 ? '停用' : '启用' }}</el-button>
              <el-button :data-testid="`revoke-${row.account_id}`" link type="danger" @click="forceLogout(row)">下线</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="user-mobile-list">
        <article v-for="user in users" :key="user.account_id">
          <header>
            <div><strong>{{ user.display_name || user.username }}</strong><code>{{ user.username }}</code></div>
            <el-tag size="small" :type="user.status === 1 ? 'success' : 'danger'">{{ user.status === 1 ? '有效' : '停用' }}</el-tag>
          </header>
          <div class="identity-tags">
            <el-tag size="small" effect="plain">{{ providerLabel(user.provider_code) }}</el-tag>
            <el-tag size="small" :type="user.credential_status === 'must_change' ? 'warning' : 'info'">{{ credentialLabel(user) }}</el-tag>
            <span class="sync-state" :class="user.sync_status"><i></i>{{ syncLabel(user.sync_status) }}</span>
          </div>
          <p>{{ user.dept || '未分配部门' }}</p>
          <div class="source-groups">
            <el-tag v-for="group in user.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
            <small v-if="!user.source_groups.length">{{ user.provider_code === 'local' ? '本地维护，无目录来源组' : '暂无同步记录' }}</small>
          </div>
          <footer>
            <span><el-tag :type="roleTag[user.role]" size="small">{{ roleLabels[user.role] }}</el-tag>{{ roleOrigin(user) }}</span>
            <span>
              <el-button :data-testid="`mobile-role-${user.account_id}`" link type="primary" @click="openRole(user)">角色</el-button>
              <el-button v-if="user.provider_code === 'local'" :data-testid="`mobile-reset-password-${user.account_id}`" link type="primary" @click="openPasswordReset(user)">重置密码</el-button>
              <el-button :data-testid="`mobile-status-${user.account_id}`" link :type="user.status === 1 ? 'danger' : 'success'" @click="changeStatus(user)">{{ user.status === 1 ? '停用' : '启用' }}</el-button>
              <el-button :data-testid="`mobile-revoke-${user.account_id}`" link type="danger" @click="forceLogout(user)">下线</el-button>
            </span>
          </footer>
        </article>
      </div>
    </template>
    <div v-else class="user-empty-action">
      <EmptyState title="尚无平台账号" description="本地账号由管理员创建；AD 账号在首次成功登录后进入台账。" />
      <el-button data-testid="empty-create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
    </div>

    <footer class="user-pagination">
      <span>共 {{ total }} 名平台用户</span>
      <el-pagination v-model:current-page="filters.page" :page-size="filters.pageSize" :total="total" layout="prev, pager, next" @current-change="load" />
    </footer>
  </el-card>

  <el-drawer v-model="createDrawerOpen" title="创建本地账号" size="min(460px, 100vw)" :teleported="false" class="account-drawer" @closed="resetCreateForm">
    <p class="drawer-intro">账号创建后立即生效。临时密码不会回显，用户首次登录时必须修改。</p>
    <el-form label-position="top" @submit.prevent="saveLocalUser">
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
          <el-option v-for="(label, value) in roleLabels" :key="value" :label="label" :value="value" />
        </el-select>
      </el-form-item>
      <el-form-item label="临时密码" required>
        <el-input v-model="createForm.temporary_password" data-testid="create-password" type="password" show-password autocomplete="new-password" :maxlength="passwordPolicy.max_length" />
        <small class="field-rule">{{ passwordPolicy.description }}。首次登录必须修改。</small>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="closeCreate">取消</el-button>
      <el-button data-testid="save-local-user" type="primary" :loading="saving" @click="saveLocalUser">创建账号</el-button>
    </template>
  </el-drawer>

  <el-drawer v-model="roleDrawerOpen" title="角色策略" size="min(420px, 100vw)" :teleported="false" class="account-drawer role-drawer">
    <template v-if="selected">
      <section class="role-subject">
        <span>{{ selected.display_name || selected.username }}</span>
        <code>{{ selected.username }}</code>
        <small>{{ providerLabel(selected.provider_code) }} · {{ selected.dept || '未分配部门' }}</small>
      </section>
      <el-form label-position="top">
        <el-form-item label="目标角色">
          <el-select v-model="roleDraft" :disabled="selected.provider_code === 'ad' && !overrideDraft">
            <el-option v-for="(label, value) in roleLabels" :key="value" :label="label" :value="value" />
          </el-select>
        </el-form-item>
        <el-form-item label="角色来源">
          <div v-if="selected.provider_code === 'ad'" class="override-control">
            <el-switch v-model="overrideDraft" data-testid="override-switch" inline-prompt active-text="人工" inactive-text="AD" />
            <p>{{ overrideDraft ? '保存后固定为所选角色，后续目录同步不改写。' : '保存后按最近来源组和当前映射恢复角色。' }}</p>
          </div>
          <p v-else class="local-role-note">本地账号角色始终由平台管理员维护。</p>
        </el-form-item>
      </el-form>
      <div v-if="selected.provider_code === 'ad'" class="role-source-preview">
        <span>最近来源组</span>
        <div class="source-groups">
          <el-tag v-for="group in selected.source_groups" :key="group" effect="plain">{{ group }}</el-tag>
          <small v-if="!selected.source_groups.length">暂无可用于恢复的目录来源组</small>
        </div>
      </div>
    </template>
    <template #footer>
      <el-button @click="roleDrawerOpen = false">取消</el-button>
      <el-button type="primary" :loading="saving" :disabled="!selected" @click="saveRole">保存角色</el-button>
    </template>
  </el-drawer>

  <el-drawer v-model="resetDrawerOpen" title="重置本地密码" size="min(420px, 100vw)" :teleported="false" class="account-drawer" @closed="resetPasswordDraft = ''">
    <template v-if="selected">
      <section class="role-subject">
        <span>{{ selected.display_name || selected.username }}</span>
        <code>{{ selected.username }}</code>
        <small>重置后立即吊销全部会话；用户下次登录必须修改密码。</small>
      </section>
      <el-form label-position="top" @submit.prevent="confirmPasswordReset">
        <el-form-item label="新临时密码" required>
          <el-input v-model="resetPasswordDraft" data-testid="reset-password-input" type="password" show-password autocomplete="new-password" :maxlength="passwordPolicy.max_length" />
          <small class="field-rule">{{ passwordPolicy.description }}</small>
        </el-form-item>
      </el-form>
    </template>
    <template #footer>
      <el-button @click="closePasswordReset">取消</el-button>
      <el-button data-testid="confirm-password-reset" type="danger" :loading="saving" :disabled="!selected" @click="confirmPasswordReset">重置密码</el-button>
    </template>
  </el-drawer>
</template>
