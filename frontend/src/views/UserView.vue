<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, h, onMounted, reactive, ref } from "vue"

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
const roleDescriptions: Record<UserRole, string> = {
  admin: "全部功能，含账号维护、角色覆盖、队列恢复与强制下线",
  approver: "审批（不能审本人提交），查看全量记录与报表",
  operator: "Web 人工发送（通知/营销），本部门记录与模板",
  viewer: "本部门记录与报表",
}
const LOCAL_LOGIN_RE = /^[a-z0-9._-]{3,64}$/

const providerOptions = [
  { label: "全部", value: "", key: "all" },
  { label: "本地", value: "local", key: "local" },
  { label: "AD", value: "ad", key: "ad" },
]
const roleSegOptions = [
  { label: "全部", value: "" as UserRole | "", key: "all" },
  { label: "管理员", value: "admin" as UserRole, key: "admin" },
  { label: "审批人", value: "approver" as UserRole, key: "approver" },
  { label: "操作员", value: "operator" as UserRole, key: "operator" },
  { label: "只读", value: "viewer" as UserRole, key: "viewer" },
]
const statusOptions = [
  { label: "全部", value: "" as 0 | 1 | "", key: "all" },
  { label: "有效", value: 1 as 0 | 1, key: "active" },
  { label: "停用", value: 0 as 0 | 1, key: "disabled" },
]

const filtering = computed(
  () =>
    Boolean(filters.keyword.trim()) ||
    Boolean(filters.providerCode) ||
    Boolean(filters.role) ||
    filters.status !== "",
)

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

function disabledRowClass({ row }: { row: ManagedUser }): string {
  return row.status === 0 ? "user-row-off" : ""
}

let loadToken = 0

