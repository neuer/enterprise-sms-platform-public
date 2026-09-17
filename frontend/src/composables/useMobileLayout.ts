import { onScopeDispose, readonly, ref, type Ref } from "vue"

/** 跟随共享 CSS 的 760px 断点，仅挂载当前列表布局，离开页面时释放监听。 */
export function useMobileLayout(): Readonly<Ref<boolean>> {
  const media =
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(max-width: 760px)")
      : undefined
  const isMobile = ref(media?.matches ?? false)
  const update = (event: MediaQueryListEvent): void => {
    isMobile.value = event.matches
  }
  media?.addEventListener("change", update)
  onScopeDispose(() => media?.removeEventListener("change", update))
  return readonly(isMobile)
}
