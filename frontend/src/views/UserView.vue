<script setup lang="ts">
import AdminStepUpDialog from "../components/AdminStepUpDialog.vue"
import { useAdminStepUp } from "../composables/useAdminStepUp"
const adminStepUp = useAdminStepUp()
import { ElMessage } from "element-plus"
import { computed, onMounted, reactive, ref } from "vue"

import { passwordPolicyRequest, type PasswordPolicy, type UserRole } from "../api/auth"
import { listUsers, revokeUserSessions, updateUserStatus, type ManagedUser, type UserSyncStatus } from "../api/users"
import EmptyState from "../components/EmptyState.vue"
import FilterSeg from "../components/FilterSeg.vue"
import ListPagination from "../components/ListPagination.vue"
import MobileFilterToggle from "../components/MobileFilterToggle.vue"
import UserCreateDrawer from "../components/UserCreateDrawer.vue"
import UserPasswordResetDrawer from "../components/UserPasswordResetDrawer.vue"
import UserRoleDrawer from "../components/UserRoleDrawer.vue"

import LoadErrorAlert from "../components/LoadErrorAlert.vue"
import { usePagedList } from "../composables/usePagedList"
import { useLatestRead } from "../composables/useLatestRead"
import { useConfirmActions } from "../lib/confirm"
const { confirmAuditedAction } = useConfirmActions()
import { errorText } from "../lib/error"
import { DEFAULT_PAGE_SIZE, ROLE_LABELS, toOptions } from "../lib/labels"
import { providerLabel } from "../lib/localAccount"
import { formatDateTime } from "../lib/time"

// 行级账号动作在途守卫：进入即置位（confirm 之前），拦截确认框期间的重复点击。
const userActionBusyId = ref<number | null>(null)
const selected = ref<ManagedUser | null>(null)
const createDrawerOpen = ref(false)
const roleDrawerOpen = ref(false)
const resetDrawerOpen = ref(false)
const createDrawer = ref<InstanceType<typeof UserCreateDrawer> | null>(null)
const roleDrawer = ref<InstanceType<typeof UserRoleDrawer> | null>(null)
const resetDrawer = ref<InstanceType<typeof UserPasswordResetDrawer> | null>(null)
const filters = reactive({
  keyword: "",
  providerCode: "",
  role: "" as UserRole | "",
  status: "" as 0 | 1 | "",
  pageSize: DEFAULT_PAGE_SIZE,
})
const filtersCollapsed = ref(true)
const passwordPolicy = ref<PasswordPolicy>({
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名；服务端检查常见或泄露密码",
})

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

const providerOptions = [
  { label: "全部", value: "", key: "all" },
  { label: "本地", value: "local", key: "local" },
  { label: "AD", value: "ad", key: "ad" },
]
// 角色筛选 chip 与表格/表单统一取 ROLE_LABELS 单点（系统管理员/只读用户），不另立短文案映射。
const roleSegOptions = [
  { label: "全部", value: "" as UserRole | "", key: "all" },
  ...toOptions(ROLE_LABELS).map((option) => ({ ...option, value: option.value as UserRole, key: option.value })),
]
const statusOptions = [
  { label: "全部", value: "" as 0 | 1 | "", key: "all" },
  { label: "启用", value: 1 as 0 | 1, key: "active" },
  { label: "停用", value: 0 as 0 | 1, key: "disabled" },
]

const filtering = computed(
  () =>
    Boolean(filters.keyword.trim()) || Boolean(filters.providerCode) || Boolean(filters.role) || filters.status !== "",
)

function roleLabel(role: UserRole): string {
  return ROLE_LABELS[role]
}

function roleTagType(role: UserRole): "danger" | "warning" | "primary" | "info" {
  return roleTag[role]
}

function credentialLabel(user: ManagedUser): string {
  if (user.credential_status === "must_change") return "首次登录待改密"
  if (user.credential_status === "active") return "密码有效"
  return "目录认证"
}

function credentialTagType(user: ManagedUser): "success" | "warning" | "info" {
  if (user.credential_status === "must_change") return "warning"
  if (user.credential_status === "active") return "success"
  return "info"
}

function roleOrigin(user: ManagedUser): string {
  if (user.provider_code === "local") return "本地固定"
  return user.role_override ? "人工覆盖" : "跟随 AD"
}

function syncLabel(status: UserSyncStatus): string {
  return syncLabels[status]
}

function disabledRowClass({ row }: { row: ManagedUser }): string {
  return row.status === 0 ? "user-row-off" : ""
}

