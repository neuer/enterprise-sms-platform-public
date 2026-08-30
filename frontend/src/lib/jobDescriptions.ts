/** 后台任务的中文用途说明，供仪表盘和运维中心共享。 */
export const JOB_DESCRIPTIONS: Readonly<Record<string, string>> = {
  aggregate_stats: "聚合最近三日的消息统计并写入每日统计数据",
  anomaly_scan: "按应用和消息类别扫描发送量异常并生成告警",
  cleanup_exports: "清理已过期的加密导出文件",
  dispatch_callbacks: "扫描待投递的回调任务并投递到回调队列",
  dispatch_exports: "扫描导出任务并投递到导出队列",
  dispatch_imports: "投递加密导入文件的分块解析、批量写入和崩溃恢复任务",
  dispatch_scheduled: "扫描到点的定时批次并投递到发送队列",
  expire_approvals: "将超过有效期的审批申请置为过期",
  housekeeping: "执行业务数据生命周期清理和导入文件清理",
  poll_balance: "定时轮询厂商余额并更新平台余额快照",
  poll_reply: "轮询厂商上行回复，保存原始报文后解析入库",
  poll_report: "轮询厂商状态报告，保存原始报文后解析并更新发送结果",
  reconcile: "对账结果未知的发送分片，并恢复可恢复的投递或联调操作",
  sync_signs: "同步待处理签名的厂商状态",
  sync_templates: "同步待处理模板的厂商状态",
  reconcile_usage_projection: "恢复超时预留；确认漂移后按事实账本覆盖 Redis 投影并复核",
}

const UNKNOWN_JOB_DESCRIPTION = "用途说明暂未登记，请查阅任务定义"

export function jobDescription(jobName: string): string {
  return JOB_DESCRIPTIONS[jobName] ?? UNKNOWN_JOB_DESCRIPTION
}
