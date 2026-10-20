<script setup lang="ts">
import type { Mood } from "../lib/loginConversation"
import LoginEgret from "./LoginEgret.vue"

/** 登录页左侧白鹭区：纯展示，情绪与台词由页面给出。 */
defineProps<{
  mood: Mood
  sayLines: string[]
  poseClass: string
}>()
</script>

<template>
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
</template>
