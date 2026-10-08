<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { useRouter } from "vue-router"

import { AuthApiError, initialPasswordChangeRequest, passwordPolicyRequest, type PasswordPolicy } from "../api/auth"
import { ACCESS_ONLY_SESSION_MESSAGE, isAccessOnlySessionMode } from "../api/refreshLock"
import { EGRET_PATHS, EGRET_SYMBOL_ID, EGRET_VIEWBOX } from "../assets/brand/egretPaths"
import LoginEgret from "../components/LoginEgret.vue"
import { useLatestRead } from "../composables/useLatestRead"
import { errorText } from "../lib/error"
import { recallLoginProvider, rememberLoginProvider } from "../lib/loginProvider"
import { formatHm } from "../lib/time"
import { useSessionStore } from "../stores/session"

type ProviderCode = "local" | "ad"
type Step =
  | "loading"
  | "blocked"
  | "choose"
  | "account"
  | "password"
  | "verifying"
  | "new-password"
  | "confirm-password"
  | "changing"
  | "done"
type Mood = "idle" | "back" | "listen" | "shy" | "think" | "oops" | "rest" | "lost" | "fly"
type Part = string | { strong: string }
type ChipAction = { kind: "provider"; code: ProviderCode } | { kind: "forgot" } | { kind: "retry" }
interface Chip {
  label: string
  action: ChipAction
  off?: boolean
  quiet?: boolean
}
type Item =
  | { id: number; type: "bot"; parts: Part[]; tone?: "err" | "ok" }
  | { id: number; type: "rules" }
  | {
      id: number
      type: "me"
      text: string
      secret: boolean
      edit?: "provider" | "account"
      receipt: string
      failed: boolean
    }
  | { id: number; type: "sys"; text: string; tone?: "warn"; testid?: string }
  | { id: number; type: "chips"; chips: Chip[]; used: boolean }
type DistributiveOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never
type NewItem = DistributiveOmit<Item, "id">

/** 本期固定目录：始终画出 local / AD，服务端未返回的标未开通。 */
const PROVIDER_CATALOG: Record<ProviderCode, { name: string; ask: string; offHint: string }> = {
  local: { name: "本地账号", ask: "账号", offHint: "本地账号尚未开通，暂时只能用 AD 账号登录。" },
  ad: { name: "AD 账号", ask: "企业 AD 账号", offHint: "企业目录尚未开通，暂时只能用本地账号登录。" },
}
const PROVIDER_ORDER: ProviderCode[] = ["local", "ad"]
/** 密码气泡固定圆点数，不暴露真实长度。 */
const SECRET_DOTS = "••••••••"
const SUCCESS_PAUSE_MS = 560
const SAY: Record<Mood, string[]> = {
  idle: ["有新消息要发？", "先登录吧"],
  back: ["欢迎回来"],
  listen: ["我在听"],
  shy: ["我转过去了", "不看你的密码"],
  think: ["正在送信…"],
  oops: ["好像不太对", "再试一次？"],
  rest: ["先歇一会儿"],
  lost: ["信没送出去"],
  fly: ["出发！"],
}
const DEFAULT_POLICY: PasswordPolicy = {
  min_length: 12,
  max_length: 128,
  required_character_classes: 3,
  forbid_username: true,
  description: "12–128 位，至少包含大小写字母、数字、特殊字符中的三类，不能包含用户名；服务端检查常见或泄露密码",
}
/** 会话顶部进度条：「设置新密码」一段只在首次改密会话内出现。 */
const PROGRESS_LABELS = ["登录方式", "账号", "密码"]
const CHANGE_LABEL = "设置新密码"
const CHANGE_STEPS: Step[] = ["new-password", "confirm-password", "changing"]
const PROGRESS_INDEX: Record<Step, number> = {
  loading: 0,
  blocked: 0,
  choose: 0,
  account: 1,
  password: 2,
  verifying: 2,
  "new-password": 3,
  "confirm-password": 3,
  changing: 3,
  done: 3,
}
/** 输入框左侧标签：只说明当前要发送什么，不反映输入内容。 */
const FIELD_TAG: Record<Step, string> = {
  loading: "连接中",
  blocked: "等待重试",
  choose: "等待选择",
  account: "账号",
  password: "密码",
  verifying: "密码",
  "new-password": "新密码",
  "confirm-password": "确认新密码",
  changing: "新密码",
  done: "已验证",
}
const SECRET_TAG_STEPS: Step[] = ["password", "verifying", "new-password", "confirm-password", "changing"]
const CLASS_PATTERNS = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/]
const CN_DIGITS = ["零", "一", "二", "三", "四"]
const INPUT_STEPS: Step[] = ["account", "password", "new-password", "confirm-password"]
const EDITABLE_STEPS: Step[] = ["choose", "account", "password"]
const PLACEHOLDER: Record<Step, string> = {
  loading: "正在连接…",
  blocked: "等待重试",
  choose: "请先在上方选择登录方式",
  account: "发送账号",
  password: "发送密码",
  verifying: "正在验证…",
  "new-password": "发送新密码",
  "confirm-password": "再次发送新密码",
  changing: "正在提交…",
  done: "正在进入工作台…",
}