const {
  items: users,
  total,
  page,
  loading,
  errorMessage,
  load,
  search,
  reset: resetFilters,
} = usePagedList({
  fetcher: (page, signal) => listUsers({ ...filters, page }, signal),
  errorMessage: "用户台账加载失败",
  resetFilters: () => {
    filters.keyword = ""
    filters.providerCode = ""
    filters.role = ""
    filters.status = ""
  },
})

const policyRead = useLatestRead()
async function loadPolicy(): Promise<void> {
  const signal = policyRead.start()
  try {
    const policy = await passwordPolicyRequest(signal)
    if (signal.aborted) return
    passwordPolicy.value = policy
  } catch {
    // 后端仍会执行同一密码策略；规则接口短暂不可用或被取消时保留当前版本的安全缺省文案。
  }
}

/** 认证源 / 角色 / 状态 seg 点选即重查，与黑名单、上行回复页同一语言。 */
function setProvider(value: string): void {
  filters.providerCode = value
  search()
}

function setRole(value: UserRole | ""): void {
  filters.role = value
  search()
}

function setStatus(value: 0 | 1 | ""): void {
  filters.status = value
  search()
}

function openCreate(): void {
  createDrawer.value?.prepare()
  createDrawerOpen.value = true
}

function openRole(user: ManagedUser): void {
  selected.value = user
  roleDrawer.value?.prepare(user)
  roleDrawerOpen.value = true
}

function openPasswordReset(user: ManagedUser): void {
  selected.value = user
  resetDrawer.value?.prepare()
  resetDrawerOpen.value = true
}

async function changeStatus(user: ManagedUser): Promise<void> {
  user = { ...user }
  if (userActionBusyId.value !== null) return
  const nextStatus: 0 | 1 = user.status === 1 ? 0 : 1
  const action = nextStatus === 1 ? "启用" : "停用"
  // 进函数即置 busy（confirm 之前），确认框期间拦截重复点击。
  userActionBusyId.value = user.account_id
  try {
    if (
      !(await confirmAuditedAction({
        title: `确认${action}账号`,
        body:
          nextStatus === 0
            ? `停用 ${user.display_name || user.username} 后，该账号将无法登录且现有会话立即失效；台账记录保留，可随时重新启用。`
            : `将重新允许 ${user.display_name || user.username} 登录平台，角色与权限维持不变。`,
        auditNote: `${action}行为、操作人与对象 account_id 将写入审计日志。`,
        confirmText: action,
        danger: nextStatus === 0,
      }))
    )
      return
    const saved = await adminStepUp.run(
      { operation: "user_status_change", target_id: String(user.account_id), parameters: { status: nextStatus } },
      `${action}账号`,
      (token) => updateUserStatus(user.account_id, nextStatus, token),
    )
    if (!saved) return
    ElMessage.success(`账号已${action} · 本次操作已记入审计`)
    await load()
  } catch (error) {
    ElMessage.error(errorText(error, `账号${action}失败`))
  } finally {
    userActionBusyId.value = null
  }
}

async function forceLogout(user: ManagedUser): Promise<void> {
  user = { ...user }
  if (userActionBusyId.value !== null) return
  userActionBusyId.value = user.account_id
  try {
    if (
      !(await confirmAuditedAction({
        title: "确认强制下线",
        body: `将立即吊销 ${user.display_name || user.username} 的全部现有会话，需重新登录。`,
        auditNote: "强制下线行为、操作人与对象 account_id 将写入审计日志。",
        confirmText: "强制下线",
      }))
    )
      return
    await revokeUserSessions(user.account_id)
    ElMessage.success("用户已强制下线 · 本次操作已记入审计")
  } catch (error) {
    ElMessage.error(errorText(error, "强制下线失败"))
  } finally {
    userActionBusyId.value = null
  }
}

onMounted(() => {
  void load()
  void loadPolicy()
})
</script>

