<script setup lang="ts">
import { ElMessage, ElMessageBox } from "element-plus"
import { computed, onBeforeUnmount, onMounted, ref } from "vue"

import {
  activateVendorTest,
  disableVendorTestRecipient,
  getVendorTestOperation,
  getVendorTestStatus,
  getVendorTestUat,
  issueVendorTestStepUp,
  listVendorTestRecipients,
  pauseVendorTest,
  refreshVendorTestRecipientIndex,
  resetVendorTest,
  resumeVendorTest,
  VendorRequestError,
  type VendorTestOperation,
  type VendorTestRecipient,
  type VendorTestStatus,
} from "../api/admin"
import { listApps, type ManagedApp } from "../api/apps"
import PhoneMask from "./PhoneMask.vue"
import VendorCredentialDialog from "./VendorCredentialDialog.vue"
import VendorTestRecipientDialog from "./VendorTestRecipientDialog.vue"
import VendorTestUatPanel from "./VendorTestUatPanel.vue"

const loading = ref(false)
const loadErrorMessage = ref("")
const restoreErrorMessage = ref("")
const status = ref<VendorTestStatus | null>(null)
const recipients = ref<VendorTestRecipient[]>([])
const apps = ref<ManagedApp[]>([])
const activeOperation = ref<VendorTestOperation | null>(null)
const operationRestoring = ref(false)
const operationCompletionRefreshing = ref(false)
const credentialDialog = ref(false)
const recipientDialog = ref(false)
const refreshVisible = ref(false)
const refreshRecipient = ref<VendorTestRecipient | null>(null)
const refreshPhone = ref("")
const refreshBusy = ref(false)
const stepUpVisible = ref(false)
const stepUpPassword = ref("")
const resetConfirmation = ref("")
const stepUpAction = ref<"activate" | "reset_configuration" | "resume_critical" | null>(null)
const controlBusy = ref(false)
let pollTimer: ReturnType<typeof setTimeout> | undefined
let completionRefreshTimer: ReturnType<typeof setTimeout> | undefined
let loadGeneration = 0
let disposed = false
// 轮询连续失败只提示一次，恢复成功后重置；避免控制代理短暂不可用时每 1.6s 弹一条错误。
let pollFailureNotified = false

const OPERATION_SESSION_KEY = "sms-platform:vendor-test:operation:v1"
const RESET_CONFIRMATION = "切回Mock"
const OPERATION_TYPES = new Set<VendorTestOperation["operation_type"]>([
  "install_credentials",
  "rotate_credentials",
  "activate",
  "pause",
  "resume",
  "reset_configuration",
  "uat_send",
])

const activeRecipients = computed(() => recipients.value.filter((item) => item.status === "active"))
const errorMessage = computed(() => restoreErrorMessage.value || loadErrorMessage.value)
const activeApps = computed(() => apps.value.filter((item) => item.status === 1))
const isControlled = computed(() => status.value?.mode === "controlled")
const operationBusy = computed(
  () => operationRestoring.value
    || operationCompletionRefreshing.value
    || activeOperation.value?.status === "requested"
    || activeOperation.value?.status === "running",
)
const credentialOperation = computed(() =>
  status.value?.credential_configured ? "rotate_credentials" : "install_credentials",
)
const resetAvailable = computed(() => {
  if (!status.value) return false
  if (status.value.pause_kind !== null) return false
  return ["inactive", "controlled"].includes(status.value.mode)
    && status.value.credential_configured
})
const resetOperationPending = computed(() =>
  activeOperation.value?.operation_type === "reset_configuration"
  && (activeOperation.value.status === "requested" || activeOperation.value.status === "running"),
)
const resetOperationFailed = computed(() =>
  activeOperation.value?.operation_type === "reset_configuration"
  && activeOperation.value.status === "failed",
)