/** 待命台词首句随浏览器本地时段变化，只读本地时钟，不涉及会话数据。 */
function greetingFor(hour: number): string {
  if (hour >= 6 && hour < 11) return "早，有新消息要发？"
  if (hour >= 18 && hour < 22) return "晚上好，还有消息要发？"
  if (hour >= 22 || hour < 6) return "夜深了，还有消息要发？"
  return SAY.idle[0]
}

const router = useRouter()
const session = useSessionStore()
const accessOnlySession = isAccessOnlySessionMode()
const policyRead = useLatestRead()

const items = ref<Item[]>([])
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
const openedOn = new Date()
const openedAt = formatHm(openedOn)
const idleLines = [greetingFor(openedOn.getHours()), ...SAY.idle.slice(1)]
/** 账号输入时白鹭点头：1/2 交替以重启动画；密码步不点头，不随按键给出任何可见反馈。 */
const nod = ref<0 | 1 | 2>(0)
const threadRef = ref<HTMLElement | null>(null)
const usernameInput = ref<HTMLInputElement | null>(null)
const passwordInput = ref<HTMLInputElement | null>(null)

let seq = 0
let returnProvider: ProviderCode = "local"
let helpOffered = false
/** 首次改密短令牌与两步确认间的新密码：仅存本组件易失内存，完成、失效或卸载即清空。 */
let pendingChange: { token: string; expiresAt: number } | null = null
let draftPassword = ""
let changeTimer = 0
let disposed = false

const secretStep = computed(() => step.value !== "account")
const composerOff = computed(() => !INPUT_STEPS.includes(step.value))
const editable = computed(() => EDITABLE_STEPS.includes(step.value))
const fieldLabel = computed(() =>
  step.value === "account" && providerCode.value
    ? `发送${PROVIDER_CATALOG[providerCode.value].ask}`
    : PLACEHOLDER[step.value],
)
const statusText = computed(() => (typing.value ? "正在输入…" : netWarn.value ? "连接不稳定" : "在线"))
const sayLines = computed(() => (mood.value === "idle" ? idleLines : SAY[mood.value]))
const poseClass = computed(() => (mood.value === "listen" && nod.value ? `is-nod-${nod.value}` : ""))
const progress = computed(() => {
  const labels = CHANGE_STEPS.includes(step.value) ? [...PROGRESS_LABELS, CHANGE_LABEL] : PROGRESS_LABELS
  const at = step.value === "done" ? labels.length : PROGRESS_INDEX[step.value]
  return labels.map((label, index) => ({
    label,
    state: index < at ? "done" : index === at ? "current" : "todo",
  }))
})
const fieldTag = computed(() =>
  step.value === "account" && providerCode.value ? PROVIDER_CATALOG[providerCode.value].ask : FIELD_TAG[step.value],
)
const secretTag = computed(() => SECRET_TAG_STEPS.includes(step.value))
const rules = computed(() => {
  const { min_length: min, max_length: max, required_character_classes: classes, forbid_username } = policy.value
  const list = [
    { label: `${min}–${max} 位`, test: (value: string) => value.length >= min && value.length <= max },
    {
      label:
        classes >= CLASS_PATTERNS.length
          ? "大写、小写、数字、符号四类都要有"
          : `大写、小写、数字、符号至少${CN_DIGITS[classes] ?? classes}类`,
      test: (value: string) => CLASS_PATTERNS.filter((pattern) => pattern.test(value)).length >= classes,
    },
  ]
  if (forbid_username) {
    list.push({
      label: "不包含账号",
      test: (value: string) => value.length > 0 && !value.toLowerCase().includes(account.value.toLowerCase()),
    })
  }
  return list
})

