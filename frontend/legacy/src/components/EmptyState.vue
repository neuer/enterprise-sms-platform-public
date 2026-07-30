<script setup lang="ts">
defineProps<{ title: string; description: string }>()
</script>

<template>
  <section class="empty-state" role="status">
    <span class="empty-state-mark" aria-hidden="true"></span>
    <strong>{{ title }}</strong>
    <p>{{ description }}</p>
  </section>
</template>
