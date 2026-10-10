/* Existing view wording retained for the D03–D06 content handoff. D01 owns the shared data presentation API. */
export const statusNames: Record<string, string> = {
  rebuilding: "维护重建中",
  not_checked: "尚未检查",
  empty: "当前为空",
  available: "当前可用",
  restricted: "存在限制",
  rebuild_required: "需要重建",
  processed: "处理完成",
  processed_with_issues: "处理完成，存在问题",
  incomplete: "处理未完成",
  running: "处理中"
};
export const issueNames: Record<string, string> = {
  LEGACY_RESTRICTION: "旧数据限制待核对",
  SOURCE_CONFIRMATION_UNPROVEN: "来源确认依据不足",
  REPORT_INCOMPLETE: "报告内容不完整",
  ISSUE_BUDGET_EXCEEDED: "问题数量超过处理上限"
};
export const frequencies: Record<string, string> = {
  daily: "日频", minute: "分钟", tick: "逐笔", report: "报告", object: "按对象"
};

export function time(value: string | null | undefined): string {
  if (!value) return "暂无记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "时间未知" : parsed.toLocaleString("zh-CN", { hour12: false });
}

export function count(value: number | null | undefined): string {
  return value == null ? "未检查" : value.toLocaleString("zh-CN");
}
