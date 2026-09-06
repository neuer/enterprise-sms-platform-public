/** 触发浏览器下载：Blob → 临时 ObjectURL → 锚点点击后立即回收。 */
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement("a")
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}
