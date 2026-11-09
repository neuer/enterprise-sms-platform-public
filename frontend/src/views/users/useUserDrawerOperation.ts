import { computed, onScopeDispose, ref, watch, type Ref } from "vue"
import { SESSION_CLEARING_EVENT } from "../../api/sessionEvents"
import { useConfirmActions } from "../../lib/confirm"

/** 把账号写入绑定到发起它的抽屉草稿；关闭后仍允许请求完成，但不能改动新草稿。 */
export function useUserDrawerOperation(open: Ref<boolean>, clearDraft: () => void) {
  const { captureCurrent } = useConfirmActions()
  const generation = ref(0)
  const pending = ref<number | null>(null)
  const saving = computed(() => pending.value === generation.value)

  function retire(): void {
    generation.value += 1
    clearDraft()
  }
  watch(
    open,
    (value) => {
      if (!value) retire()
    },
    { flush: "sync" },
  )
  window.addEventListener(SESSION_CLEARING_EVENT, retire)
  onScopeDispose(() => {
    retire()
    window.removeEventListener(SESSION_CLEARING_EVENT, retire)
  })

  function begin() {
    if (!open.value || saving.value) return null
    const owner = generation.value
    const isCurrent = captureCurrent()
    pending.value = owner
    return {
      isCurrent,
      ownsDraft: () => isCurrent() && open.value && generation.value === owner,
      finish: () => {
        if (pending.value === owner) pending.value = null
      },
    }
  }
  return { saving, retire, begin }
}
