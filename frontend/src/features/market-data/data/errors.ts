import { cancellation, isCancellation } from "../../data-assets/data/errors";
export { cancellation, isCancellation };

export class MarketApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message); this.name = "MarketApiError";
  }
}
const messages: Record<string, string> = {
  AUTH_REQUIRED: "登录状态已失效，请重新登录。", PERMISSION_DENIED: "当前账号无权读取这些市场数据。",
  IDENTITY_UNKNOWN: "未找到获准的标的身份。", IDENTITY_UNAVAILABLE: "缺少可核验的标的代码映射。",
  IDENTITY_AMBIGUOUS: "代码对应多个身份，请使用明确的标的入口。",
  IDENTITY_MAPPING_UNAVAILABLE: "身份映射资料暂不可读取。", CAPABILITY_UNAVAILABLE: "该业务能力尚未提供。",
  PRICE_BASIS_UNVERIFIED: "所选价格口径尚未核验。", UNIT_UNVERIFIED: "所选能力缺少已声明单位。",
  FACTOR_ANCHOR_UNVERIFIED: "复权基准或锚点尚未核验。", FACTOR_UNAVAILABLE: "所选复权模式缺少因子依据。",
  DATA_STORE_REBUILDING: "当前市场数据正在维护，暂不可读取。",
  DATA_STORE_NOT_INITIALIZED: "当前市场数据目录尚未初始化。", DATASET_MISSING: "尚未登记该正式数据。",
  DATA_RESTRICTED: "该市场读取受到数据质量限制。", REBUILD_REQUIRED: "数据结构或口径已改变，请重新读取。",
  DATA_CHANGED: "当前读取依赖已改变，请重新加载，旧分页已清除。", INVALID_CURSOR: "分页已失效，请重新加载。",
  QUERY_BUDGET_EXCEEDED: "读取超过预算，请缩小范围或页大小。", CONTROL_BUDGET_EXCEEDED: "读取条件超过预算。",
  QUERY_TIMEOUT: "市场数据读取超时，请稍后重试。", LOCK_TIMEOUT: "数据正在处理，请稍后重试。",
  CATALOG_UNAVAILABLE: "正式目录暂不可读取。", STORAGE_UNAVAILABLE: "正式存储暂不可读取。",
  FILE_INVALID: "正式数据文件暂不可读取。", INVALID_VALUE: "读取条件不符合契约。",
  INVALID_REQUEST: "读取条件不符合市场接口。", INVALID_RESPONSE: "市场接口返回格式异常。",
  NETWORK_ERROR: "无法连接市场数据服务，请检查网络。", MARKET_UNAVAILABLE: "市场数据服务暂不可用。"
};
/** Only local summaries and allowlisted codes are ever displayed. */
export function marketError(status: number, code?: unknown): MarketApiError {
  const safeCode = status === 401 ? "AUTH_REQUIRED" : status === 403 ? "PERMISSION_DENIED"
    : typeof code === "string" && Object.hasOwn(messages, code) ? code : "MARKET_UNAVAILABLE";
  return new MarketApiError(status, safeCode, messages[safeCode]);
}
export function invalidResponse(): never { throw marketError(502, "INVALID_RESPONSE"); }
export function invalidRequest(): never { throw marketError(400, "INVALID_REQUEST"); }
export function retainStale(error: unknown): boolean {
  return error instanceof MarketApiError && ![401, 403].includes(error.status)
    && ["NETWORK_ERROR", "QUERY_TIMEOUT"].includes(error.code);
}
