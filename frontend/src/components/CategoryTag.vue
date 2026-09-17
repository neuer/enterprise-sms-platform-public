<script setup lang="ts">
import { CATEGORY_LABELS, type MessageCategory } from "../lib/labels"

defineProps<{ category: MessageCategory }>()
</script>

<template>
  <el-tag effect="plain" :class="['category-tag', `category-tag--${category}`]">
    {{ CATEGORY_LABELS[category] }}
  </el-tag>
</template>
