<script setup lang="ts">
import { computed, ref } from "vue"

import type { ApprovalAction, ApprovalListItem, ApprovalStatus } from "../api/approvals"
import CategoryTag from "./CategoryTag.vue"
import EmptyState from "./EmptyState.vue"
import StatusTag from "./StatusTag.vue"
import { CATEGORY_LABELS } from "../lib/labels"
import { formatDateTime } from "../lib/time"

const REASON_MAX_LENGTH = 256
const URGENT_THRESHOLD_MS = 2 * 3600_000
const SOON_THRESHOLD_MS = 6 * 3600_000

const props = defineProps<{
  status: ApprovalStatus
  items: ApprovalListItem[]
  now: number
  loading: boolean
  decidingId: number | null
  currentUsername: string
}>()

const emit = defineEmits<{
  detail: [item: ApprovalListItem]
  quick: [item: ApprovalListItem, action: ApprovalAction, reason?: string]
}>()

const emptyTitle = computed(() =>
  props.status === "pending" ? "当前没有待审批记录" : "当前分类没有审批记录",
)

function isMine(item: ApprovalListItem): boolean {
  return item.applicant === props.currentUsername
}

function categoryLabel(category: ApprovalListItem["category"]): string {
  return CATEGORY_LABELS[category]
}

function triggerRule(item: ApprovalListItem): string {
  if (item.trigger_threshold_source === "legacy_unknown" || item.trigger_threshold === null) {
    return "历史阈值不可确认"
  }
  const base = `${categoryLabel(item.category)} ≥ ${item.trigger_threshold} 个号码`
  return item.trigger_threshold_source === "snapshot" ? `${base} · 提交时阈值快照` : base
}

function formatSegments(value: number | null): string {
  return value === null ? "—" : `${value.toLocaleString()} 条`
}

function scheduleChip(item: ApprovalListItem): string {
  return item.scheduled_at ? `定时 ${formatDateTime(item.scheduled_at)}` : "立即发送"
}

interface Countdown {
  text: string
  caption: string
  level: "normal" | "soon" | "urgent" | "expired" | "unknown"
}

/** 由 expires_at 与父级 1s tick 计算剩余有效期；服务端到期会关闭单据，过期态仅作防御展示。 */
function countdownOf(item: ApprovalListItem): Countdown {
  if (!item.expires_at) return { text: "—", caption: "有效期暂不可用", level: "unknown" }
  const remaining = new Date(item.expires_at).getTime() - props.now
  if (Number.isNaN(remaining)) return { text: "—", caption: "有效期暂不可用", level: "unknown" }
  if (remaining <= 0) return { text: "已临期截止", caption: "等待服务端过期关闭", level: "expired" }
  const totalSeconds = Math.floor(remaining / 1000)
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  const text = [hours, minutes, seconds]
    .map((unit) => String(unit).padStart(2, "0"))
    .join(":")
  const level =
    remaining < URGENT_THRESHOLD_MS ? "urgent" : remaining < SOON_THRESHOLD_MS ? "soon" : "normal"
  return { text, caption: "后过期", level }
}

function rowClass(item: ApprovalListItem): Record<string, boolean> {
  const level = countdownOf(item).level
  return {
    "is-urgent": level === "urgent" || level === "expired",
    "is-soon": level === "soon",
    "is-mine": isMine(item),
  }
}

function deciderLabel(item: ApprovalListItem): string {
  if (item.approver) return item.approver
  if (item.status === "expired") return "系统自动"
  return "—"
}

interface Destination {
  title: string
  note: string | null
}

/** 决策后批次真实去向，依据列表项回带的 batch_status / deferred_reason。 */
function destinationOf(item: ApprovalListItem): Destination {
  if (item.status === "approved") {
    if (item.batch_status === "scheduled") {
      const note = item.scheduled_at ? `定时 ${formatDateTime(item.scheduled_at)}` : null
      return item.deferred_reason === "market_window"
        ? { title: "窗外改派为定时", note }
        : { title: "已进入定时", note }
    }
    if (item.batch_status === "queued") {
      return { title: "已进入发送队列", note: `${item.category === "market" ? "bulk" : "realtime"} 通道` }
    }
    return { title: "已进入发送流程", note: null }
  }
  if (item.status === "rejected") {
    return { title: "配额已释放", note: item.reason ? `原因：${item.reason}` : null }
  }
  return { title: "超时未决作废", note: "配额已释放" }
}