const statusPresentation = computed(() => {
  if (!status.value) return { title: "状态读取中", detail: "正在连接本机控制代理", tone: "neutral" }
  if (status.value.pause_kind === "daily") {
    return { title: "日预算已封顶", detail: "达到 100 条后当日不可人工恢复", tone: "danger" }
  }
  if (status.value.mode === "setup_required") {
    return { title: "待完成设置", detail: "联调未激活，请先安装正式凭据并登记测试号码", tone: "warning" }
  }
  if (status.value.mode === "inactive") {
    return { title: "待激活", detail: "真实出口保持关闭，完成检查后可二次认证激活", tone: "neutral" }
  }
  if (status.value.mode === "controlled") {
    return { title: "受控联调中", detail: "仅登记号码可通过真实运营商出口发送", tone: "success" }
  }
  return { title: "安全阻断", detail: "需先完成错误处置，再按暂停类型恢复", tone: "danger" }
})

function safeTime(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return "状态时间无效"
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(parsed)
}

async function load(): Promise<boolean> {
  const generation = ++loadGeneration
  loading.value = true
  loadErrorMessage.value = ""
  try {
    const [nextStatus, nextRecipients, nextApps] = await Promise.all([
      getVendorTestStatus(),
      listVendorTestRecipients(),
      listApps(),
    ])
    if (disposed || generation !== loadGeneration) return false
    status.value = nextStatus
    recipients.value = nextRecipients
    apps.value = nextApps
    return true
  } catch (error) {
    if (!disposed && generation === loadGeneration) {
      loadErrorMessage.value = error instanceof Error ? error.message : "真实联调状态加载失败"
    }
    return false
  } finally {
    if (!disposed && generation === loadGeneration) loading.value = false
  }
}

function stopPolling(): void {
  if (pollTimer !== undefined) clearTimeout(pollTimer)
  pollTimer = undefined
}

function terminal(operation: VendorTestOperation): boolean {
  return operation.status === "succeeded" || operation.status === "failed"
}

function rememberOperation(operation: VendorTestOperation): void {
  try {
    sessionStorage.setItem(
      OPERATION_SESSION_KEY,
      JSON.stringify({
        operation_id: operation.operation_id,
        operation_type: operation.operation_type,
      }),
    )
  } catch {
    // 浏览器禁用 sessionStorage 时仍可在当前页面跟踪，不降低控制操作安全性。
  }
}

function forgetOperation(): void {
  try {
    sessionStorage.removeItem(OPERATION_SESSION_KEY)
  } catch {
    // 与写入相同：存储能力不可用时不影响当前页的受控操作。
  }
}

function isGoneOperation(error: unknown): boolean {
  return error instanceof VendorRequestError && (error.status === 404 || error.status === 410)
}

