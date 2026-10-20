<script setup lang="ts">
import type { Chip, ChipsItem, Item, PasswordRule } from "../lib/loginConversation"
import LoginEgret from "./LoginEgret.vue"

/** 登录会话单条消息：纯展示，回改与快捷回复只发出意图；密码气泡只渲染固定圆点。 */
defineProps<{
  item: Item
  /** 与上一条同属青鸾连续发言时隐去头像。 */
  ghost: boolean
  editable: boolean
  rules: PasswordRule[]
  ruleState: boolean[]
  policyDescription: string
}>()
const emit = defineEmits<{ edit: []; chip: [item: ChipsItem, chip: Chip] }>()
</script>

<template>
  <p
    v-if="item.type === 'sys'"
    :class="['login-sys', { 'is-warn': item.tone === 'warn' }]"
    :data-testid="item.testid"
    :role="item.tone === 'warn' ? 'alert' : 'status'"
  >
    {{ item.text }}
  </p>

  <div v-else-if="item.type === 'bot' || item.type === 'rules'" class="login-row">
    <span :class="['login-mini', { 'is-ghost': ghost }]"><LoginEgret /></span>
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
      <small>密码要求：{{ policyDescription }}</small>
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
        @click="emit('edit')"
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
      :data-testid="chip.action.kind === 'provider' ? `provider-${chip.action.code}` : `login-chip-${chip.action.kind}`"
      :aria-disabled="chip.off || item.used ? 'true' : 'false'"
      :aria-label="chip.off ? `${chip.label}，未开通` : chip.label"
      @click="emit('chip', item, chip)"
    >
      {{ chip.label }}<small v-if="chip.off">未开通</small>
    </button>
  </div>
</template>
