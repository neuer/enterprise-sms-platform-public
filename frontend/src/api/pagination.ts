/**
 * 分页响应公共形状单点（对应 openapi.yaml 各列表端点 200 响应）。
 * 契约存在两种分页外壳：
 * - Page<T>：仅 items/total（/messages/batches、/messages 号码搜索、/web/replies、
 *   /web/approvals、/admin/blacklist、/admin/callbacks、/admin/sensitive-words），
 *   请求侧页大小参数名为 size；
 * - NumberedPage<T>：额外回显 page/page_size（/admin/audit-logs、/admin/users、
 *   /admin/security-daily/reports 及 ops.ts 各运维列表），请求侧参数名为 page_size。
 * ops.ts 的 OpsPage<T> 与 admin.ts 的 AuditPage 均为本类型的别名引用。
 */
export interface Page<T> {
  items: T[]
  total: number
}

export interface NumberedPage<T> extends Page<T> {
  page: number
  page_size: number
}