function providerName(code: ProviderCode): string {
  return session.providers.find((provider) => provider.code === code)?.name ?? PROVIDER_CATALOG[code].name
}

function isEnabled(code: ProviderCode): boolean {
  return session.providers.some((provider) => provider.code === code)
}

function leadsGroup(index: number): boolean {
  const previous = items.value[index - 1]
  return !previous || (previous.type !== "bot" && previous.type !== "rules")
}

function scrollToEnd(): void {
  void nextTick(() => {
    const thread = threadRef.value
    if (thread) thread.scrollTop = thread.scrollHeight
  })
}

function push(item: NewItem): Item {
  items.value.push({ ...item, id: ++seq } as Item)
  scrollToEnd()
  return items.value[items.value.length - 1]
}

function bot(parts: Part[], tone?: "err" | "ok"): void {
  push({ type: "bot", parts, tone })
}

function sys(text: string, tone?: "warn", testid?: string): void {
  push({ type: "sys", text, tone, testid })
}

function chips(list: Chip[]): void {
  push({ type: "chips", chips: list, used: false })
}

function lastMine(): Extract<Item, { type: "me" }> | undefined {
  for (let index = items.value.length - 1; index >= 0; index -= 1) {
    const item = items.value[index]
    if (item.type === "me") return item
  }
  return undefined
}

function me(text: string, options: { secret?: boolean; edit?: "provider" | "account" } = {}): void {
  for (const item of items.value) if (item.type === "me") item.receipt = ""
  push({
    type: "me",
    text,
    secret: options.secret ?? false,
    edit: options.edit,
    receipt: options.secret ? "已加密发送" : "已送达",
    failed: false,
  })
}

function markRead(): void {
  const mine = lastMine()
  if (mine?.receipt && !mine.failed) mine.receipt = "已读"
}

