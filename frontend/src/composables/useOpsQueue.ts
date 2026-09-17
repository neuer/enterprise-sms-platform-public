import { ElMessage } from "element-plus"

import { computed, h, ref } from "vue"

import { getQueueStatus, resumeQueue, type QueueStatus } from "../api/ops"

import { confirmAuditedAction } from "../lib/confirm"

import { errorText } from "../lib/error"

import { useLatestRead } from "../composables/useLatestRead"

export function useOpsQueue() {
  const queue = ref<QueueStatus | null>(null)

  const forceResume = ref(false)

  const queueRecovered = ref(false)

  const queueBlocked = computed(() => Boolean(queue.value?.realtime_code || queue.value?.bulk_code))

  const snapshotRead = useLatestRead()
  const loading = ref(false)
  const errorMessage = ref("")
  async function load(_tab?: string): Promise<void> {
    const signal = snapshotRead.start()
    loading.value = true
    errorMessage.value = ""
    try {
      const result = await getQueueStatus(signal)
      if (!signal.aborted) queue.value = result
    } catch (error) {
      if (!signal.aborted) errorMessage.value = errorText(error, "运维数据加载失败")
    } finally {
      if (!signal.aborted) loading.value = false
    }
  }

  async function recover(): Promise<void> {
    if (
      !(await confirmAuditedAction({
        title: "确认恢复双队列",
        body: forceResume.value
          ? h("p", ["FORCE 已开启：将", h("strong", "绕过余额与暂停原因守卫"), "，实时与批量队列立即恢复投递。"])
          : h("p", "仅在余额达标且暂停码为 999 时恢复，不满足条件时服务端拒绝。"),
        auditNote: "恢复行为、force 取值与操作人将写入审计日志。",
        confirmText: "恢复队列",
      }))
    )
      return
    try {
      const result = await resumeQueue(forceResume.value)
      queueRecovered.value = true
      ElMessage.success(`已恢复 ${result.resumed_batches} 个批次 · 本次操作已记入审计`)
      await load("queue")
    } catch (error) {
      ElMessage.error(errorText(error, "队列恢复失败"))
    }
  }
  return { queue, queueBlocked, queueRecovered, forceResume, loading, errorMessage, load, recover }
}