function rememberedOperation(): Pick<VendorTestOperation, "operation_id" | "operation_type"> | null {
  try {
    const raw = sessionStorage.getItem(OPERATION_SESSION_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (typeof parsed !== "object" || parsed === null) throw new Error("invalid operation")
    const candidate = parsed as Record<string, unknown>
    if (
      Object.keys(candidate).sort().join(",") !== "operation_id,operation_type"
      || typeof candidate.operation_id !== "string"
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(candidate.operation_id)
      || typeof candidate.operation_type !== "string"
      || !OPERATION_TYPES.has(candidate.operation_type as VendorTestOperation["operation_type"])
    ) {
      throw new Error("invalid operation")
    }
    return {
      operation_id: candidate.operation_id,
      operation_type: candidate.operation_type as VendorTestOperation["operation_type"],
    }
  } catch {
    forgetOperation()
    return null
  }
}

async function restoreOperation(): Promise<void> {
  const remembered = rememberedOperation()
  if (!remembered) return
  operationRestoring.value = true
  try {
    const operation = remembered.operation_type === "uat_send"
      ? await getVendorTestUat(remembered.operation_id)
      : await getVendorTestOperation(remembered.operation_id)
    if (disposed) return
    activeOperation.value = operation
    restoreErrorMessage.value = ""
    operationRestoring.value = false
    if (terminal(operation)) finishOperation(operation)
    else pollTimer = setTimeout(() => void pollOperation(), 800)
  } catch (error) {
    if (disposed) return
    if (isGoneOperation(error)) {
      forgetOperation()
      operationRestoring.value = false
      restoreErrorMessage.value = error instanceof Error ? error.message : "上次操作已不存在，已停止恢复"
      return
    }
    restoreErrorMessage.value = error instanceof Error ? error.message : "操作状态恢复失败"
    pollTimer = setTimeout(() => void restoreOperation(), 1600)
  }
}

async function refreshCompletedProjection(): Promise<void> {
  operationCompletionRefreshing.value = true
  const refreshed = await load()
  if (disposed) return
  if (refreshed) {
    forgetOperation()
    operationCompletionRefreshing.value = false
    completionRefreshTimer = undefined
    return
  }
  completionRefreshTimer = setTimeout(() => void refreshCompletedProjection(), 1600)
}

function finishOperation(operation: VendorTestOperation): void {
  stopPolling()
  if (operation.status === "failed") {
    if (operation.operation_type === "reset_configuration") {
      ElMessage.error(
        `切回 Mock 未确认完成：${operation.safe_code || "RESET_FAILED"}；`
        + "测试环境可能处于部分切换状态，请勿发送，并按安全代码恢复同一操作",
      )
    } else if (operation.vendor_code !== null) {
      ElMessage.error(`运营商返回错误代码 ${operation.vendor_code}`)
    } else {
      ElMessage.error(`受控操作失败：${operation.safe_code || "CONTROL_OPERATION_FAILED"}`)
    }
  } else {
    ElMessage.success(
      operation.operation_type === "uat_send"
        ? "真实 UAT 已被运营商受理"
        : operation.operation_type === "reset_configuration"
          ? "测试环境已切回 Mock，正式厂商凭据已撤销；测试号码与生产环境未变"
          : "受控操作成功",
    )
  }
  void refreshCompletedProjection()
}

async function pollOperation(): Promise<void> {
  const current = activeOperation.value
  if (!current || disposed) return
  try {
    const next = current.operation_type === "uat_send"
      ? await getVendorTestUat(current.operation_id)
      : await getVendorTestOperation(current.operation_id)
    if (disposed || activeOperation.value?.operation_id !== next.operation_id) return
    pollFailureNotified = false
    activeOperation.value = next
    if (terminal(next)) finishOperation(next)
    else pollTimer = setTimeout(() => void pollOperation(), 800)
  } catch (error) {
    if (disposed) return
    if (!pollFailureNotified) {
      pollFailureNotified = true
      ElMessage.error(error instanceof Error ? error.message : "操作状态查询失败")
    }
    pollTimer = setTimeout(() => void pollOperation(), 1600)
  }
}

function trackOperation(operation: VendorTestOperation): void {
  stopPolling()
  pollFailureNotified = false
  rememberOperation(operation)
  activeOperation.value = operation
  if (terminal(operation)) finishOperation(operation)
  else pollTimer = setTimeout(() => void pollOperation(), 800)
}

async function requestActivation(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "确认正式凭据已安装、至少登记一个自有测试号码，并理解激活后仅允许系统配置页单号码 UAT。",
      "激活真实运营商受控联调",
      { type: "warning", confirmButtonText: "进入二次认证", cancelButtonText: "继续检查" },
    )
    stepUpAction.value = "activate"
    stepUpVisible.value = true
  } catch {
    // 操作者保留当前关闭状态。
  }
}

function clearStepUpSecrets(): void {
  stepUpPassword.value = ""
  resetConfirmation.value = ""
}

function clearStepUp(): void {
  clearStepUpSecrets()
  stepUpAction.value = null
}

function closeStepUp(): void {
  clearStepUpSecrets()
  stepUpVisible.value = false
}

function requestReset(): void {
  if (!resetAvailable.value || operationBusy.value || controlBusy.value) return
  clearStepUp()
  stepUpAction.value = "reset_configuration"
  stepUpVisible.value = true
}

async function submitStepUp(): Promise<void> {
  if (controlBusy.value) return
  const action = stepUpAction.value
  if (!action || !stepUpPassword.value) {
    ElMessage.warning("请输入当前账号密码")
    return
  }
  if (action === "reset_configuration" && resetConfirmation.value !== RESET_CONFIRMATION) {
    ElMessage.warning(`请输入精确短语“${RESET_CONFIRMATION}”`)
    clearStepUpSecrets()
    return
  }
  controlBusy.value = true
  try {
    const token = await issueVendorTestStepUp(action, stepUpPassword.value)
    const operation = action === "activate"
      ? await activateVendorTest(token.token)
      : action === "reset_configuration"
        ? await resetVendorTest(token.token)
        : await resumeVendorTest(token.token)
    stepUpVisible.value = false
    trackOperation(operation)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "二次认证操作失败")
  } finally {
    clearStepUpSecrets()
    controlBusy.value = false
  }
}

