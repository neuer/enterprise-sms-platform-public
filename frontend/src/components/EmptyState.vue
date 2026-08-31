<script setup lang="ts">
defineProps<{ title: string; description: string }>()
</script>

<template>
  <section class="empty-state" role="status">
    <strong>{{ title }}</strong>
    <p>{{ description }}</p>
  </section>
</template>
