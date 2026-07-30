<script setup lang="ts">
import "../styles/workspace.css"

import { ElMessage, ElMessageBox } from "element-plus"
import { computed, nextTick, onMounted, reactive, ref } from "vue"

import {
  activateAuthProvider,
  disableAuthProvider,
  getAuthProvider,
  listAuthProviderRoleMappings,
  listConfigs,
  replaceAuthProviderRoleMappings,
  saveAuthProviderDraft,
  testAuthProvider,
  updateConfigs,
  type AdminUserRole,
  type AuthProviderAdmin,
  type ConfigItem,
  type ConfigUpdate,
  type ExternalRoleMapping,
  type LdapProviderConfig,
} from "../api/admin"
import VendorTestConsole from "../components/VendorTestConsole.vue"
import { useSessionStore } from "../stores/session"

type ConfigTab = "runtime" | "providers" | "vendor-test"

const session = useSessionStore()
const activeTab = ref<ConfigTab>("runtime")
const configTabs = computed(() => [
  { name: "runtime" as const, label: "运行参数" },
  { name: "providers" as const, label: "认证源" },
  ...(session.role === "admin" ? [{ name: "vendor-test" as const, label: "真实联调" }] : []),
])

const configs = ref<ConfigItem[]>([])
const values = reactive<Record<string, string>>({})
const original = reactive<Record<string, string>>({})
const touched = new Set<string>()
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref("")

const adProvider = ref<AuthProviderAdmin | null>(null)
const providerLoading = ref(false)
const providerSaving = ref(false)
const testing = ref(false)
const providerError = ref("")
const providerDirty = ref(false)
const advancedOpen = ref(false)
const disabledPreserved = ref(false)
const mappingsSaving = ref(false)
const roleMappings = ref<ExternalRoleMapping[]>([])
const adForm = reactive<LdapProviderConfig>({
  server: "",
  base_dn: "",
  bind_dn: "",
  user_search_filter: "(sAMAccountName={username})",
  username_attribute: "sAMAccountName",
  display_name_attribute: "displayName",
  dept_attribute: "department",
  subject_attribute: "objectGUID",
  group_attribute: "memberOf",
  connect_timeout_s: 5,
  receive_timeout_s: 8,
})

const roleLabels: Record<AdminUserRole, string> = {
  admin: "系统管理员",
  approver: "审批人",
  operator: "操作员",
  viewer: "只读用户",
}

const groups = computed(() => {
  const result = new Map<string, ConfigItem[]>()
  for (const item of configs.value) {
    const rows = result.get(item.group) || []
    rows.push(item)
    result.set(item.group, rows)
  }
  return [...result.entries()]
})

const currentDraftTested = computed(
  () =>
    !providerDirty.value &&
    adProvider.value !== null &&
    adProvider.value.tested_version === adProvider.value.draft_version &&
    adProvider.value.last_test_status?.toLowerCase() === "success",
)

const canActivateProvider = computed(
  () =>
    currentDraftTested.value &&
    adProvider.value !== null &&
    (!adProvider.value.enabled || adProvider.value.active_version !== adProvider.value.draft_version),
)

function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function selectTab(tab: ConfigTab): void {
  activeTab.value = tab
}

async function moveTab(direction: -1 | 1): Promise<void> {
  const current = configTabs.value.findIndex((item) => item.name === activeTab.value)
  const next = (current + direction + configTabs.value.length) % configTabs.value.length
  activeTab.value = configTabs.value[next].name
  await nextTick()
  document.getElementById(`config-tab-${activeTab.value}`)?.focus()
}

function hydrate(items: ConfigItem[]): void {
  configs.value = items
  touched.clear()
  for (const item of items) {
    const value = item.value ?? ""
    values[item.key] = value
    original[item.key] = value
  }
}

function hydrateProvider(provider: AuthProviderAdmin): void {
  adProvider.value = provider
  Object.assign(adForm, provider.draft_config)
  providerDirty.value = false
}