async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  errorMessage.value = ""
  try {
    const page = await listUsers(filters)
    if (token !== loadToken) return
    users.value = page.items
    total.value = page.total
  } catch (error) {
    if (token !== loadToken) return
    errorMessage.value = errorText(error, "用户台账加载失败")
  } finally {
    if (token === loadToken) loading.value = false
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

function resetFilters(): void {
  filters.keyword = ""
  filters.providerCode = ""
  filters.role = ""
  filters.status = ""
  filters.page = 1
  void load()
}

/** 认证源 / 角色 / 状态 seg 点选即重查，与黑名单、上行回复页同一语言。 */
function setProvider(value: string): void {
  if (value === filters.providerCode) return
  filters.providerCode = value
  search()
}

function setRole(value: UserRole | ""): void {
  if (value === filters.role) return
  filters.role = value
  search()
}

function setStatus(value: 0 | 1 | ""): void {
  if (value === filters.status) return
  filters.status = value
  search()
}

function usernameProblem(value: string): string | null {
  // 与服务端 validate_local_login_name 同一规则（规范化后 3–64 位小写字符集）。
  return LOCAL_LOGIN_RE.test(value.trim().toLowerCase())
    ? null
    : "本地用户名必须为 3–64 位字母、数字、点、下划线或短横线"
}

function passwordProblem(password: string, username: string): string | null {
  // 提交前按服务端下发的策略即时校验；服务端仍为权威校验。
  const policy = passwordPolicy.value
  if (password.length < policy.min_length || password.length > policy.max_length) {
    return `密码长度必须为 ${policy.min_length}–${policy.max_length} 位`
  }
  const classes = [/\p{Ll}/u, /\p{Lu}/u, /\p{N}/u, /[^\p{L}\p{N}]/u].filter((re) =>
    re.test(password),
  ).length
  if (classes < policy.required_character_classes) {
    return `密码必须满足至少 ${policy.required_character_classes} 类：大写字母、小写字母、数字、特殊字符`
  }
  const normalized = username.trim().toLowerCase()
  if (policy.forbid_username && normalized && password.toLowerCase().includes(normalized)) {
    return "密码不能包含用户名"
  }
  return null
}

interface Precheck {
  key: string
  label: string
  ok: boolean | null
}

/** 与服务端密码策略同口径的单项预检，未输入时保持中性（null）。 */
function passwordPrechecks(password: string, username: string): Precheck[] {
  const policy = passwordPolicy.value
  const lengthLabel = `长度 ${policy.min_length}–${policy.max_length} 位`
  const classesLabel = `字符类别 ≥${policy.required_character_classes}`
  if (!password) {
    return [
      { key: "length", label: lengthLabel, ok: null },
      { key: "classes", label: classesLabel, ok: null },
      { key: "forbid", label: "不含用户名", ok: null },
    ]
  }
  const classes = [/\p{Ll}/u, /\p{Lu}/u, /\p{N}/u, /[^\p{L}\p{N}]/u].filter((re) =>
    re.test(password),
  ).length
  const normalized = username.trim().toLowerCase()
  return [
    {
      key: "length",
      label: lengthLabel,
      ok: password.length >= policy.min_length && password.length <= policy.max_length,
    },
    { key: "classes", label: classesLabel, ok: classes >= policy.required_character_classes },
    {
      key: "forbid",
      label: "不含用户名",
      ok: !(policy.forbid_username && normalized && password.toLowerCase().includes(normalized)),
    },
  ]
}

const createChecks = computed<Precheck[]>(() => [
  {
    key: "username",
    label: "用户名 3–64 位合规",
    ok: createForm.username ? usernameProblem(createForm.username) === null : null,
  },
  ...passwordPrechecks(createForm.temporary_password, createForm.username),
])

const createError = computed(() => {
  if (!createForm.username.trim() && !createForm.temporary_password) return ""
  const issue =
    usernameProblem(createForm.username) ??
    (createForm.temporary_password
      ? passwordProblem(createForm.temporary_password, createForm.username)
      : null)
  return issue ?? ""
})

const canCreate = computed(
  () =>
    !saving.value &&
    !usernameProblem(createForm.username) &&
    Boolean(createForm.display_name.trim()) &&
    !passwordProblem(createForm.temporary_password, createForm.username),
)

const resetChecks = computed<Precheck[]>(() =>
  passwordPrechecks(resetPasswordDraft.value, selected.value?.username ?? ""),
)

const canReset = computed(
  () =>
    !saving.value &&
    Boolean(resetPasswordDraft.value) &&
    !passwordProblem(resetPasswordDraft.value, selected.value?.username ?? ""),
)

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
  const passwordIssue = passwordProblem(createForm.temporary_password, username)
  if (passwordIssue) {
    ElMessage.warning(passwordIssue)
    return
  }
  saving.value = true
  try {
    await createLocalUser({
      username,
      display_name: displayName,
      dept: createForm.dept.trim(),
      role: createForm.role,
      temporary_password: createForm.temporary_password,
    })
    closeCreate()
    ElMessage.success("本地账号已创建，首次登录须修改临时密码 · 本次操作已记入审计")
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
    ElMessage.success("角色策略已更新，既有会话已失效 · 本次操作已记入审计")
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
  const passwordIssue = passwordProblem(resetPasswordDraft.value, selected.value.username)
  if (passwordIssue) {
    ElMessage.warning(passwordIssue)
    return
  }
  try {
    await ElMessageBox.confirm(
      h("div", { class: "user-confirm-dialog" }, [
        h(
          "p",
          `将重置 ${selected.value.display_name || selected.value.username} 的本地密码，并立即吊销现有会话；用户下次登录必须修改密码。`,
        ),
        h(
          "p",
          { class: "user-confirm-audit" },
          "重置行为、操作人与对象 account_id 将写入审计日志；临时密码不回显。",
        ),
      ]),
      "确认重置密码",
      {
        confirmButtonText: "确认重置",
        cancelButtonText: "取消",
        type: "warning",
        customClass: "user-confirm-box",
      },
    )
    saving.value = true
    await resetLocalPassword(selected.value.account_id, resetPasswordDraft.value)
    closePasswordReset()
    ElMessage.success("临时密码已重置，下次登录须修改 · 本次操作已记入审计")
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
      h("div", { class: "user-confirm-dialog" }, [
        h(
          "p",
          nextStatus === 0
            ? `停用 ${user.display_name || user.username} 后，该账号将无法登录且现有会话立即失效；台账记录保留，可随时重新启用。`
            : `将重新允许 ${user.display_name || user.username} 登录平台，角色与权限维持不变。`,
        ),
        h(
          "p",
          { class: "user-confirm-audit" },
          `${action}行为、操作人与对象 account_id 将写入审计日志。`,
        ),
      ]),
      `确认${action}账号`,
      {
        confirmButtonText: action,
        cancelButtonText: "取消",
        type: nextStatus === 0 ? "warning" : "info",
        customClass: "user-confirm-box",
      },
    )
    await updateUserStatus(user.account_id, nextStatus)
    ElMessage.success(`账号已${action} · 本次操作已记入审计`)
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
      h("div", { class: "user-confirm-dialog" }, [
        h("p", `将立即吊销 ${user.display_name || user.username} 的全部现有会话，需重新登录。`),
        h(
          "p",
          { class: "user-confirm-audit" },
          "强制下线行为、操作人与对象 account_id 将写入审计日志。",
        ),
      ]),
      "确认强制下线",
      {
        confirmButtonText: "强制下线",
        cancelButtonText: "取消",
        type: "warning",
        customClass: "user-confirm-box",
      },
    )
    await revokeUserSessions(user.account_id)
    ElMessage.success("用户已强制下线 · 本次操作已记入审计")
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
      <p class="eyebrow">IDENTITY LEDGER / 身份治理</p>
      <h1>用户与角色</h1>
      <p>本地账号由管理员维护，AD 账号首次成功登录后进入台账；角色人工覆盖优先于目录组映射。账号、角色与凭据变更即递增 security_version，既有会话立即失效；写操作全部写入审计。</p>
    </div>
    <el-button data-testid="create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
  </section>

  <form class="user-filter-bar" @submit.prevent="search">
    <label class="user-fld">
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
      <div class="user-seg" role="group" aria-label="认证源筛选" data-testid="user-provider-seg">
        <button
          v-for="option in providerOptions"
          :key="option.key"
          type="button"
          :class="{ on: filters.providerCode === option.value }"
          :data-testid="`user-provider-${option.key}`"
          @click="setProvider(option.value)"
        >{{ option.label }}</button>
      </div>
    </div>
    <div class="user-fld">
      <span>角色</span>
      <div class="user-seg" role="group" aria-label="角色筛选" data-testid="user-role-seg">
        <button
          v-for="option in roleSegOptions"
          :key="option.key"
          type="button"
          :class="{ on: filters.role === option.value }"
          :data-testid="`user-role-${option.key}`"
          @click="setRole(option.value)"
        >{{ option.label }}</button>
      </div>
    </div>
    <div class="user-fld">
      <span>状态</span>
      <div class="user-seg" role="group" aria-label="状态筛选" data-testid="user-status-seg">
        <button
          v-for="option in statusOptions"
          :key="option.key"
          type="button"
          :class="{ on: filters.status === option.value }"
          :data-testid="`user-status-${option.key}`"
          @click="setStatus(option.value)"
        >{{ option.label }}</button>
      </div>
    </div>
    <div class="user-filter-go">
      <el-button data-testid="user-search" type="primary" native-type="submit" :loading="loading">查询</el-button>
      <el-button data-testid="user-reset" @click="resetFilters">重置</el-button>
    </div>
    <p class="user-privacy">关键词服务端匹配用户名、显示姓名与部门；认证源 / 角色 / 状态点选即重查。台账服务端分页，全部写操作经审计，停用与重置密码须二次确认。</p>
  </form>

  <aside class="user-rules" aria-label="账号与密码规则">
    <div><span>用户名规则</span><p>本地用户名：3–64 位 ASCII 字母、数字、点、下划线或短横线；不区分大小写，创建后不可修改。</p></div>
    <div><span>密码规则</span><p>{{ passwordPolicy.description }}；创建或重置后为临时密码，首次登录必须修改。</p></div>
  </aside>

  <el-alert v-if="errorMessage" class="user-alert" :title="errorMessage" type="error" show-icon :closable="false">
    <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
  </el-alert>

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
              <el-tag size="small" :type="row.provider_code === 'ad' ? 'primary' : 'info'" effect="plain">{{ providerLabel(row.provider_code) }}</el-tag>
              <el-tag size="small" :type="credentialTagType(row)" effect="plain">{{ credentialLabel(row) }}</el-tag>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="部门 / 来源组" min-width="220">
          <template #default="{ row }">
            <span>{{ row.dept || "未分配部门" }}</span>
            <div class="source-groups">
              <el-tag v-for="group in row.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
              <small v-if="!row.source_groups.length">{{ row.provider_code === "local" ? "本地维护，无目录来源组" : "暂无同步记录" }}</small>
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
            <el-tag size="small" :type="row.status === 1 ? 'success' : 'danger'">{{ row.status === 1 ? "账号有效" : "账号停用" }}</el-tag>
            <span class="sync-state" :class="row.sync_status"><i></i>{{ syncLabel(row.sync_status) }}</span>
            <div class="user-times">
              <time v-if="row.provider_code !== 'local'" class="mono-time">同步 {{ localTime(row.last_synced_at) }}</time>
              <time class="mono-time">最近登录 {{ localTime(row.last_login_at) }}</time>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <div class="user-row-actions">
              <el-button :data-testid="`role-${row.account_id}`" link type="primary" @click="openRole(row)">角色</el-button>
              <el-button v-if="row.provider_code === 'local'" :data-testid="`reset-password-${row.account_id}`" link type="primary" @click="openPasswordReset(row)">重置密码</el-button>
              <el-button :data-testid="`status-${row.account_id}`" link :type="row.status === 1 ? 'danger' : 'success'" @click="changeStatus(row)">{{ row.status === 1 ? "停用" : "启用" }}</el-button>
              <el-button :data-testid="`revoke-${row.account_id}`" link type="danger" @click="forceLogout(row)">下线</el-button>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <div class="user-mobile-list">
        <article v-for="user in users" :key="user.account_id">
          <header>
            <div><strong class="user-name">{{ user.display_name || user.username }}</strong><code class="user-code">{{ user.username }}</code></div>
            <el-tag size="small" :type="user.status === 1 ? 'success' : 'danger'">{{ user.status === 1 ? "有效" : "停用" }}</el-tag>
          </header>
          <div class="identity-tags">
            <el-tag size="small" :type="user.provider_code === 'ad' ? 'primary' : 'info'" effect="plain">{{ providerLabel(user.provider_code) }}</el-tag>
            <el-tag size="small" :type="credentialTagType(user)" effect="plain">{{ credentialLabel(user) }}</el-tag>
            <span class="sync-state" :class="user.sync_status"><i></i>{{ syncLabel(user.sync_status) }}</span>
          </div>
          <p>{{ user.dept || "未分配部门" }}</p>
          <p>最近登录 {{ localTime(user.last_login_at) }}</p>
          <div class="source-groups">
            <el-tag v-for="group in user.source_groups" :key="group" size="small" effect="plain">{{ group }}</el-tag>
            <small v-if="!user.source_groups.length">{{ user.provider_code === "local" ? "本地维护，无目录来源组" : "暂无同步记录" }}</small>
          </div>
          <footer>
            <span><el-tag :type="roleTag[user.role]" size="small">{{ roleLabels[user.role] }}</el-tag>{{ roleOrigin(user) }}</span>
            <span>
              <el-button :data-testid="`mobile-role-${user.account_id}`" link type="primary" @click="openRole(user)">角色</el-button>
              <el-button v-if="user.provider_code === 'local'" :data-testid="`mobile-reset-password-${user.account_id}`" link type="primary" @click="openPasswordReset(user)">重置密码</el-button>
              <el-button :data-testid="`mobile-status-${user.account_id}`" link :type="user.status === 1 ? 'danger' : 'success'" @click="changeStatus(user)">{{ user.status === 1 ? "停用" : "启用" }}</el-button>
              <el-button :data-testid="`mobile-revoke-${user.account_id}`" link type="danger" @click="forceLogout(user)">下线</el-button>
            </span>
          </footer>
        </article>
      </div>
    </template>
    <div v-else-if="filtering" class="user-empty-action">
      <EmptyState title="没有符合条件的账号" description="调整关键词或筛选条件后重新查询。" />
      <el-button data-testid="clear-user-filters" @click="resetFilters">清除筛选</el-button>
    </div>
    <div v-else class="user-empty-action">
      <EmptyState title="尚无平台账号" description="本地账号由管理员创建；AD 账号在首次成功登录后进入台账。" />
      <el-button data-testid="empty-create-local-user" type="primary" @click="openCreate">创建本地账号</el-button>
    </div>

    <footer class="user-pagination">
      <span>共 {{ total }} 名用户 · 每页 20</span>
      <el-pagination
        v-model:current-page="filters.page"
        :page-size="filters.pageSize"
        :total="total"
        layout="prev, pager, next"
        @current-change="load"
      />
    </footer>
  </section>

  <el-drawer v-model="createDrawerOpen" size="min(440px, 92vw)" :teleported="false" class="user-drawer" @closed="resetCreateForm">
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
          <el-option v-for="(label, value) in roleLabels" :key="value" :label="label" :value="value" />
        </el-select>
        <small class="field-rule" data-testid="create-role-permission">{{ roleDescriptions[createForm.role] }}</small>
      </el-form-item>
      <el-form-item label="临时密码" required>
        <el-input v-model="createForm.temporary_password" data-testid="create-password" type="password" show-password autocomplete="new-password" :maxlength="passwordPolicy.max_length" />
        <small class="field-rule">{{ passwordPolicy.description }}；首次登录必须修改。</small>
      </el-form-item>
    </el-form>
    <div v-if="createForm.username || createForm.temporary_password" class="user-parse" data-testid="create-precheck">
      <span
        v-for="check in createChecks"
        :key="check.key"
        class="user-chip"
        :class="{ 'user-chip-ok': check.ok === true, 'user-chip-bad': check.ok === false }"
      >{{ check.label }}</span>
    </div>
    <p v-if="createError" class="user-parse-error">{{ createError }}</p>
    <template #footer>
      <div class="user-editor-foot">
        <small>创建行为与操作人写入审计日志；临时密码只随本次请求提交，不回显、不入库明文。</small>
        <div>
          <el-button @click="closeCreate">取消</el-button>
          <el-button data-testid="save-local-user" type="primary" :disabled="!canCreate" :loading="saving" @click="saveLocalUser">创建账号</el-button>
        </div>
      </div>
    </template>
  </el-drawer>

  <el-drawer v-model="roleDrawerOpen" size="min(440px, 92vw)" :teleported="false" class="user-drawer">
    <template #header>
      <div class="user-drawer-head">
        <div class="user-drawer-title">角色策略</div>
        <code>PUT /api/v1/web/admin/users/{{ selected?.account_id ?? "—" }}/role · 保存即失效旧会话</code>
      </div>
    </template>
    <template v-if="selected">
      <section class="role-subject">
        <span>{{ selected.display_name || selected.username }}</span>
        <code>{{ selected.username }}</code>
        <small>{{ providerLabel(selected.provider_code) }} · {{ selected.dept || "未分配部门" }}</small>
      </section>
      <el-form label-position="top" class="user-form">
        <el-form-item label="目标角色">
          <el-select v-model="roleDraft" :disabled="selected.provider_code === 'ad' && !overrideDraft">
            <el-option v-for="(label, value) in roleLabels" :key="value" :label="label" :value="value" />
          </el-select>
          <small class="field-rule" data-testid="role-permission">{{ roleDescriptions[roleDraft] }}</small>
        </el-form-item>
        <el-form-item label="角色来源">
          <div v-if="selected.provider_code === 'ad'" class="override-control">
            <el-switch v-model="overrideDraft" data-testid="override-switch" inline-prompt active-text="人工" inactive-text="AD" />
            <p>{{ overrideDraft ? "保存后固定为所选角色，后续目录同步不改写。" : "保存后按最近来源组和当前映射恢复角色。" }}</p>
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
      <div class="user-editor-foot">
        <small>保存即递增 security_version，该账号全部既有会话立即失效；角色变更与操作人写入审计日志。</small>
        <div>
          <el-button @click="roleDrawerOpen = false">取消</el-button>
          <el-button type="primary" :loading="saving" :disabled="!selected" @click="saveRole">保存角色</el-button>
        </div>
      </div>
    </template>
  </el-drawer>

  <el-drawer v-model="resetDrawerOpen" size="min(440px, 92vw)" :teleported="false" class="user-drawer" @closed="resetPasswordDraft = ''">
    <template #header>
      <div class="user-drawer-head">
        <div class="user-drawer-title">重置本地密码</div>
        <code>POST /api/v1/web/admin/users/{{ selected?.account_id ?? "—" }}/password/reset · 重置即吊销全部会话</code>
      </div>
    </template>
    <template v-if="selected">
      <section class="role-subject">
        <span>{{ selected.display_name || selected.username }}</span>
        <code>{{ selected.username }}</code>
        <small>重置后立即吊销全部会话；用户下次登录必须修改密码。</small>
      </section>
      <el-form label-position="top" class="user-form" @submit.prevent="confirmPasswordReset">
        <el-form-item label="新临时密码" required>
          <el-input v-model="resetPasswordDraft" data-testid="reset-password-input" type="password" show-password autocomplete="new-password" :maxlength="passwordPolicy.max_length" />
          <small class="field-rule">{{ passwordPolicy.description }}。</small>
        </el-form-item>
      </el-form>
      <div v-if="resetPasswordDraft" class="user-parse" data-testid="reset-precheck">
        <span
          v-for="check in resetChecks"
          :key="check.key"
          class="user-chip"
          :class="{ 'user-chip-ok': check.ok === true, 'user-chip-bad': check.ok === false }"
        >{{ check.label }}</span>
      </div>
    </template>
    <template #footer>
      <div class="user-editor-foot">
        <small>重置行为与操作人写入审计日志；临时密码不回显。</small>
        <div>
          <el-button @click="closePasswordReset">取消</el-button>
          <el-button data-testid="confirm-password-reset" type="danger" :disabled="!canReset" :loading="saving" @click="confirmPasswordReset">重置密码</el-button>
        </div>
      </div>
    </template>
  </el-drawer>
</template>
