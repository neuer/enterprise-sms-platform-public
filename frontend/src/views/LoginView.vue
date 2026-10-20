<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { useRouter } from "vue-router"

import { AuthApiError, initialPasswordChangeRequest, passwordPolicyRequest, type PasswordPolicy } from "../api/auth"
import { ACCESS_ONLY_SESSION_MESSAGE, isAccessOnlySessionMode } from "../api/refreshLock"
import { EGRET_PATHS, EGRET_SYMBOL_ID, EGRET_VIEWBOX } from "../assets/brand/egretPaths"
import LoginChatHead from "../components/LoginChatHead.vue"
import LoginEgret from "../components/LoginEgret.vue"
import LoginHero from "../components/LoginHero.vue"
import LoginThreadItem from "../components/LoginThreadItem.vue"
import { useLatestRead } from "../composables/useLatestRead"
import { useLoginDisplay } from "../composables/useLoginDisplay"
import { useLoginThread } from "../composables/useLoginThread"
import { errorText } from "../lib/error"
import {
  DEFAULT_POLICY,
  INPUT_STEPS,
  PROVIDER_ORDER,
  SUCCESS_PAUSE_MS,
  passwordRules,
  type Chip,
  type ChipsItem,
  type Mood,
  type ProviderCode,
  type Step,
} from "../lib/loginConversation"
import { recallLoginProvider, rememberLoginProvider } from "../lib/loginProvider"
import { useSessionStore } from "../stores/session"

/** 本期固定目录：始终画出 local / AD，服务端未返回的标未开通。 */
const PROVIDER_CATALOG: Record<ProviderCode, { name: string; ask: string; offHint: string }> = {
  local: { name: "本地账号", ask: "账号", offHint: "本地账号尚未开通，暂时只能用 AD 账号登录。" },
  ad: { name: "AD 账号", ask: "企业 AD 账号", offHint: "企业目录尚未开通，暂时只能用本地账号登录。" },
}
/** 密码气泡固定圆点数，不暴露真实长度。 */
const SECRET_DOTS = "••••••••"

const router = useRouter()
const session = useSessionStore()
const accessOnlySession = isAccessOnlySessionMode()
const policyRead = useLatestRead()

const step = ref<Step>("loading")
const mood = ref<Mood>("idle")
const typing = ref(false)
const netWarn = ref(false)
const providerCode = ref<ProviderCode | "">("")
const returning = ref(false)
const account = ref("")
const username = ref("")
const password = ref("")
const capsLock = ref(false)
const policy = ref<PasswordPolicy>(DEFAULT_POLICY)
const ruleState = ref<boolean[]>([])
/** 账号输入时白鹭点头：1/2 交替以重启动画；密码步不点头，不随按键给出任何可见反馈。 */
const nod = ref<0 | 1 | 2>(0)
const usernameInput = ref<HTMLInputElement | null>(null)
const passwordInput = ref<HTMLInputElement | null>(null)

let returnProvider: ProviderCode = "local"
let helpOffered = false
/** 首次改密短令牌与两步确认间的新密码：仅存本组件易失内存，完成、失效或卸载即清空。 */
let pendingChange: { token: string; expiresAt: number } | null = null
let draftPassword = ""
let changeTimer = 0
let disposed = false

const { items, threadRef, leadsGroup, push, bot, sys, chips, me, markRead, markUndelivered } = useLoginThread(netWarn)
const {
  secretStep,
  composerOff,
  editable,
  fieldLabel,
  statusText,
  sayLines,
  poseClass,
  progress,
  fieldTag,
  secretTag,
  openedAt,
} = useLoginDisplay({ step, mood, typing, netWarn, providerCode, nod }, (code) => PROVIDER_CATALOG[code].ask)
const rules = computed(() => passwordRules(policy.value, () => account.value))

function providerName(code: ProviderCode): string {
  return session.providers.find((provider) => provider.code === code)?.name ?? PROVIDER_CATALOG[code].name
}

