/** Errors expose local allowlisted summaries, never a server message or raw body. */
export class DataStoreApiError extends Error {
  constructor(public status: number, public code: string, message: string, public field?: string) {
    super(message);
    this.name = "DataStoreApiError";
  }
}

const messages: Record<string, string> = {
  DATASET_UNKNOWN: "未找到该数据集，请返回目录重新选择。",
  DATASET_MISSING: "尚未登记此数据集，请刷新目录。",
  DATA_STORE_REBUILDING: "当前数据暂不可读取，正在维护或重建，请稍后重试。",
  DATA_STORE_NOT_INITIALIZED: "当前数据目录尚未初始化，请等待服务端完成初始化。",
  DATA_RESTRICTED: "本次读取受到数据限制，请查看更新与问题。",
  DATA_CHANGED: "当前数据已改变，请重新读取，旧分页已清除。",
  REBUILD_REQUIRED: "当前数据需要重建，请查看更新与问题。",
  QUERY_BUDGET_EXCEEDED: "查询超过读取预算，请缩小范围或页大小。",
  QUERY_TIMEOUT: "数据服务响应超时，请稍后重试。",
  INVALID_CURSOR: "分页游标已失效，请重新读取。",
  INVALID_VALUE: "查询条件不符合当前契约，请检查条件。",
  FREQUENCY_UNSUPPORTED: "所选频率未获数据集支持，请检查频率。",
  HISTORY_UNSUPPORTED: "当前数据接口未提供历史版本查询。",
  PARTIAL_SCOPE_REQUIRED: "服务端未接受此读取范围，请调整查询条件。",
  CATALOG_UNAVAILABLE: "当前目录暂不可读取，请稍后刷新。",
  STORAGE_UNAVAILABLE: "当前数据存储暂不可读取，请稍后重试。",
  LOCK_TIMEOUT: "当前数据正在处理，请稍后重试。",
  FILE_INVALID: "当前数据文件暂不可读取，请查看更新与问题。",
  INVALID_RESPONSE: "数据服务返回格式异常，请稍后重试。",
  INVALID_REQUEST: "查询条件不符合当前接口，请检查条件。",
  PREVIEW_UNDECLARED: "服务端尚未提供当前对象预览起点。",
  NETWORK_ERROR: "无法连接数据服务，请检查网络后重试。",
  DATA_STORE_UNAVAILABLE: "数据服务暂不可用，请稍后重试。"
};

export function safeReasonCode(value: unknown): string | null {
  return typeof value === "string" && /^[A-Z][A-Z0-9_]{0,63}$/.test(value)
    && !/(TOKEN|SECRET|PASSWORD|BEARER|SQL|DSN)/.test(value) ? value : null;
}

export function apiError(status: number, code: unknown): DataStoreApiError {
  if (status === 401) return new DataStoreApiError(status, "AUTH_REQUIRED", "登录状态已失效，请重新登录。");
  if (status === 403) return new DataStoreApiError(status, "PERMISSION_DENIED", "当前账号无权读取这些数据，请检查访问权限。");
  const safeCode = safeReasonCode(code) ?? "DATA_STORE_UNAVAILABLE";
  return new DataStoreApiError(status, safeCode, messages[safeCode] ?? messages.DATA_STORE_UNAVAILABLE);
}

export function invalidResponse(field: string): never {
  throw new DataStoreApiError(502, "INVALID_RESPONSE", messages.INVALID_RESPONSE, field);
}

export function invalidRequest(field: string): never {
  throw new DataStoreApiError(400, "INVALID_REQUEST", messages.INVALID_REQUEST, field);
}

export function isCancellation(error: unknown): boolean {
  return error instanceof Error && error.name === "AbortError";
}

export function cancellation(): DOMException {
  return new DOMException("操作已取消。", "AbortError");
}

/** These failures revoke the displayed page and its cursor stack. */
export function clearsPreview(error: unknown): boolean {
  return error instanceof DataStoreApiError && ([401, 403].includes(error.status)
    || ["DATA_CHANGED", "INVALID_CURSOR", "DATA_RESTRICTED", "REBUILD_REQUIRED",
      "DATA_STORE_REBUILDING", "DATA_STORE_NOT_INITIALIZED"].includes(error.code));
}
