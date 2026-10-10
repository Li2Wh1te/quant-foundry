import { apiError, DataStoreApiError, invalidRequest, safeReasonCode } from "./errors";
import type { CurrentDataset, CurrentUpdate, PreviewRequest, StatusPresentation } from "./types";
import { validatePreviewRequest } from "./validation";

const current: Record<string, [string, StatusPresentation["tone"]]> = {
  not_checked: ["尚未检查", "neutral"], empty: ["当前为空", "neutral"],
  available: ["当前可用（按服务端声明）", "success"], restricted: ["存在限制", "warning"],
  rebuild_required: ["需要重建", "warning"], rebuilding: ["暂不可读取／处理中", "info"]
};
const updates: Record<string, [string, StatusPresentation["tone"]]> = {
  not_checked: ["尚未检查", "neutral"], processed: ["处理完成", "success"],
  processed_with_issues: ["处理完成，存在问题", "warning"], incomplete: ["处理未完成", "warning"],
  running: ["处理中", "info"], backoff: ["等待重试", "warning"], deferred: ["已延后处理", "warning"],
  failed: ["本次更新失败", "danger"]
};
function present(value: unknown, mapping: typeof current, missing: string): StatusPresentation {
  const known = typeof value === "string" && Object.hasOwn(mapping, value) ? mapping[value] : undefined;
  return { label: known?.[0] ?? missing, tone: known?.[1] ?? "neutral", known: !!known,
    diagnosticCode: typeof value === "string" && /^[a-z][a-z0-9_-]{0,63}$/.test(value) ? value : null };
}
export function currentStatus(value: unknown): StatusPresentation {
  return present(value, current, "状态待确认");
}
export function updateStatus(value: CurrentUpdate | null | undefined): StatusPresentation {
  return value == null ? { label: "暂无更新记录", tone: "neutral", known: false, diagnosticCode: null }
    : present(value.state, updates, "更新状态待确认");
}
const reasons: Record<string, string> = {
  LEGACY_RESTRICTION: "已有数据限制待核对", SOURCE_CONFIRMATION_UNPROVEN: "来源确认依据不足",
  REPORT_INCOMPLETE: "报告内容不完整", ISSUE_BUDGET_EXCEEDED: "问题数量超过处理上限",
  SOURCE_RANGES_BLOCKED: "部分来源范围仍待处理", CURRENT_SOURCE_MISMATCH: "本地来源与当前数据尚未完全对齐",
  LOCAL_UPDATE_BUDGET_EXCEEDED: "本次更新达到处理预算", DATA_RESTRICTED: "当前存在读取限制",
  DATA_CHANGED: "当前数据已改变", REBUILD_REQUIRED: "当前数据需要重建"
};
export function issueReason(value: unknown): StatusPresentation {
  const code = safeReasonCode(value), label = code ? reasons[code] : undefined;
  return { label: label ?? "原因待确认", tone: "warning", known: !!label, diagnosticCode: code };
}
export function frequencyLabel(value: unknown): string {
  const names: Record<string, string> = { daily: "日频", minute: "分钟", tick: "逐笔", report: "报告", object: "按对象" };
  return typeof value === "string" && Object.hasOwn(names, value) ? names[value] : "未声明";
}

/** Formatting never converts exact decimal, large integer, or nanosecond text to Number/Date. */
export function formatValue(value: unknown): string {
  if (value == null) return "未声明";
  if (typeof value === "string" || typeof value === "boolean") return String(value);
  if (typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= Number.MAX_SAFE_INTEGER) return String(value);
  return "未声明";
}
export function formatCount(value: unknown): string {
  let digits: string;
  if (typeof value === "number" && Number.isSafeInteger(value) && value >= 0) digits = String(value);
  else if (typeof value === "string" && /^\d+$/.test(value)) digits = value;
  else return "未声明";
  return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}
export function formatTimestamp(value: unknown): string {
  return typeof value === "string" && value.length > 0 ? value : "暂无记录";
}
export function errorPresentation(error: unknown): string {
  return error instanceof DataStoreApiError ? apiError(error.status, error.code).message
    : "读取失败，请稍后重试。";
}

/** Display-derived facts retain their source/layout qualifiers and do not claim undeclared capabilities. */
export function datasetPresentation(dataset: CurrentDataset) {
  return { name: dataset.name, source: dataset.source, frequency: frequencyLabel(dataset.frequency),
    current: currentStatus(dataset.status), update: updateStatus(dataset.last_update),
    physicalRowCount: formatCount(dataset.row_count), physicalRowLabel: "当前物理存储行",
    rowLayout: dataset.representation, previewRepresentation: dataset.preview_key?.representation ?? "未声明",
    partitionLabel: "存储分区范围", partitionRange: dataset.partition_range,
    units: "未声明", priceBasis: "未声明", businessDateCoverage: "未声明", historicalCapability: "未声明",
    updatedAt: formatTimestamp(dataset.updated_at) };
}

/** Build an example-object request from the actual preview key; it is not a market-wide selector. */
export function buildPreviewRequest(dataset: CurrentDataset,
  options: { columns?: string[]; pageSize?: number } = {}): PreviewRequest {
  const key = dataset.preview_key;
  if (!key) throw apiError(400, "PREVIEW_UNDECLARED");
  if (dataset.frequency === "unknown") invalidRequest("query.frequency");
  return validatePreviewRequest({ dataset: dataset.dataset, frequency: dataset.frequency,
    representation: key.representation, subject: key.subject, from_key: key.object_key, to_key: key.object_key,
    columns: options.columns ?? [], page_size: options.pageSize ?? 20, cursor: null, allow_partial: false });
}
