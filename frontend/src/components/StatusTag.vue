<script setup lang="ts">
import { computed } from "vue"

type TagType = "success" | "warning" | "info" | "danger"
type TagEffect = "plain" | "dark"

const props = defineProps<{ status: string; label?: string }>()

const labels: Record<string, string> = {
  pending: "待处理",
  pending_approval: "待审批",
  scheduled: "已排期",
  queued: "排队中",
  sending: "发送中",
  submitted: "已提交",
  completed: "已完成",
  completed_unknown: "完成(含未知)",
  delivered: "已送达",
  approved: "已通过",
  failed: "失败",
  rejected: "已驳回",
  cancelled: "已取消",
  expired: "已过期",
  uncertain: "结果未知",
  unknown_terminal: "未知终态",
  balance_blocked: "余额阻断",
  unknown: "未知",
  dead: "终止重试",
}

const infoStates = new Set(["queued", "sending"])
const dangerStates = new Set(["failed", "rejected", "cancelled", "expired", "dead", "unknown"])
const warningStates = new Set(["pending", "pending_approval", "scheduled"])
const successStates = new Set(["completed", "delivered", "approved"])
const interventionStates = new Set([
  "uncertain",
  "unknown_terminal",
  "completed_unknown",
  "balance_blocked",
])

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
  <el-tag
    :type="presentation.type"
    :effect="presentation.effect"
    :class="['status-tag', `status-tag--${status}`]"
  >
    {{ label || labels[status] || status }}
  </el-tag>
</template>