<template>
  <AdminStepUpDialog :controller="adminStepUp" />
  <section class="page-heading user-heading">
    <div>
      <p class="eyebrow">IDENTITY LEDGER / 身份治理</p>
      <h1>用户与角色</h1>
      <p
        >本地账号由管理员维护，AD 账号首次成功登录后进入台账；角色人工覆盖优先于目录组映射。账号、角色与凭据变更即递增
        security_version，既有会话立即失效；写操作全部写入审计。</p
      >
    </div>
    <el-button data-testid="create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
  </section>

  <form
    class="user-filter-bar filter-collapsible"
    :class="{ 'is-collapsed': filtersCollapsed }"
    @submit.prevent="search"
  >
    <label class="user-fld filter-keep">
      <span>关键词</span>
      <el-input
        v-model="filters.keyword"
        class="user-keyword"
        data-testid="user-filter-keyword"
        clearable
        placeholder="用户名、姓名或部门"
        maxlength="128"
        @clear="search"
      />
    </label>
    <div class="user-fld">
      <span>认证源</span>
      <FilterSeg
        :model-value="filters.providerCode"
        :options="providerOptions"
        data-testid="user-provider-seg"
        button-testid-prefix="user-provider"
        label="认证源筛选"
        @update:model-value="setProvider"
      />
    </div>
    <div class="user-fld">
      <span>角色</span>
      <FilterSeg
        :model-value="filters.role"
        :options="roleSegOptions"
        data-testid="user-role-seg"
        button-testid-prefix="user-role"
        label="角色筛选"
        @update:model-value="setRole"
      />
    </div>
    <div class="user-fld">
      <span>状态</span>
      <FilterSeg
        :model-value="filters.status"
        :options="statusOptions"
        data-testid="user-status-seg"
        button-testid-prefix="user-status"
        label="状态筛选"
        @update:model-value="setStatus"
      />
    </div>
    <MobileFilterToggle
      v-model:collapsed="filtersCollapsed"
      :active-count="[filters.providerCode, filters.role, filters.status].filter((value) => value !== '').length"
    />
    <div class="user-filter-go filter-keep">
      <el-button data-testid="user-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="user-reset" @click="resetFilters">重置</el-button>
    </div>
    <p class="user-privacy"
      >关键词服务端匹配用户名、显示姓名与部门；认证源 / 角色 /
      状态点选即重查。台账服务端分页，全部写操作经审计，停用与重置密码须二次确认。</p
    >
  </form>

  <aside class="user-rules" aria-label="账号与密码规则">
    <div
      ><span>用户名规则</span
      ><p>本地用户名：3–64 位 ASCII 字母、数字、点、下划线或短横线；不区分大小写，创建后不可修改。</p></div
    >
    <div
      ><span>密码规则</span
      ><p
        >{{ passwordPolicy.description }}；创建或重置后为临时密码，默认有效期 24 小时（管理员可配置 1–168
        小时），首次登录必须修改，过期需管理员重置。</p
      ></div
    >
  </aside>

  <LoadErrorAlert class="user-alert" :message="errorMessage" @retry="load" />

  <section class="user-results">
    <template v-if="users.length || loading">
      <el-table
        v-loading="loading"
        :data="users"
        row-key="account_id"
        class="user-table"
        :row-class-name="disabledRowClass"
      >
        <el-table-column label="账号" min-width="200">
          <template #default="{ row }">
            <strong class="user-name">{{ row.display_name || row.username }}</strong>
            <code class="user-code">{{ row.username }}</code>
            <div class="identity-tags">
              <el-tag size="small" :type="row.provider_code === 'ad' ? 'primary' : 'info'" effect="plain">{{
                providerLabel(row.provider_code)
              }}</el-tag>
              <el-tag size="small" :type="credentialTagType(row)" effect="plain">{{ credentialLabel(row) }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="部门 / 来源组" min-width="220">
          <template #default="{ row }">
            <span>{{ row.dept || "未分配部门" }}</span>
            <div class="source-groups">
              <el-tag v-for="group in row.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
              <small v-if="!row.source_groups.length">{{
                row.provider_code === "local" ? "本地维护，无目录来源组" : "暂无同步记录"
              }}</small>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="角色" min-width="140">
          <template #default="{ row }">
            <el-tag :type="roleTagType(row.role)">{{ roleLabel(row.role) }}</el-tag>
            <small class="role-origin">{{ roleOrigin(row) }}</small>
          </template>
        </el-table-column>
        <el-table-column label="状态 / 同步" min-width="210">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 1 ? 'success' : 'info'">{{
              row.status === 1 ? "启用" : "停用"
            }}</el-tag>
            <span class="sync-state" :class="row.sync_status"><i></i>{{ syncLabel(row.sync_status) }}</span>
            <div class="user-times">
              <time v-if="row.provider_code !== 'local'" class="mono-time"
                >同步 {{ formatDateTime(row.last_synced_at) }}</time
              >
              <time class="mono-time">最近登录 {{ formatDateTime(row.last_login_at) }}</time>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <div class="user-row-actions">
              <el-button :data-testid="`role-${row.account_id}`" link type="primary" @click="openRole(row)"
                >角色</el-button
              >
              <el-button
                v-if="row.provider_code === 'local'"
                :data-testid="`reset-password-${row.account_id}`"
                link
                type="primary"
                @click="openPasswordReset(row)"
                >重置密码</el-button
              >
              <el-button
                :data-testid="`status-${row.account_id}`"
                link
                :type="row.status === 1 ? 'danger' : 'success'"
                :loading="userActionBusyId === row.account_id"
                :disabled="userActionBusyId !== null"
                @click="changeStatus(row)"
                >{{ row.status === 1 ? "停用" : "启用" }}</el-button
              >
              <el-button
                :data-testid="`revoke-${row.account_id}`"
                link
                type="danger"
                :loading="userActionBusyId === row.account_id"
                :disabled="userActionBusyId !== null"
                @click="forceLogout(row)"
                >下线</el-button
              >
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="user-mobile-list">
        <article v-for="user in users" :key="user.account_id">
          <header>
            <div
              ><strong class="user-name">{{ user.display_name || user.username }}</strong
              ><code class="user-code">{{ user.username }}</code></div
            >
            <el-tag size="small" :type="user.status === 1 ? 'success' : 'info'">{{
              user.status === 1 ? "启用" : "停用"
            }}</el-tag>
          </header>
          <div class="identity-tags">
            <el-tag size="small" :type="user.provider_code === 'ad' ? 'primary' : 'info'" effect="plain">{{
              providerLabel(user.provider_code)
            }}</el-tag>
            <el-tag size="small" :type="credentialTagType(user)" effect="plain">{{ credentialLabel(user) }}</el-tag>
            <span class="sync-state" :class="user.sync_status"><i></i>{{ syncLabel(user.sync_status) }}</span>
          </div>
          <p>{{ user.dept || "未分配部门" }}</p>
          <p>最近登录 {{ formatDateTime(user.last_login_at) }}</p>
          <div class="source-groups">
            <el-tag v-for="group in user.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
            <small v-if="!user.source_groups.length">{{
              user.provider_code === "local" ? "本地维护，无目录来源组" : "暂无同步记录"
            }}</small>
          </div>
          <footer>
            <span
              ><el-tag :type="roleTag[user.role]" size="small">{{ ROLE_LABELS[user.role] }}</el-tag
              >{{ roleOrigin(user) }}</span
            >
            <span>
              <el-button :data-testid="`mobile-role-${user.account_id}`" link type="primary" @click="openRole(user)"
                >角色</el-button
              >
              <el-button
                v-if="user.provider_code === 'local'"
                :data-testid="`mobile-reset-password-${user.account_id}`"
                link
                type="primary"
                @click="openPasswordReset(user)"
                >重置密码</el-button
              >
              <el-button
                :data-testid="`mobile-status-${user.account_id}`"
                link
                :type="user.status === 1 ? 'danger' : 'success'"
                :loading="userActionBusyId === user.account_id"
                :disabled="userActionBusyId !== null"
                @click="changeStatus(user)"
                >{{ user.status === 1 ? "停用" : "启用" }}</el-button
              >
              <el-button
                :data-testid="`mobile-revoke-${user.account_id}`"
                link
                type="danger"
                :loading="userActionBusyId === user.account_id"
                :disabled="userActionBusyId !== null"
                @click="forceLogout(user)"
                >下线</el-button
              >
            </span>
          </footer>
        </article>
      </div>
    </template>
    <div v-else-if="filtering" class="user-empty-action">
      <EmptyState title="没有符合筛选条件的账号" description="调整关键词或筛选条件后重新查询。" />
      <el-button data-testid="clear-user-filters" @click="resetFilters">清除筛选</el-button>
    </div>
    <div v-else class="user-empty-action">
      <EmptyState title="尚无平台账号" description="本地账号由管理员创建；AD 账号在首次成功登录后进入台账。" />
      <el-button data-testid="empty-create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
    </div>

    <ListPagination v-model:page="page" :total="total" unit="名用户" @change="load" />
  </section>
  <UserCreateDrawer
    ref="createDrawer"
    v-model="createDrawerOpen"
    :policy="passwordPolicy"
    :step-up="adminStepUp"
    @created="load"
  />
  <UserRoleDrawer ref="roleDrawer" v-model="roleDrawerOpen" :user="selected" :step-up="adminStepUp" @saved="load" />
  <UserPasswordResetDrawer
    ref="resetDrawer"
    v-model="resetDrawerOpen"
    :user="selected"
    :policy="passwordPolicy"
    :step-up="adminStepUp"
    @reset="load"
  />
</template>