async function load(): Promise<void> {
  loading.value = true
  errorMessage.value = ""
  try {
    hydrate(await listConfigs())
  } catch (error) {
    errorMessage.value = errorText(error, "系统参数加载失败")
  } finally {
    loading.value = false
  }
}

async function loadProvider(): Promise<void> {
  providerLoading.value = true
  providerError.value = ""
  try {
    const [provider, mappings] = await Promise.all([
      getAuthProvider("ad"),
      listAuthProviderRoleMappings("ad"),
    ])
    hydrateProvider(provider)
    roleMappings.value = mappings.mappings.map((item) => ({ ...item }))
  } catch (error) {
    providerError.value = errorText(error, "认证源配置加载失败")
  } finally {
    providerLoading.value = false
  }
}

function mark(key: string): void {
  touched.add(key)
}

function markProviderDirty(): void {
  providerDirty.value = true
  disabledPreserved.value = false
}

function changes(): ConfigUpdate[] {
  return configs.value
    .filter((item) => (item.sensitive ? touched.has(item.key) : values[item.key] !== original[item.key]))
    .map((item) => ({ key: item.key, value: values[item.key] }))
}

async function save(): Promise<void> {
  const items = changes()
  if (!items.length) {
    ElMessage.info("没有待保存的变更")
    return
  }
  const restartKeys = new Set(
    configs.value.filter((item) => item.beat_restart_required).map((item) => item.key),
  )
  try {
    if (items.some((item) => restartKeys.has(item.key))) {
      await ElMessageBox.confirm(
        "包含 beat 调度参数；保存后必须重启 beat 与 API 容器才会生效。",
        "确认调度参数变更",
        { type: "warning", confirmButtonText: "保存变更" },
      )
    }
    saving.value = true
    hydrate(await updateConfigs(items))
    ElMessage.success("系统参数已更新并写入审计")
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(errorText(error, "参数保存失败"))
    }
  } finally {
    saving.value = false
  }
}

function providerDraft(): LdapProviderConfig {
  return {
    server: adForm.server.trim(),
    base_dn: adForm.base_dn.trim(),
    bind_dn: adForm.bind_dn.trim(),
    user_search_filter: adForm.user_search_filter.trim(),
    username_attribute: adForm.username_attribute.trim(),
    display_name_attribute: adForm.display_name_attribute.trim(),
    dept_attribute: adForm.dept_attribute.trim(),
    subject_attribute: adForm.subject_attribute.trim(),
    group_attribute: adForm.group_attribute.trim(),
    connect_timeout_s: Number(adForm.connect_timeout_s),
    receive_timeout_s: Number(adForm.receive_timeout_s),
  }
}

async function saveProviderDraft(): Promise<void> {
  providerSaving.value = true
  try {
    hydrateProvider(await saveAuthProviderDraft("ad", providerDraft()))
    disabledPreserved.value = false
    ElMessage.success("AD 配置草稿已保存，请测试当前版本")
  } catch (error) {
    ElMessage.error(errorText(error, "AD 配置草稿保存失败"))
  } finally {
    providerSaving.value = false
  }
}

async function runProviderTest(): Promise<void> {
  testing.value = true
  try {
    const result = await testAuthProvider("ad")
    hydrateProvider(await getAuthProvider("ad"))
    if (result.success) {
      ElMessage.success("AD 当前草稿连接测试通过")
    } else {
      ElMessage.error(`连接测试失败：${result.result_code}`)
    }
  } catch (error) {
    ElMessage.error(errorText(error, "AD 连接测试失败"))
  } finally {
    testing.value = false
  }
}

async function activateProvider(): Promise<void> {
  providerSaving.value = true
  try {
    hydrateProvider(await activateAuthProvider("ad"))
    disabledPreserved.value = false
    ElMessage.success("AD 认证源已启用")
  } catch (error) {
    ElMessage.error(errorText(error, "AD 认证源启用失败"))
  } finally {
    providerSaving.value = false
  }
}