function markUndelivered(message: string): void {
  const mine = lastMine()
  if (mine) {
    mine.failed = true
    mine.receipt = "发送失败"
  }
  netWarn.value = true
  sys(message, "warn")
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

function runChip(item: Extract<Item, { type: "chips" }>, chip: Chip): void {
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
function chooseProvider(code: ProviderCode, item: Extract<Item, { type: "chips" }>): void {
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

    <section class="login-hero" aria-hidden="true">
      <div class="login-stage">
        <div class="login-say">
          <span v-for="(line, index) in sayLines" :key="`${mood}-${index}`">{{ line }}</span>
        </div>
        <div class="login-bird">
          <div :class="['login-pose', poseClass]">
            <span class="login-flip"><LoginEgret /></span>
            <svg class="login-letter" viewBox="0 0 40 28" focusable="false">
              <rect x="1" y="1" width="38" height="26" rx="3" />
              <path d="M2 3l18 13L38 3" />
              <circle cx="20" cy="16" r="3.4" />
            </svg>
          </div>
        </div>
        <svg class="login-letter is-dropped" viewBox="0 0 40 28" focusable="false">
          <rect x="1" y="1" width="38" height="26" rx="3" />
          <path d="M2 3l18 13L38 3" />
          <circle cx="20" cy="16" r="3.4" />
        </svg>
        <i class="login-floor"></i>
      </div>
      <p class="login-legal">仅限授权人员访问 · 连续失败将临时锁定</p>
    </section>

    <section class="login-chat" aria-label="登录会话">
      <header class="login-chat-head">
        <span class="login-avatar">
          <span class="login-flip"><LoginEgret /></span>
        </span>
        <span class="login-who">
          <b>青鸾</b>
          <small :class="{ 'is-typing': typing }" data-testid="login-status">{{ statusText }}</small>
        </span>
        <button
          class="login-restart"
          type="button"
          aria-label="重新开始"
          title="重新开始"
          :disabled="step === 'verifying' || step === 'changing' || step === 'done'"
          @click="openConversation"
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M3 12a9 9 0 1 0 3-6.7L3 8" />
            <path d="M3 3v5h5" />
          </svg>
        </button>
      </header>

      <ol class="login-steps" aria-label="登录进度" data-testid="login-progress">
        <li
          v-for="item in progress"
          :key="item.label"
          :class="`is-${item.state}`"
          :aria-current="item.state === 'current' ? 'step' : undefined"
        >
          {{ item.label }}
        </li>
      </ol>

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

        <template v-for="(item, index) in items" :key="item.id">
          <p
            v-if="item.type === 'sys'"
            :class="['login-sys', { 'is-warn': item.tone === 'warn' }]"
            :data-testid="item.testid"
            :role="item.tone === 'warn' ? 'alert' : 'status'"
          >
            {{ item.text }}
          </p>

          <div v-else-if="item.type === 'bot' || item.type === 'rules'" class="login-row">
            <span :class="['login-mini', { 'is-ghost': !leadsGroup(index) }]"><LoginEgret /></span>
            <div
              v-if="item.type === 'bot'"
              :class="['login-bubble', item.tone ? `is-${item.tone}` : '']"
              :role="item.tone === 'err' ? 'alert' : undefined"
            >
              <template v-for="(part, partIndex) in item.parts" :key="partIndex">
                <b v-if="typeof part === 'object'">{{ part.strong }}</b>
                <template v-else>{{ part }}</template>
              </template>
            </div>
            <div v-else class="login-bubble login-rules" data-testid="login-password-rules">
              新密码需要满足：
              <ul>
                <li v-for="(rule, ruleIndex) in rules" :key="rule.label" :class="{ 'is-ok': ruleState[ruleIndex] }">
                  <i aria-hidden="true">
                    <svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10" /></svg>
                  </i>
                  {{ rule.label }}
                </li>
              </ul>
              <small>密码要求：{{ policy.description }}</small>
            </div>
          </div>

          <template v-else-if="item.type === 'me'">
            <div :class="['login-row', 'is-me', { 'is-failed': item.failed }]">
              <span v-if="item.failed" class="login-bang" aria-hidden="true">!</span>
              <button
                v-if="item.edit"
                type="button"
                class="login-bubble is-editable"
                :title="editable ? '点按修改' : undefined"
                :aria-label="`${item.text}，点按修改`"
                :disabled="!editable"
                @click="editFrom(index)"
              >
                {{ item.text }}
                <svg class="login-pen" viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M4 20h4L19 9l-4-4L4 16v4Z" />
                </svg>
              </button>
              <div v-else :class="['login-bubble', { 'is-secret': item.secret }]">
                <svg v-if="item.secret" class="login-lock" viewBox="0 0 24 24" aria-hidden="true">
                  <rect x="5" y="11" width="14" height="10" rx="2" />
                  <path d="M8 11V8a4 4 0 0 1 8 0v3" />
                </svg>
                <span v-if="item.secret" class="sr-only">密码已隐藏</span>
                <span :aria-hidden="item.secret ? 'true' : undefined">{{ item.text }}</span>
              </div>
            </div>
            <p v-if="item.receipt" :class="['login-receipt', { 'is-failed': item.failed }]">{{ item.receipt }}</p>
          </template>

          <div v-else-if="item.type === 'chips'" :class="['login-chips', { 'is-used': item.used }]">
            <button
              v-for="chip in item.chips"
              :key="chip.label"
              type="button"
              :class="['login-chip', { 'is-off': chip.off, 'is-quiet': chip.quiet }]"
              :data-testid="
                chip.action.kind === 'provider' ? `provider-${chip.action.code}` : `login-chip-${chip.action.kind}`
              "
              :aria-disabled="chip.off || item.used ? 'true' : 'false'"
              :aria-label="chip.off ? `${chip.label}，未开通` : chip.label"
              @click="runChip(item, chip)"
            >
              {{ chip.label }}<small v-if="chip.off">未开通</small>
            </button>
          </div>
        </template>

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
