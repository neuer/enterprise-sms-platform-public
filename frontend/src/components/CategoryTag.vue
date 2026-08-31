<script setup lang="ts">
import { CATEGORY_LABELS } from "../lib/labels"

defineProps<{ category: "verify" | "notice" | "market" }>()
</script>

<template>
  <el-tag effect="plain" :class="['category-tag', `category-tag--${category}`]">
    {{ CATEGORY_LABELS[category] }}
  </el-tag>
</template>
