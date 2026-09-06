/**
 * 统一错误文案提取：Error 实例取 message，其余（取消标记、非 Error 抛出物）回退到固定文案。
 * 全站 catch 分支一律使用本函数，禁止内联 `error instanceof Error ? error.message : …`
 * （ESLint no-restricted-syntax 已拦截回潮）。
 */
export function errorText(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}
