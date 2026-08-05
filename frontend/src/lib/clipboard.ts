/** 复制文本到剪贴板；优先异步剪贴板 API，非安全上下文回退隐藏 textarea。 */
export async function copyText(value: string): Promise<boolean> {
  if (!value) return false
  try {
    if (window.isSecureContext && navigator.clipboard) {
      await navigator.clipboard.writeText(value)
      return true
    }
    const helper = document.createElement("textarea")
    helper.value = value
    helper.setAttribute("readonly", "")
    helper.style.position = "fixed"
    helper.style.opacity = "0"
    document.body.appendChild(helper)
    helper.select()
    const copied = document.execCommand("copy")
    helper.remove()
    return copied
  } catch {
    return false
  }
}