/* 行内快捷决策：全列表同时只保留一个 Popover，意见随目标切换重置。
   Popover 绑定 :visible 且不透传 update 处理器 → Element 视为受控组件，触发器事件全部惰性，
   开合完全由引用按钮的 click 驱动（避免受控模式下 jsdom 触发竞态）。 */
const quickTarget = ref<{ id: number; action: ApprovalAction } | null>(null)
const quickReason = ref("")

function quickVisible(item: ApprovalListItem, action: ApprovalAction): boolean {
  return quickTarget.value?.id === item.id && quickTarget.value.action === action
}

function toggleQuick(item: ApprovalListItem, action: ApprovalAction): void {
  if (quickVisible(item, action)) {
    closeQuick()
    return
  }
  quickTarget.value = { id: item.id, action }
  quickReason.value = ""
}

function closeQuick(): void {
  quickTarget.value = null
  quickReason.value = ""
}

const quickConfirmDisabled = computed(() => {
  if (props.decidingId !== null || quickTarget.value === null) return true
  const reason = quickReason.value.trim()
  if (quickTarget.value.action === "reject" && !reason) return true
  return reason.length > REASON_MAX_LENGTH
})

function confirmQuick(item: ApprovalListItem): void {
  if (quickTarget.value === null || quickConfirmDisabled.value) return
  const action = quickTarget.value.action
  const reason = quickReason.value.trim() || undefined
  closeQuick()
  emit("quick", item, action, reason)
}
</script>

