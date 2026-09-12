import { onScopeDispose, reactive } from "vue"
import { issueAdminStepUp, type AdminIntent } from "../api/adminStepUp"
import { errorText } from "../lib/error"

/** 授权口令只留在当前组件内存；关闭、清会话和销毁都会废弃迟到响应。 */
export function useAdminStepUp() {
  const state = reactive({ open: false, busy: false, password: "", label: "", error: "" })
  let generation = 0
  let disposed = false
  let intent: AdminIntent | null = null
  let settle: ((token: string | null) => void) | null = null

  function cancel(): void {
    generation += 1
    state.password = ""
    state.open = false
    state.busy = false
    state.error = ""
    intent = null
    const resolve = settle
    settle = null
    resolve?.(null)
  }

  function updatePassword(value: string): void {
    if (!disposed && state.open && !state.busy) state.password = value
  }

  async function submit(): Promise<void> {
    if (state.busy || !state.password || !intent || disposed) return
    const current = generation
    let password = state.password
    state.password = ""
    state.busy = true
    state.error = ""
    try {
      const token = await issueAdminStepUp(intent, password)
      if (disposed || generation !== current) return
      const resolve = settle
      settle = null
      state.open = false
      intent = null
      resolve?.(token)
    } catch (error) {
      if (!disposed && generation === current) state.error = errorText(error, "二次认证失败")
    } finally {
      password = ""
      if (!disposed && generation === current) state.busy = false
    }
  }

  async function run<T>(
    value: AdminIntent,
    label: string,
    action: (token: string) => Promise<T>,
  ): Promise<T | undefined> {
    if (disposed) return undefined
    cancel()
    const current = generation
    intent = structuredClone(value)
    state.label = label
    state.open = true
    let token = await new Promise<string | null>((resolve) => {
      settle = resolve
    })
    if (!token || disposed || generation !== current) return undefined
    try {
      return await action(token)
    } finally {
      token = null
    }
  }
  window.addEventListener("sms:session-clearing", cancel)
  onScopeDispose(() => {
    disposed = true
    cancel()
    window.removeEventListener("sms:session-clearing", cancel)
  })
  return { state, run, submit, cancel, updatePassword }
}
export type AdminStepUpController = ReturnType<typeof useAdminStepUp>
