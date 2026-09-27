<script setup lang="ts">
import { EGRET_SYMBOL_ID, EGRET_VIEWBOX } from "../assets/brand/egretPaths"
</script>

<template>
  <svg class="login-egret" :viewBox="EGRET_VIEWBOX" aria-hidden="true" focusable="false">
    <use :href="`#${EGRET_SYMBOL_ID}`" />
  </svg>
</template>
