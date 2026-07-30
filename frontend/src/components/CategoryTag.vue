<script setup lang="ts">
defineProps<{ category: "verify" | "notice" | "market" }>()

const labels = {
  verify: "验证码",
  notice: "通知",
  market: "营销",
} as const
</script>

<template>
  <el-tag effect="plain" :class="['category-tag', `category-tag--${category}`]">
    {{ labels[category] }}
  </el-tag>
</template>