async function disableProvider(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "禁用后登录页不再显示 AD，已登录会话不受影响；配置与角色映射继续保留。",
      "确认禁用 AD",
      { type: "warning", confirmButtonText: "禁用 AD", cancelButtonText: "取消" },
    )
    providerSaving.value = true
    hydrateProvider(await disableAuthProvider("ad"))
    disabledPreserved.value = true
    ElMessage.success("AD 已禁用，配置与角色映射均已保留")
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(errorText(error, "AD 认证源禁用失败"))
    }
  } finally {
    providerSaving.value = false
  }
}

function addRoleMapping(): void {
  roleMappings.value.push({ external_group: "", role: "viewer" })
}

function removeRoleMapping(index: number): void {
  roleMappings.value.splice(index, 1)
}

async function saveRoleMappings(): Promise<void> {
  mappingsSaving.value = true
  try {
    const mappings = roleMappings.value
      .map((item) => ({ external_group: item.external_group.trim(), role: item.role }))
      .filter((item) => item.external_group)
    const result = await replaceAuthProviderRoleMappings("ad", mappings)
    roleMappings.value = result.mappings.map((item) => ({ ...item }))
    ElMessage.success("AD 目录组角色映射已更新")
  } catch (error) {
    ElMessage.error(errorText(error, "角色映射保存失败"))
  } finally {
    mappingsSaving.value = false
  }
}

onMounted(() => {
  void load()
  void loadProvider()
})
</script>

