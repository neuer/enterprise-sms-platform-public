import { computed, type Ref } from "vue"

import {
  CHANGE_LABEL,
  CHANGE_STEPS,
  EDITABLE_STEPS,
  FIELD_TAG,
  INPUT_STEPS,
  PLACEHOLDER,
  PROGRESS_INDEX,
  PROGRESS_LABELS,
  SAY,
  SECRET_TAG_STEPS,
  greetingFor,
  type Mood,
  type ProgressStep,
  type ProviderCode,
  type Step,
} from "../lib/loginConversation"
import { formatHm } from "../lib/time"

interface LoginDisplaySource {
  step: Ref<Step>
  mood: Ref<Mood>
  typing: Ref<boolean>
  netWarn: Ref<boolean>
  providerCode: Ref<ProviderCode | "">
  nod: Ref<0 | 1 | 2>
}

/**
 * 登录会话的纯展示派生：只读当前步与情绪状态，不读取任何输入值。
 * askFor 给出认证源的提问用语（如「企业 AD 账号」），由页面目录提供。
 */
export function useLoginDisplay(source: LoginDisplaySource, askFor: (code: ProviderCode) => string) {
  const { step, mood, typing, netWarn, providerCode, nod } = source
  const openedOn = new Date()
  const openedAt = formatHm(openedOn)
  const idleLines = [greetingFor(openedOn.getHours()), ...SAY.idle.slice(1)]
  const secretStep = computed(() => step.value !== "account")
  const composerOff = computed(() => !INPUT_STEPS.includes(step.value))
  const editable = computed(() => EDITABLE_STEPS.includes(step.value))
  const fieldLabel = computed(() =>
    step.value === "account" && providerCode.value ? `发送${askFor(providerCode.value)}` : PLACEHOLDER[step.value],
  )
  const statusText = computed(() => (typing.value ? "正在输入…" : netWarn.value ? "连接不稳定" : "在线"))
  const sayLines = computed(() => (mood.value === "idle" ? idleLines : SAY[mood.value]))
  const poseClass = computed(() => (mood.value === "listen" && nod.value ? `is-nod-${nod.value}` : ""))
  const progress = computed<ProgressStep[]>(() => {
    const labels = CHANGE_STEPS.includes(step.value) ? [...PROGRESS_LABELS, CHANGE_LABEL] : PROGRESS_LABELS
    const at = step.value === "done" ? labels.length : PROGRESS_INDEX[step.value]
    return labels.map((label, index) => ({
      label,
      state: index < at ? "done" : index === at ? "current" : "todo",
    }))
  })
  const fieldTag = computed(() =>
    step.value === "account" && providerCode.value ? askFor(providerCode.value) : FIELD_TAG[step.value],
  )
  const secretTag = computed(() => SECRET_TAG_STEPS.includes(step.value))
  return {
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
  }
}