async function pause(): Promise<void> {
  try {
    await ElMessageBox.confirm(
      "暂停后真实出口立即关闭；已提交或结果未知的批次不会自动重发。",
      "人工暂停真实联调",
      { type: "warning", confirmButtonText: "立即暂停", cancelButtonText: "保持运行" },
    )
    controlBusy.value = true
    trackOperation(await pauseVendorTest())
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "人工暂停失败")
    }
  } finally {
    controlBusy.value = false
  }
}

async function resume(): Promise<void> {
  if (status.value?.pause_kind === "daily") {
    ElMessage.warning("每日 100 条预算已用尽，只能等待次日自动重置")
    return
  }
  if (status.value?.pause_kind === "critical") {
    try {
      await ElMessageBox.confirm(
        "确认已完成余额或运营商错误处置。恢复前系统会再次检查余额。",
        "恢复安全阻断",
        { type: "warning", confirmButtonText: "进入二次认证", cancelButtonText: "继续阻断" },
      )
      stepUpAction.value = "resume_critical"
      stepUpVisible.value = true
    } catch {
      // 保持安全阻断。
    }
    return
  }
  try {
    await ElMessageBox.confirm(
      "确认恢复人工暂停并重新开放已登记号码的真实 UAT。",
      "恢复受控联调",
      { type: "warning", confirmButtonText: "恢复联调", cancelButtonText: "继续暂停" },
    )
    controlBusy.value = true
    trackOperation(await resumeVendorTest())
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "恢复联调失败")
    }
  } finally {
    controlBusy.value = false
  }
}

function recipientAdded(recipient: VendorTestRecipient): void {
  recipients.value = [recipient, ...recipients.value.filter((item) => item.id !== recipient.id)]
  if (status.value) status.value = { ...status.value, active_recipient_count: activeRecipients.value.length }
}

async function disableRecipient(recipient: VendorTestRecipient): Promise<void> {
  try {
    await ElMessageBox.confirm(
      `停用 ${recipient.label}（${recipient.phone_mask}）后不可再用于真实 UAT。`,
      "停用测试号码",
      { type: "warning", confirmButtonText: "停用号码", cancelButtonText: "保留号码" },
    )
    const disabled = await disableVendorTestRecipient(recipient.id)
    recipients.value = recipients.value.map((item) => (item.id === disabled.id ? disabled : item))
    if (status.value) status.value = { ...status.value, active_recipient_count: activeRecipients.value.length }
  } catch (error) {
    if (error !== "cancel" && error !== "close") {
      ElMessage.error(error instanceof Error ? error.message : "测试号码停用失败")
    }
  }
}

function openIndexRefresh(recipient: VendorTestRecipient): void {
  refreshRecipient.value = recipient
  refreshPhone.value = ""
  refreshVisible.value = true
}

function clearIndexRefresh(): void {
  refreshPhone.value = ""
  refreshRecipient.value = null
}

async function submitIndexRefresh(): Promise<void> {
  const recipient = refreshRecipient.value
  const phone = refreshPhone.value
  if (!recipient || !/^1\d{10}$/.test(phone)) {
    ElMessage.warning("请输入 11 位测试手机号")
    return
  }
  refreshBusy.value = true
  try {
    const refreshed = await refreshVendorTestRecipientIndex(recipient.id, phone)
    recipients.value = recipients.value.map((item) =>
      item.id === refreshed.id ? refreshed : item,
    )
    refreshVisible.value = false
    ElMessage.success("号码索引已覆盖当前全部密钥版本")
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : "号码索引刷新失败")
  } finally {
    refreshPhone.value = ""
    refreshBusy.value = false
  }
}

onMounted(() => {
  void restoreOperation()
  void load()
})
onBeforeUnmount(() => {
  disposed = true
  stopPolling()
  if (completionRefreshTimer !== undefined) clearTimeout(completionRefreshTimer)
  completionRefreshTimer = undefined
  clearStepUp()
})
</script>

