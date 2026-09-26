import type { Directive } from "vue"

type ActivateHandler = (event: KeyboardEvent) => void

const HANDLER = Symbol("row-activate-handler")

/**
 * 行内详情触发器的键盘激活单点：Enter/Space 触发并阻断冒泡与默认行为，
 * 避免按键穿过控件命中 el-table 的 row-click 造成二次触发。
 * 等价替换手写的 `@keydown.enter/space.stop.prevent` 对。
 * 也可直接挂在可点击行容器（如原生 tr）上：事件源自内部控件（按钮/输入框）时不抢占，
 * 仅当事件目标就是绑定元素本身（焦点在行上）才触发。
 * 注意 VTU 的 trigger("keydown.enter") 派发小写 key="enter" 与 code="Enter"，
 * 故 Enter 判定同时看 key 与 code；Space 兼容 key=" " 与 code="Space"。
 */
export const vRowActivate: Directive<HTMLElement, ActivateHandler> = {
  mounted(el, binding) {
    const listener = (event: KeyboardEvent): void => {
      if (event.target !== el) return
      const isEnter = event.key === "Enter" || event.key === "enter" || event.code === "Enter"
      const isSpace = event.key === " " || event.key === "Spacebar" || event.code === "Space"
      if (!isEnter && !isSpace) return
      event.stopPropagation()
      event.preventDefault()
      binding.value(event)
    }
    ;(el as unknown as Record<symbol, unknown>)[HANDLER] = listener
    el.addEventListener("keydown", listener)
  },
  unmounted(el) {
    const listener = (el as unknown as Record<symbol, unknown>)[HANDLER]
    if (listener) el.removeEventListener("keydown", listener as EventListener)
  },
}
