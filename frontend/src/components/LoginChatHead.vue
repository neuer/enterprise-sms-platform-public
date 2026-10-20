<script setup lang="ts">
import type { ProgressStep } from "../lib/loginConversation"
import LoginEgret from "./LoginEgret.vue"

/** 登录会话窗头部与进度条：纯展示，「重新开始」只发出意图。 */
defineProps<{
  typing: boolean
  statusText: string
  progress: ProgressStep[]
  restartDisabled: boolean
}>()
const emit = defineEmits<{ restart: [] }>()
</script>

<template>
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
      :disabled="restartDisabled"
      @click="emit('restart')"
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
</template>