<template>
  <section v-loading="loading" class="vendor-test-console" aria-labelledby="vendor-test-heading">
    <el-alert v-if="errorMessage" :title="errorMessage" type="error" :closable="false" show-icon>
      <template #default><el-button link type="primary" @click="load">重新连接控制代理</el-button></template>
    </el-alert>

    <header class="vendor-test-status" :class="`is-${statusPresentation.tone}`">
      <div class="vendor-test-state-mark" aria-hidden="true"><i></i><span>LIVE</span></div>
      <div>
        <p class="eyebrow">CONTROLLED CARRIER LINK</p>
        <h2 id="vendor-test-heading">{{ statusPresentation.title }}</h2>
        <p>{{ statusPresentation.detail }}</p>
      </div>
      <dl v-if="status">
        <div><dt>凭据</dt><dd>{{ status.credential_configured ? '正式凭据已安装' : '正式凭据未安装' }}</dd></div>
        <div><dt>收件人</dt><dd>{{ status.active_recipient_count }} 个已登记</dd></div>
        <div><dt>预算</dt><dd>{{ status.daily_limit }} 条/日</dd></div>
        <div><dt>心跳</dt><dd>{{ safeTime(status.heartbeat_at) }}</dd></div>
      </dl>
    </header>

    <div class="vendor-safety-line" role="note">
      <span><i></i>仅系统配置页入口</span>
      <span><i></i>仅登记号码</span>
      <span><i></i>超时不自动重发</span>
      <span><i></i>运营商报备默认已完成，返回错误时仅告知代码</span>
    </div>

    <div class="vendor-test-layout">
      <section class="vendor-control-panel" aria-labelledby="vendor-control-title">
        <header>
          <div><p class="eyebrow">SAFETY CONTROLS</p><h3 id="vendor-control-title">联调控制</h3></div>
          <span>单机控制代理</span>
        </header>

        <div class="vendor-readiness-list">
          <div :class="{ ready: status?.credential_configured }">
            <span>01</span><div><strong>正式凭据</strong><small>{{ status?.credential_configured ? '已安装，不回显任何值' : '尚未安装' }}</small></div>
          </div>
          <div :class="{ ready: activeRecipients.length > 0 }">
            <span>02</span><div><strong>测试收件人</strong><small>{{ activeRecipients.length }} 个有效掩码号码</small></div>
          </div>
          <div :class="{ ready: isControlled }">
            <span>03</span><div><strong>真实出口</strong><small>{{ isControlled ? '已受控开放' : '保持关闭' }}</small></div>
          </div>
        </div>

        <div class="vendor-test-actions">
          <el-button
            data-testid="vendor-credentials"
            :disabled="operationBusy"
            @click="credentialDialog = true"
          >{{ status?.credential_configured ? '轮换正式凭据' : '安装正式凭据' }}</el-button>
          <el-button
            v-if="status?.mode === 'inactive'"
            data-testid="vendor-activate"
            type="primary"
            :disabled="!status.credential_configured || activeRecipients.length < 1 || operationBusy"
            @click="requestActivation"
          >二次认证并激活</el-button>
          <el-button
            v-if="resetAvailable"
            data-testid="vendor-reset"
            class="vendor-reset-trigger"
            type="danger"
            plain
            :disabled="operationBusy || controlBusy"
            @click="requestReset"
          >切回 Mock</el-button>
          <el-button
            v-if="isControlled"
            data-testid="vendor-pause"
            type="danger"
            plain
            :disabled="operationBusy || controlBusy"
            @click="pause"
          >人工暂停</el-button>
          <el-button
            v-if="status?.mode === 'blocked'"
            data-testid="vendor-resume"
            type="primary"
            :disabled="status.pause_kind === 'daily' || operationBusy || controlBusy"
            @click="resume"
          >{{ status.pause_kind === 'critical' ? '处置后认证恢复' : '恢复联调' }}</el-button>
        </div>

        <section class="vendor-recipient-panel">
          <header>
            <div><strong>测试收件人</strong><small>持久层与页面均不回显明文</small></div>
            <el-button
              data-testid="vendor-add-recipient"
              :disabled="isControlled || operationBusy"
              @click="recipientDialog = true"
            >登记号码</el-button>
          </header>
          <div v-if="recipients.length" class="vendor-recipient-list">
            <article v-for="recipient in recipients" :key="recipient.id">
              <div><strong>{{ recipient.label }}</strong><PhoneMask :value="recipient.phone_mask" /></div>
              <el-tag :type="recipient.status === 'active' ? 'success' : 'info'" size="small">
                {{ recipient.status === 'active' ? '有效' : '已停用' }}
              </el-tag>
              <div v-if="recipient.status === 'active'" class="vendor-recipient-actions">
                <el-button
                  link
                  type="primary"
                  :data-testid="`vendor-refresh-recipient-${recipient.id}`"
                  :disabled="operationBusy"
                  @click="openIndexRefresh(recipient)"
                >刷新索引</el-button>
                <el-button
                  link
                  type="danger"
                  :disabled="isControlled || operationBusy"
                  @click="disableRecipient(recipient)"
                >停用</el-button>
              </div>
            </article>
          </div>
          <div v-else class="vendor-empty-state">
            <strong>尚未登记测试号码</strong><p>登记自有号码后，真实出口仍保持关闭，需另行激活。</p>
          </div>
        </section>
      </section>

      <VendorTestUatPanel
        :disabled="!isControlled || operationBusy || activeRecipients.length < 1"
        :recipients="activeRecipients"
        :apps="activeApps"
        :daily-limit="status?.daily_limit ?? 100"
        @operation="trackOperation"
      />
    </div>

    <section v-if="activeOperation" class="vendor-operation-strip" aria-live="polite">
      <div>
        <span>最近操作</span>
        <strong>{{ activeOperation.operation_type }}</strong>
        <code>{{ activeOperation.operation_id }}</code>
      </div>
      <div class="vendor-operation-state" :class="activeOperation.status">
        <i></i><strong>{{ activeOperation.status === 'succeeded' ? '操作成功' : activeOperation.status }}</strong>
      </div>
      <div v-if="activeOperation.batch_no"><span>批次引用</span><code>{{ activeOperation.batch_no }}</code></div>
      <div v-if="activeOperation.safe_code"><span>安全代码</span><code>{{ activeOperation.safe_code }}</code></div>
      <div v-if="activeOperation.vendor_code !== null"><span>运营商错误代码</span><code>{{ activeOperation.vendor_code }}</code></div>
      <p v-if="resetOperationPending" class="vendor-operation-guidance">
        正在切回 Mock，请勿发送或重复操作；切换前历史未决记录会保留。
      </p>
      <p v-if="resetOperationFailed" class="vendor-operation-guidance is-danger">
        切回 Mock 未确认完成，测试环境可能处于部分切换状态；请勿发送，并按安全代码恢复同一操作。
      </p>
    </section>

    <VendorCredentialDialog
      v-model="credentialDialog"
      :operation="credentialOperation"
      @operation="trackOperation"
    />
    <VendorTestRecipientDialog v-model="recipientDialog" @added="recipientAdded" />

    <el-dialog
      v-model="refreshVisible"
      title="刷新号码索引"
      width="440px"
      destroy-on-close
      append-to-body
      @closed="clearIndexRefresh"
    >
      <div v-if="refreshVisible" class="vendor-sensitive-form">
        <p>
          数据密钥轮换后，请重新输入 <PhoneMask v-if="refreshRecipient" :value="refreshRecipient.phone_mask" /> 对应的同一号码。
          系统只重建跨版本 HMAC 索引，不解密或回显历史号码。
        </p>
        <el-input
          v-model="refreshPhone"
          data-testid="vendor-refresh-phone"
          inputmode="numeric"
          maxlength="11"
          autocomplete="off"
          spellcheck="false"
          placeholder="请输入同一测试手机号"
          @keyup.enter="submitIndexRefresh"
        />
      </div>
      <template #footer>
        <el-button :disabled="refreshBusy" @click="refreshVisible = false">保留现状</el-button>
        <el-button
          data-testid="vendor-refresh-submit"
          type="primary"
          :loading="refreshBusy"
          @click="submitIndexRefresh"
        >确认刷新</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="stepUpVisible"
      :title="stepUpAction === 'activate'
        ? '二次认证激活'
        : stepUpAction === 'reset_configuration'
          ? '切回 Mock'
          : '二次认证恢复'"
      width="440px"
      destroy-on-close
      append-to-body
      class="vendor-step-up-dialog"
      :close-on-click-modal="!controlBusy"
      :close-on-press-escape="!controlBusy"
      :show-close="!controlBusy"
      @close="clearStepUpSecrets"
      @closed="clearStepUp"
    >
      <div v-if="stepUpVisible" class="vendor-sensitive-form">
        <el-alert
          v-if="stepUpAction === 'reset_configuration'"
          id="vendor-reset-consequences"
          title="仅影响测试环境的厂商连接，操作不可撤销"
          type="error"
          :closable="false"
          show-icon
        >
          <p>
            测试环境将停止真实发送与厂商状态/回复拉取，切回本机 Mock，并删除测试环境的正式厂商凭据
            全部版本。生产环境的配置、凭据、服务和数据不受影响。
          </p>
          <p>
            保留全部加密测试号码及其索引，也保留管理员、短信业务数据、审计记录、当日 UAT 用量、
            uncertain 占额、数据库、Docker volume 和运行态目录；切换前已发送、待回执、uncertain
            或被错误环境消费的历史状态不会自动修复。这不是系统初始化。
          </p>
        </el-alert>
        <p>请输入当前登录账号密码。认证令牌五分钟内单次有效，不写入浏览器存储。</p>
        <el-form label-position="top" @submit.prevent="submitStepUp">
          <el-form-item label="当前 Provider 密码" required>
            <el-input
              v-model="stepUpPassword"
              :data-testid="stepUpAction === 'reset_configuration'
                ? 'vendor-reset-password'
                : 'vendor-step-up-password'"
              type="password"
              autocomplete="current-password"
              spellcheck="false"
              show-password
            />
          </el-form-item>
          <el-form-item
            v-if="stepUpAction === 'reset_configuration'"
            :label="`输入“${RESET_CONFIRMATION}”确认`"
            required
          >
            <el-input
              v-model="resetConfirmation"
              data-testid="vendor-reset-confirmation"
              autocomplete="off"
              spellcheck="false"
              aria-describedby="vendor-reset-consequences"
            />
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button
          :data-testid="stepUpAction === 'reset_configuration' ? 'vendor-reset-cancel' : undefined"
          :disabled="controlBusy"
          @click="closeStepUp"
        >{{ stepUpAction === 'reset_configuration' ? '保留现状' : '继续检查' }}</el-button>
        <el-button
          :data-testid="stepUpAction === 'reset_configuration' ? 'vendor-reset-submit' : undefined"
          :type="stepUpAction === 'reset_configuration' ? 'danger' : 'primary'"
          :loading="controlBusy"
          @click="submitStepUp"
        >
          {{ stepUpAction === 'activate'
            ? '验证并激活'
            : stepUpAction === 'reset_configuration'
              ? '验证并切回 Mock'
              : '验证并恢复' }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style>
.vendor-step-up-dialog {
  max-width: calc(100vw - 32px);
}

.vendor-step-up-dialog .el-input__wrapper,
.vendor-step-up-dialog .el-dialog__footer .el-button,
.vendor-reset-trigger {
  min-height: 44px;
}

.vendor-step-up-dialog .el-dialog__footer {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: flex-end;
}

.vendor-step-up-dialog .el-dialog__footer .el-button + .el-button {
  margin-left: 0;
}

.vendor-operation-guidance {
  grid-column: 1 / -1;
  margin: 0;
  padding: 10px 12px;
  background: var(--surface);
  color: var(--ink-soft);
  font-size: 11px;
  line-height: 1.6;
}

.vendor-operation-guidance.is-danger {
  color: var(--red);
}

@media (max-width: 360px) {
  .vendor-step-up-dialog .el-dialog__footer .el-button {
    flex: 1 1 100%;
    margin-left: 0;
  }
}
</style>
