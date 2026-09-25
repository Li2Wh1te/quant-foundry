import { readApiToken } from "../auth/tokenStorage";

export class DataStoreApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export interface CurrentDataset {
  dataset: string;
  name: string;
  domain: string;
  source: string;
  frequency: string;
  representation: string;
  schema_id: string;
  rule: string;
  fields: { column: string; type: string; meaning: string; logical_type: string; arithmetic: string }[];
  limitations: string[];
  business_key: string[];
  status: "rebuilding" | "not_checked" | "empty" | "available" | "restricted" | "rebuild_required";
  row_count: number | null;
  generation: number | null;
  updated_at: string | null;
  issues: number | null;
  legacy_restrictions?: number;
  last_update: {
    state: string; complete: boolean; qualified: boolean; reason: string | null;
    source_rows: number | null; committed_partitions: number | null; updated_at: string;
  } | null;
  partitions: string[];
  partitions_truncated: boolean;
  partition_range: { from: string | null; to: string | null; precision: "partition" };
  preview_key: { representation: string; subject: string; object_key: string; partition: string } | null;
}

export interface DatasetList {
  items: CurrentDataset[];
  total: number;
  phase: string;
  next_offset: number | null;
}

export interface IssueList {
  items: { dataset: string | null; scope_key: string | null; reason: string; kind: "current" | "legacy"; updated_at: string }[];
  total: number;
  next_offset: number | null;
}

export interface PreviewRequest {
  dataset: string;
  frequency: string;
  representation: string;
  subject: string;
  from_key: string;
  to_key: string;
  columns: string[];
  page_size: number;
  cursor?: string | null;
  allow_partial: boolean;
}

export interface PreviewResult {
  dataset: string;
  status: string;
  request_satisfied: boolean;
  business_date_coverage_verified: boolean;
  partial_requested?: boolean;
  rows: Record<string, string | number | boolean | null>[];
  next_cursor: string | null;
  generation: number | null;
  actual_range: { from: string | null; to: string | null };
  selected_partitions: string[];
  limitations: string[];
  unresolved_issues?: number;
}

export async function dataStoreApi<T>(
  path: string,
  signal?: AbortSignal,
  body?: unknown
): Promise<T> {
  signal?.throwIfAborted();
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, body === undefined ? 15_000 : 30_000);
  try {
    const response = await fetch(`/api/admin/data-store${path}`, {
      method: body === undefined ? "GET" : "POST",
      signal: controller.signal,
      headers: {
        Authorization: `Bearer ${readApiToken() ?? ""}`,
        "Content-Type": "application/json"
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) })
    });
    if (!response.ok) {
      let code = "DATA_STORE_UNAVAILABLE";
      let message = `数据服务暂不可用（HTTP ${response.status}），请重试。`;
      try {
        const error = await response.json() as { detail?: { code?: string; message?: string } };
        if (typeof error.detail?.code === "string") code = error.detail.code;
        if (typeof error.detail?.message === "string") message = error.detail.message;
      } catch { /* Keep the safe local summary. */ }
      throw new DataStoreApiError(response.status, code, message);
    }
    return await response.json() as T;
  } catch (error) {
    if (signal?.aborted || error instanceof DataStoreApiError) throw error;
    if (timedOut) throw new DataStoreApiError(504, "QUERY_TIMEOUT", "数据服务响应超时，请稍后重试。");
    if (error instanceof SyntaxError) throw new DataStoreApiError(502, "INVALID_RESPONSE", "数据服务返回格式异常，请稍后重试。");
    throw new DataStoreApiError(0, "NETWORK_ERROR", "无法连接数据服务，请检查网络后重试。");
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener("abort", cancel);
  }
}