function isEnabled(code: ProviderCode): boolean {
  return session.providers.some((provider) => provider.code === code)
}

function setStep(next: Step): void {
  step.value = next
  capsLock.value = false
  if (disposed || !INPUT_STEPS.includes(next)) return
  void nextTick(() => (next === "account" ? usernameInput.value : passwordInput.value)?.focus())
}

function pause(ms: number): Promise<void> {
  if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return Promise.resolve()
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

/** 开场：读取认证源；回访且记忆源仍开通时直接要账号，否则请用户显式选择。 */
async function openConversation(): Promise<void> {
  clearInitialChange()
  items.value = []
  helpOffered = false
  returning.value = false
  providerCode.value = ""
  account.value = ""
  username.value = ""
  password.value = ""
  netWarn.value = false
  mood.value = "idle"
  if (accessOnlySession) sys(ACCESS_ONLY_SESSION_MESSAGE, undefined, "login-access-only")
  setStep("loading")
  typing.value = true
  try {
    await session.loadProviders()
  } catch (error) {
    typing.value = false
    netWarn.value = true
    mood.value = "lost"
    bot([errorText(error, "认证源列表加载失败")], "err")
    chips([{ label: "重试", action: { kind: "retry" } }])
    setStep("blocked")
    return
  }
  typing.value = false
  if (!PROVIDER_ORDER.some(isEnabled)) {
    mood.value = "lost"
    bot(["暂无可用的登录方式，请稍后重试或联系管理员。"], "err")
    chips([{ label: "重试", action: { kind: "retry" } }])
    setStep("blocked")
    return
  }
  const remembered = recallLoginProvider()
  if ((remembered === "local" || remembered === "ad") && isEnabled(remembered)) {
    returning.value = true
    returnProvider = remembered
    providerCode.value = remembered
    const other: ProviderCode = remembered === "ad" ? "local" : "ad"
    bot(["欢迎回来。上次用的是", { strong: providerName(remembered) }, "，直接发送账号就行。"])
    if (isEnabled(other)) {
      chips([{ label: `换成 ${providerName(other)}`, quiet: true, action: { kind: "provider", code: other } }])
    }
    mood.value = "back"
    setStep("account")
    return
  }
  bot(["你好，我是青鸾。"])
  bot(["请选择登录方式："])
  chips(
    PROVIDER_ORDER.map((code) => ({
      label: providerName(code),
      off: !isEnabled(code),
      action: { kind: "provider", code },
    })),
  )
  setStep("choose")
}

function runChip(item: ChipsItem, chip: Chip): void {
  if (item.used) return
  if (chip.action.kind === "retry") {
    item.used = true
    void openConversation()
  } else if (chip.action.kind === "forgot") {
    item.used = true
    me("忘记密码？")
    markRead()
    bot(
      providerCode.value === "ad"
        ? ["AD 账号的密码由企业目录管理，请按公司流程修改，或联系 IT 服务台。"]
        : ["本地账号的密码由系统管理员重置。重置后你会拿到一个临时密码，首次登录时再改成自己的新密码。"],
    )
  } else {
    chooseProvider(chip.action.code, item)
  }
}

/** 显式选择认证源；未开通的只提示、不改当前认证源，提交只走所选 Provider。 */
function chooseProvider(code: ProviderCode, item: ChipsItem): void {
  if (!(step.value === "choose" || (returning.value && step.value === "account"))) return
  if (!isEnabled(code)) {
    bot([PROVIDER_CATALOG[code].offHint])
    return
  }
  item.used = true
  providerCode.value = code
  rememberLoginProvider(code)
  me(returning.value ? `换成 ${providerName(code)}` : providerName(code), { edit: "provider" })
  markRead()
  bot(["好的，请发送你的", { strong: PROVIDER_CATALOG[code].ask }, "。"])
  mood.value = "listen"
  setStep("account")
}

/** 点自己发过的认证源或账号即回到那一步重新发送，之后的对话一并撤回。 */
function editFrom(index: number): void {
  const item = items.value[index]
  if (!editable.value || item?.type !== "me" || !item.edit) return
  items.value = items.value.slice(0, index)
  for (const rest of items.value) if (rest.type === "me") rest.receipt = ""
  const tail = items.value[items.value.length - 1]
  if (tail?.type === "chips") tail.used = false
  helpOffered = items.value.some(
    (rest) => rest.type === "chips" && rest.chips.some((chip) => chip.action.kind === "forgot"),
  )
  password.value = ""
  if (item.edit === "account") {
    username.value = account.value
    mood.value = "listen"
    setStep("account")
    void nextTick(() => usernameInput.value?.select())
  } else if (returning.value) {
    providerCode.value = returnProvider
    mood.value = "listen"
    setStep("account")
  } else {
    providerCode.value = ""
    mood.value = "idle"
    setStep("choose")
  }
}

function submit(): void {
  if (step.value === "account") submitAccount()
  else if (step.value === "password") void submitPassword()
  else if (step.value === "new-password") submitNewPassword()
  else if (step.value === "confirm-password") void submitConfirm()
}

function acceptAccount(): boolean {
  const value = username.value.trim()
  if (!value || !providerCode.value) return false
  account.value = value
  for (const item of items.value) if (item.type === "chips") item.used = true
  me(value, { edit: "account" })
  return true
}

function submitAccount(): void {
  if (!acceptAccount()) return
  markRead()
  bot(["收到。再发送", { strong: "密码" }, "，只有你能看到它。"])
  mood.value = "shy"
  setStep("password")
}

async function submitPassword(): Promise<void> {
  const secret = password.value
  password.value = ""
  if (!secret) return
  me(SECRET_DOTS, { secret: true })
  await verify(secret)
}

/** 浏览器密码管理器在账号步一次填好账号与密码时，直接进入验证。 */
function onPasswordInput(): void {
  if (step.value === "account" && password.value && username.value.trim()) {
    const secret = password.value
    password.value = ""
    if (!acceptAccount()) return
    me(SECRET_DOTS, { secret: true })
    sys("已使用浏览器保存的密码")
    void verify(secret)
    return
  }
  if (step.value === "new-password") ruleState.value = rules.value.map((rule) => rule.test(password.value))
  if (INPUT_STEPS.includes(step.value) && step.value !== "account") mood.value = "shy"
}

function onAccountInput(): void {
  mood.value = "listen"
  nod.value = nod.value === 1 ? 2 : 1
}

function onPasswordKey(event: KeyboardEvent): void {
  capsLock.value = event.getModifierState?.("CapsLock") ?? false
}

async function verify(secret: string): Promise<void> {
  const code = providerCode.value
  if (!code) return
  setStep("verifying")
  mood.value = "think"
  typing.value = true
  try {
    const result = await session.login(code, account.value, secret)
    typing.value = false
    netWarn.value = false
    markRead()
    if (result.nextAction === "change_password") {
      beginInitialChange(result.changeToken, result.expiresAt)
      return
    }
    bot(["验证通过，正在进入工作台…"], "ok")
    mood.value = "fly"
    setStep("done")
    await pause(SUCCESS_PAUSE_MS)
    await router.replace("/dashboard")
  } catch (error) {
    typing.value = false
    const message = errorText(error, "登录失败，请稍后重试")
    if (!(error instanceof AuthApiError) || error.status >= 500) {
      markUndelivered(message)
      mood.value = "lost"
    } else {
      netWarn.value = false
      markRead()
      bot([message], "err")
      if (error.status === 423 || error.status === 429) {
        mood.value = "rest"
      } else {
        mood.value = "oops"
        if (!helpOffered) {
          helpOffered = true
          chips([{ label: "忘记密码？", quiet: true, action: { kind: "forgot" } }])
        }
      }
    }
    setStep("password")
  }
}

function beginInitialChange(token: string, expiresAt: number): void {
  const remaining = expiresAt - Date.now()
  if (!token || !Number.isFinite(remaining) || remaining <= 0) {
    bot(["改密会话已过期，请重新登录。"], "err")
    mood.value = "oops"
    setStep("password")
    return
  }
  pendingChange = { token, expiresAt }
  changeTimer = window.setTimeout(expireInitialChange, remaining)
  ruleState.value = []
  bot(["验证通过。这是你第一次登录，需要先设置一个新密码。"], "ok")
  push({ type: "rules" })
  mood.value = "shy"
  setStep("new-password")
  void loadPolicy()
}

async function loadPolicy(): Promise<void> {
  const signal = policyRead.start()
  try {
    const latest = await passwordPolicyRequest(signal)
    if (!signal.aborted) policy.value = latest
  } catch {
    // 保留与服务端相同的内置规则文案；提交仍由服务端做权威校验。
  }
}

function clearInitialChange(): void {
  window.clearTimeout(changeTimer)
  changeTimer = 0
  pendingChange = null
  draftPassword = ""
}

function expireInitialChange(): void {
  clearInitialChange()
  password.value = ""
  bot(["改密会话已过期，请重新登录。"], "err")
  mood.value = "oops"
  setStep("password")
}

function submitNewPassword(): void {
  const value = password.value
  password.value = ""
  if (!value) return
  const missing = rules.value.filter((rule) => !rule.test(value)).map((rule) => rule.label)
  if (missing.length) {
    ruleState.value = []
    bot([`还差一点：${missing.join("；")}。请重新发送新密码。`], "err")
    mood.value = "oops"
    setStep("new-password")
    return
  }
  ruleState.value = rules.value.map(() => true)
  draftPassword = value
  me(SECRET_DOTS, { secret: true })
  markRead()
  bot(["请再发送一次，确认新密码。"])
  mood.value = "shy"
  setStep("confirm-password")
}

async function submitConfirm(): Promise<void> {
  const value = password.value
  password.value = ""
  if (!value) return
  me(SECRET_DOTS, { secret: true })
  const draft = draftPassword
  draftPassword = ""
  if (value !== draft) {
    markRead()
    ruleState.value = []
    bot(["两次输入的密码不一致，请重新发送新密码。"], "err")
    mood.value = "oops"
    setStep("new-password")
    return
  }
  const change = pendingChange
  if (!change || change.expiresAt <= Date.now()) {
    expireInitialChange()
    return
  }
  setStep("changing")
  mood.value = "think"
  typing.value = true
  try {
    await initialPasswordChangeRequest(change.token, draft)
    typing.value = false
    clearInitialChange()
    netWarn.value = false
    markRead()
    bot(["密码已更新。请用新密码重新登录。"], "ok")
    mood.value = "shy"
    setStep("password")
  } catch (error) {
    typing.value = false
    if (error instanceof AuthApiError && error.status === 401) {
      clearInitialChange()
      markRead()
      bot([errorText(error, "改密会话已失效，请重新登录")], "err")
      mood.value = "oops"
      setStep("password")
      return
    }
    ruleState.value = []
    if (error instanceof AuthApiError && error.status >= 500) {
      markUndelivered("密码修改未提交，请稍后使用当前改密会话重新发送新密码。")
      mood.value = "lost"
    } else {
      markRead()
      bot([errorText(error, "密码修改失败，请重新登录")], "err")
      mood.value = "oops"
    }
    setStep("new-password")
  }
}

watch(step, (next) => {
  if (next !== "new-password") return
  ruleState.value = rules.value.map((rule) => rule.test(password.value))
})

onMounted(() => {
  void openConversation()
})

onBeforeUnmount(() => {
  disposed = true
  clearInitialChange()
  password.value = ""
})
</script>

<template>
  <main class="login-screen" :data-mood="mood" :class="{ 'is-net-warn': netWarn }">
    <svg class="login-defs" width="0" height="0" aria-hidden="true" focusable="false">
      <defs>
        <symbol :id="EGRET_SYMBOL_ID" :viewBox="EGRET_VIEWBOX">
          <path :d="EGRET_PATHS.wing" fill="var(--egret-wing)" />
          <path :d="EGRET_PATHS.line" fill="var(--egret-line)" />
          <path :d="EGRET_PATHS.gold" fill="var(--egret-gold)" />
        </symbol>
      </defs>
    </svg>
    <h1 class="sr-only">企业短信管理平台登录</h1>
    <i class="login-sky" aria-hidden="true"></i>

    <LoginHero :mood="mood" :say-lines="sayLines" :pose-class="poseClass" />

    <section class="login-chat" aria-label="登录会话">
      <LoginChatHead
        :typing="typing"
        :status-text="statusText"
        :progress="progress"
        :restart-disabled="step === 'verifying' || step === 'changing' || step === 'done'"
        @restart="openConversation"
      />

      <div
        ref="threadRef"
        class="login-thread"
        role="log"
        aria-live="polite"
        aria-label="登录对话"
        :data-edit="editable ? 'on' : 'off'"
      >
        <div class="login-intro" aria-hidden="true">
          <span class="login-intro-disc">
            <span class="login-flip"><LoginEgret /></span>
          </span>
          <b class="login-intro-name">青鸾</b>
          <small>企业短信管理平台 · 仅限授权人员访问</small>
        </div>
        <p class="login-divider">今天 {{ openedAt }}</p>

        <LoginThreadItem
          v-for="(item, index) in items"
          :key="item.id"
          :item="item"
          :ghost="!leadsGroup(index)"
          :editable="editable"
          :rules="rules"
          :rule-state="ruleState"
          :policy-description="policy.description"
          @edit="editFrom(index)"
          @chip="runChip"
        />

        <div v-if="typing" class="login-row" data-testid="login-typing">
          <span :class="['login-mini', { 'is-ghost': !leadsGroup(items.length) }]"><LoginEgret /></span>
          <div class="login-bubble login-typing" aria-label="青鸾正在输入"><i></i><i></i><i></i></div>
        </div>
      </div>

      <p v-if="capsLock && secretStep" class="login-caps" role="status">大写锁定已开启</p>
      <form class="login-composer" autocomplete="on" @submit.prevent="submit">
        <label class="login-field">
          <span id="login-field-label" class="sr-only">{{ fieldLabel }}</span>
          <span
            :class="['login-field-tag', { 'is-secret': secretTag }]"
            aria-hidden="true"
            data-testid="login-field-tag"
          >
            {{ fieldTag }}
          </span>
          <input
            ref="usernameInput"
            v-model="username"
            data-testid="login-username"
            name="username"
            autocomplete="username"
            autocapitalize="off"
            spellcheck="false"
            aria-labelledby="login-field-label"
            :class="{ 'is-parked': secretStep }"
            :tabindex="secretStep ? -1 : 0"
            :placeholder="fieldLabel"
            :disabled="composerOff"
            @input="onAccountInput"
          />
          <input
            ref="passwordInput"
            v-model="password"
            data-testid="login-password"
            name="password"
            type="password"
            aria-labelledby="login-field-label"
            :autocomplete="step === 'new-password' || step === 'confirm-password' ? 'new-password' : 'current-password'"
            :class="{ 'is-parked': !secretStep }"
            :tabindex="secretStep ? 0 : -1"
            :placeholder="fieldLabel"
            :disabled="composerOff"
            @input="onPasswordInput"
            @keydown="onPasswordKey"
            @keyup="onPasswordKey"
          />
          <button class="login-send" data-testid="login-send" type="submit" aria-label="发送" :disabled="composerOff">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 19V5M6 11l6-6 6 6" /></svg>
          </button>
        </label>
      </form>
    </section>
  </main>
</template>
