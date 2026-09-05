<script setup lang="ts">
import { computed } from "vue"

import { STATUS_LABELS } from "../lib/labels"

type TagType = "success" | "warning" | "info" | "danger"
type TagEffect = "plain" | "dark"

const props = defineProps<{ status: string; label?: string }>()

const infoStates = new Set(["queued", "sending"])
const dangerStates = new Set(["failed", "rejected", "cancelled", "expired", "dead", "unknown"])
const warningStates = new Set(["pending", "pending_approval", "scheduled", "split_capacity_blocked"])
const successStates = new Set(["completed", "delivered", "approved"])
const interventionStates = new Set(["uncertain", "unknown_terminal", "completed_unknown", "balance_blocked"])

const presentation = computed<{ type: TagType; effect: TagEffect }>(() => {
  if (interventionStates.has(props.status)) return { type: "danger", effect: "dark" }
  if (dangerStates.has(props.status)) return { type: "danger", effect: "plain" }
  if (warningStates.has(props.status)) return { type: "warning", effect: "plain" }
  if (successStates.has(props.status)) return { type: "success", effect: "plain" }
  if (infoStates.has(props.status)) return { type: "info", effect: "plain" }
  return { type: "info", effect: "plain" }
})
</script>

<template>
  <el-tag :type="presentation.type" :effect="presentation.effect" :class="['status-tag', `status-tag--${status}`]">
    {{ label || STATUS_LABELS[status] || status }}
  </el-tag>
</template>
