/**
 * 统一错误文案提取：浏览器网络异常转为可读提示，其余 Error 保留 message。
 * 全站 catch 分支一律使用本函数，禁止内联 `error instanceof Error ? error.message : …`
 * （ESLint no-restricted-syntax 已拦截回潮）。
 */
export function errorText(error: unknown, fallback: string): string {
  if (
    error instanceof TypeError &&
    ["Failed to fetch", "Load failed", "NetworkError when attempting to fetch resource."].includes(error.message)
  ) {
    return "网络请求未完成，请检查连接。"
  }
  return error instanceof Error ? error.message : fallback
}