<template>
  <section class="page-heading config-heading">
    <div>
      <p class="eyebrow">POLICY REGISTRY / 策略注册表</p>
      <h1>系统参数</h1>
      <p>运行参数、认证源与真实联调统一在此受控；正式凭据密封交付后仍只由 Docker secrets 文件提供给运行时。</p>
    </div>
    <div class="config-heading-note"><strong>{{ configs.length }}</strong><span>项受控运行参数</span></div>
  </section>

  <nav class="config-tabs" role="tablist" aria-label="系统配置模块">
    <button
      v-for="tab in configTabs"
      :id="`config-tab-${tab.name}`"
      :key="tab.name"
      type="button"
      role="tab"
      :aria-selected="activeTab === tab.name"
      :aria-controls="`config-panel-${tab.name}`"
      :tabindex="activeTab === tab.name ? 0 : -1"
      :class="{ active: activeTab === tab.name }"
      @click="selectTab(tab.name)"
      @keydown.left.prevent="moveTab(-1)"
      @keydown.right.prevent="moveTab(1)"
    >{{ tab.label }}</button>
  </nav>

  <el-card
    v-show="activeTab === 'providers'"
    id="config-panel-providers"
    v-loading="providerLoading"
    shadow="never"
    class="provider-config-card"
    role="tabpanel"
    aria-labelledby="config-tab-providers"
  >
    <template #header>
      <div class="provider-section-title">
        <div><p class="eyebrow">AUTHENTICATION SOURCES</p><strong>认证源</strong></div>
        <span>登录时由用户明确选择，不自动回退</span>
      </div>
    </template>

    <el-alert v-if="providerError" :title="providerError" type="error" :closable="false" show-icon>
      <template #default><el-button link type="primary" @click="loadProvider">重新加载</el-button></template>
    </el-alert>

    <article data-testid="local-provider" class="provider-local-row">
      <div class="provider-mark local">LOCAL</div>
      <div><strong>本地账号</strong><p>管理员创建和维护；临时密码首次登录必须修改。</p></div>
      <div class="provider-fixed-state"><el-tag type="success">始终启用</el-tag><small>系统内置，不可修改</small></div>
    </article>

    <article v-if="adProvider" class="provider-ad-workbench">
      <header class="provider-ad-header">
        <div class="provider-mark ad">AD</div>
        <div>
          <strong>{{ adProvider.name }}</strong>
          <p>{{ adProvider.enabled ? 'AD 当前已启用' : 'AD 当前已禁用' }}</p>
        </div>
        <div class="provider-version-state">
          <span>草稿版本 v{{ adProvider.draft_version }}</span>
          <span v-if="adProvider.active_version !== null">生效版本 v{{ adProvider.active_version }}</span>
          <span v-else>尚无生效版本</span>
        </div>
      </header>

      <div class="provider-readiness">
        <span :class="{ ready: adProvider.bind_secret_available }"><i></i>Bind Secret {{ adProvider.bind_secret_available ? '已就绪' : '未就绪' }}</span>
        <span :class="{ ready: adProvider.ca_available }"><i></i>CA 证书{{ adProvider.ca_available ? '已就绪' : '未就绪' }}</span>
        <span v-if="providerDirty" class="stale"><i></i>配置已修改，需保存并重新测试</span>
        <span v-else-if="currentDraftTested" class="ready"><i></i>当前草稿测试通过</span>
        <span v-else-if="adProvider.last_test_status" class="stale"><i></i>当前草稿尚未通过测试 · {{ adProvider.last_test_status }}</span>
        <span v-else class="stale"><i></i>当前草稿尚未通过测试</span>
      </div>
      <p v-if="disabledPreserved" class="provider-preserved">配置与角色映射均已保留，可随时重新测试并启用。</p>

      <el-form class="provider-form" label-position="top" @submit.prevent="saveProviderDraft">
        <div class="provider-field-grid">
          <el-form-item label="LDAP 服务地址" required>
            <el-input v-model="adForm.server" data-testid="ad-server" placeholder="ldaps://ad.example.com:636" @input="markProviderDirty" />
          </el-form-item>
          <el-form-item label="Base DN" required>
            <el-input v-model="adForm.base_dn" placeholder="DC=example,DC=com" @input="markProviderDirty" />
          </el-form-item>
          <el-form-item label="Bind DN" required>
            <el-input v-model="adForm.bind_dn" placeholder="CN=sms-bind,OU=Service,..." @input="markProviderDirty" />
            <small>Bind 密码只从 Docker secret 读取，本页不录入、不回显。</small>
          </el-form-item>
          <el-form-item label="用户搜索过滤器" required>
            <el-input v-model="adForm.user_search_filter" placeholder="(sAMAccountName={username})" @input="markProviderDirty" />
          </el-form-item>
          <el-form-item label="连接超时（秒）">
            <el-input-number v-model="adForm.connect_timeout_s" :min="0.1" :max="30" :step="0.5" @change="markProviderDirty" />
          </el-form-item>
          <el-form-item label="接收超时（秒）">
            <el-input-number v-model="adForm.receive_timeout_s" :min="0.1" :max="30" :step="0.5" @change="markProviderDirty" />
          </el-form-item>
        </div>

        <button type="button" class="provider-advanced-toggle" :aria-expanded="advancedOpen" @click="advancedOpen = !advancedOpen">
          <span>高级属性映射</span><small>{{ advancedOpen ? '收起' : '展开' }} LDAP 属性字段</small>
        </button>
        <div v-if="advancedOpen" class="provider-field-grid advanced">
          <el-form-item label="用户名属性"><el-input v-model="adForm.username_attribute" @input="markProviderDirty" /></el-form-item>
          <el-form-item label="显示名称属性"><el-input v-model="adForm.display_name_attribute" @input="markProviderDirty" /></el-form-item>
          <el-form-item label="部门属性"><el-input v-model="adForm.dept_attribute" @input="markProviderDirty" /></el-form-item>
          <el-form-item label="稳定主体属性"><el-input v-model="adForm.subject_attribute" @input="markProviderDirty" /></el-form-item>
          <el-form-item label="目录组属性"><el-input v-model="adForm.group_attribute" @input="markProviderDirty" /></el-form-item>
        </div>
      </el-form>

      <div class="provider-actions">
        <el-button data-testid="save-ad-draft" :loading="providerSaving" @click="saveProviderDraft">保存草稿</el-button>
        <el-button data-testid="test-ad" type="primary" plain :loading="testing" :disabled="providerDirty" @click="runProviderTest">测试连接</el-button>
        <el-button data-testid="activate-ad" type="primary" :loading="providerSaving" :disabled="!canActivateProvider" @click="activateProvider">启用配置</el-button>
        <el-button v-if="adProvider.enabled" data-testid="disable-ad" type="danger" plain :loading="providerSaving" @click="disableProvider">禁用 AD</el-button>
      </div>

      <section class="role-mapping-panel">
        <header><div><strong>目录组角色映射</strong><p>AD 账号未人工覆盖时，按最近同步的目录组计算平台角色。</p></div><el-button @click="addRoleMapping">添加映射</el-button></header>
        <div v-if="roleMappings.length" class="role-mapping-list">
          <div v-for="(mapping, index) in roleMappings" :key="index" class="role-mapping-row">
            <el-input v-model="mapping.external_group" :data-testid="`mapping-group-${index}`" placeholder="CN=SMS-Operators,OU=Groups,..." />
            <el-select v-model="mapping.role" :data-testid="`mapping-role-${index}`">
              <el-option v-for="(label, role) in roleLabels" :key="role" :label="label" :value="role" />
            </el-select>
            <el-button type="danger" link @click="removeRoleMapping(index)">移除</el-button>
          </div>
        </div>
        <p v-else class="role-mapping-empty">尚未配置目录组映射；AD 用户不会自动获得平台角色。</p>
        <footer><el-button data-testid="save-role-mappings" type="primary" :loading="mappingsSaving" @click="saveRoleMappings">保存角色映射</el-button></footer>
      </section>
    </article>
  </el-card>

  <section
    v-show="activeTab === 'runtime'"
    id="config-panel-runtime"
    role="tabpanel"
    aria-labelledby="config-tab-runtime"
    class="config-runtime-panel"
  >
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false">
      <template #default><el-button link type="primary" @click="load">重新加载</el-button></template>
    </el-alert>
    <el-alert class="config-restart-alert" title="调度生效规则" type="warning" :closable="false" show-icon description="标有 BEAT RESTART 的参数由 beat 与 API 在启动时读取，修改后需重启两个容器；不会动态热更。" />

    <section v-loading="loading" class="config-groups">
      <el-card v-for="[group, items] in groups" :key="group" shadow="never" class="config-group-card">
        <template #header><div class="config-group-title"><strong>{{ group }}</strong><span>{{ items.length }} PARAMETERS</span></div></template>
        <div class="config-grid">
          <article v-for="item in items" :key="item.key" class="config-item">
            <header><code>{{ item.key }}</code><span v-if="item.beat_restart_required" class="restart-badge">BEAT RESTART</span><span v-if="item.sensitive" class="secret-badge">SENSITIVE</span></header>
            <p>{{ item.description || "未提供参数说明" }}</p>
            <el-select v-if="item.value_type === 'bool'" v-model="values[item.key]" :data-testid="`config-${item.key}`" @change="mark(item.key)"><el-option label="开启 · true" value="true" /><el-option label="关闭 · false" value="false" /></el-select>
            <el-input v-else v-model="values[item.key]" :data-testid="`config-${item.key}`" :type="item.sensitive ? 'password' : 'text'" :show-password="item.sensitive" :placeholder="item.sensitive && item.configured ? '留空保持原值' : '输入参数值'" @input="mark(item.key)" />
            <div v-if="item.sensitive && item.configured" class="secret-control"><small>已配置，值不回显</small><el-button link type="danger" @click="values[item.key] = ''; mark(item.key)">清除配置</el-button></div><small v-else-if="item.sensitive">未配置 · 当前 log-sink</small><small v-else-if="item.updated_by">最近由 {{ item.updated_by }} 更新</small>
          </article>
        </div>
      </el-card>
    </section>
    <footer class="config-savebar"><span>{{ changes().length }} 项待保存</span><el-button type="primary" :loading="saving" @click="save">保存变更</el-button></footer>
  </section>

  <VendorTestConsole
    v-if="session.role === 'admin' && activeTab === 'vendor-test'"
    id="config-panel-vendor-test"
    role="tabpanel"
    aria-labelledby="config-tab-vendor-test"
  />
</template>