<template>
  <section class="approval-rows" :class="{ 'is-table': status !== 'pending' }">
    <ul v-if="status === 'pending'" class="approval-queue-list">
      <li
        v-for="item in items"
        :key="item.id"
        class="approval-row"
        :class="rowClass(item)"
        :data-testid="`approval-row-${item.id}`"
      >
        <div class="approval-cd">
          <b>{{ countdownOf(item).text }}</b>
          <span>{{ countdownOf(item).caption }}</span>
        </div>
        <div class="approval-row-main">
          <div class="approval-row-title">
            <CategoryTag :category="item.category" />
            <code class="approval-batch-no">{{ item.batch_no }}</code>
            <span class="approval-sched-chip">{{ scheduleChip(item) }}</span>
          </div>
          <p class="approval-row-facts">
            {{ item.applicant }} · {{ item.dept }}
            <em>·</em>{{ formatDateTime(item.created_at) }} 提交
            <em>·</em>受众 <b>{{ item.total.toLocaleString() }}</b> 号码
            <em>·</em>预计计费 <b>{{ formatSegments(item.estimated_segments) }}</b>
          </p>
          <p class="approval-row-rule">触发规则 {{ triggerRule(item) }}</p>
        </div>
        <span
          v-if="isMine(item)"
          class="approval-avoid-note"
          :data-testid="`approval-avoid-${item.id}`"
        >本人提交 · 按规则回避</span>
        <div v-else class="approval-row-actions" :data-testid="`approval-actions-${item.id}`">
          <button
            type="button"
            class="approval-expand"
            :data-testid="`approval-detail-${item.id}`"
            :aria-label="`查看批次 ${item.batch_no} 的审批详情`"
            @click="emit('detail', item)"
          >详情 ›</button>
          <el-popover
            :visible="quickVisible(item, 'approve')"
            placement="bottom-end"
            :width="288"
            trigger="click"
            :teleported="false"
          >
            <template #reference>
              <el-button
                type="primary"
                size="small"
                :disabled="decidingId !== null"
                :aria-expanded="quickVisible(item, 'approve')"
                :data-testid="`approval-quick-approve-${item.id}`"
                @click="toggleQuick(item, 'approve')"
              >通过</el-button>
            </template>
            <div class="approval-quick">
              <p class="approval-quick-title">快捷通过 · {{ item.batch_no }}</p>
              <el-input
                v-model="quickReason"
                type="textarea"
                :rows="3"
                maxlength="256"
                show-word-limit
                placeholder="审批意见（选填，≤256 字）"
                data-testid="approval-quick-reason-approve"
              />
              <p class="approval-quick-tip">决策写审计 · 冲突时自动刷新列表</p>
              <div class="approval-quick-actions">
                <el-button size="small" @click="closeQuick">取消</el-button>
                <el-button
                  type="primary"
                  size="small"
                  :disabled="quickConfirmDisabled"
                  data-testid="approval-quick-confirm-approve"
                  @click="confirmQuick(item)"
                >确认通过</el-button>
              </div>
            </div>
          </el-popover>
          <el-popover
            :visible="quickVisible(item, 'reject')"
            placement="bottom-end"
            :width="288"
            trigger="click"
            :teleported="false"
          >
            <template #reference>
              <el-button
                type="danger"
                size="small"
                :disabled="decidingId !== null"
                :aria-expanded="quickVisible(item, 'reject')"
                :data-testid="`approval-quick-reject-${item.id}`"
                @click="toggleQuick(item, 'reject')"
              >驳回</el-button>
            </template>
            <div class="approval-quick">
              <p class="approval-quick-title">快捷驳回 · {{ item.batch_no }}</p>
              <el-input
                v-model="quickReason"
                type="textarea"
                :rows="3"
                maxlength="256"
                show-word-limit
                placeholder="驳回原因（必填，≤256 字）"
                data-testid="approval-quick-reason-reject"
              />
              <p class="approval-quick-tip">驳回原因必填 · 配额由服务端幂等回补</p>
              <div class="approval-quick-actions">
                <el-button size="small" @click="closeQuick">取消</el-button>
                <el-button
                  type="danger"
                  size="small"
                  :disabled="quickConfirmDisabled"
                  data-testid="approval-quick-confirm-reject"
                  @click="confirmQuick(item)"
                >确认驳回</el-button>
              </div>
            </div>
          </el-popover>
        </div>
      </li>
    </ul>

    <table v-else class="approval-table">
      <thead>
        <tr>
          <th>批次号 / 申请时间</th>
          <th>类别</th>
          <th>申请人</th>
          <th>部门</th>
          <th>受众</th>
          <th>状态</th>
          <th>审批人 / 决策时间</th>
          <th>去向</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in items"
          :key="item.id"
          :data-testid="`approval-row-${item.id}`"
          tabindex="0"
          @click="emit('detail', item)"
          @keydown.enter="emit('detail', item)"
        >
          <td data-label="批次号 / 申请时间">
            <span class="approval-batch-cell">
              <code>{{ item.batch_no }}</code>
              <small>{{ formatDateTime(item.created_at) }}</small>
            </span>
          </td>
          <td data-label="类别"><CategoryTag :category="item.category" /></td>
          <td data-label="申请人">{{ item.applicant }}</td>
          <td data-label="部门">{{ item.dept }}</td>
          <td data-label="受众">{{ item.total.toLocaleString() }}</td>
          <td data-label="状态"><StatusTag :status="item.status" /></td>
          <td data-label="审批人 / 决策时间">
            <span class="approval-decider">
              <b>{{ deciderLabel(item) }}</b>
              <small>{{ item.decided_at ? formatDateTime(item.decided_at) : "—" }}</small>
            </span>
          </td>
          <td data-label="去向" class="approval-dest">
            {{ destinationOf(item).title }}
            <small v-if="destinationOf(item).note" :title="destinationOf(item).note ?? undefined">{{ destinationOf(item).note }}</small>
          </td>
          <td class="approval-open-cell">
            <button
              type="button"
              class="approval-expand"
              :data-testid="`approval-detail-${item.id}`"
              :aria-label="`查看批次 ${item.batch_no} 的审批详情`"
              @click.stop="emit('detail', item)"
            >查看</button>
          </td>
        </tr>
      </tbody>
    </table>

    <EmptyState
      v-if="!loading && !items.length"
      :title="emptyTitle"
      description="新的审批申请会出现在这里。"
    />
  </section>
</template>
