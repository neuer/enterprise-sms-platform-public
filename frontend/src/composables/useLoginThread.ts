import { nextTick, ref, type Ref } from "vue"

import type { Chip, Item, MineItem, NewItem, Part } from "../lib/loginConversation"

/** 登录会话消息流：每次调用新建，只持有展示用气泡（密码气泡仅为固定圆点），不承载任何凭据。 */
export function useLoginThread(netWarn: Ref<boolean>) {
  const items = ref<Item[]>([])
  const threadRef = ref<HTMLElement | null>(null)
  let seq = 0

  function leadsGroup(index: number): boolean {
    const previous = items.value[index - 1]
    return !previous || (previous.type !== "bot" && previous.type !== "rules")
  }

  function scrollToEnd(): void {
    void nextTick(() => {
      const thread = threadRef.value
      if (thread) thread.scrollTop = thread.scrollHeight
    })
  }

  function push(item: NewItem): Item {
    items.value.push({ ...item, id: ++seq } as Item)
    scrollToEnd()
    return items.value[items.value.length - 1]
  }

  function bot(parts: Part[], tone?: "err" | "ok"): void {
    push({ type: "bot", parts, tone })
  }

  function sys(text: string, tone?: "warn", testid?: string): void {
    push({ type: "sys", text, tone, testid })
  }

  function chips(list: Chip[]): void {
    push({ type: "chips", chips: list, used: false })
  }

  function lastMine(): MineItem | undefined {
    for (let index = items.value.length - 1; index >= 0; index -= 1) {
      const item = items.value[index]
      if (item.type === "me") return item
    }
    return undefined
  }

  function me(text: string, options: { secret?: boolean; edit?: "provider" | "account" } = {}): void {
    for (const item of items.value) if (item.type === "me") item.receipt = ""
    push({
      type: "me",
      text,
      secret: options.secret ?? false,
      edit: options.edit,
      receipt: options.secret ? "已加密发送" : "已送达",
      failed: false,
    })
  }

  function markRead(): void {
    const mine = lastMine()
    if (mine?.receipt && !mine.failed) mine.receipt = "已读"
  }

  function markUndelivered(message: string): void {
    const mine = lastMine()
    if (mine) {
      mine.failed = true
      mine.receipt = "发送失败"
    }
    netWarn.value = true
    sys(message, "warn")
  }

  return { items, threadRef, leadsGroup, push, bot, sys, chips, me, markRead, markUndelivered }
}
