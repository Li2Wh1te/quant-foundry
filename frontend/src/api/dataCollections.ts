/** Shared native catalogue for existing watchlist selection/diagnostics.
 * Formal market pages, prices, factors and overview use features/market-data/data.
 */
import { readApiToken } from "../auth/tokenStorage";
import { invalidateDataAssetsSession } from "../features/data-assets/data/lifecycle";

export interface EtfCode {
  instrument_id?: string;
  ts_code: string;
  csname: string | null;
  extname: string | null;
  cname: string | null;
  index_code: string | null;
  index_name: string | null;
  list_date: string | null;
  list_status: string;
  exchange: string;
  mgr_name: string | null;
  mgt_fee: string | null;
  etf_type: string | null;
  updated_at: string;
}

export interface EtfPage {
  items: EtfCode[];
  total: number;
  limit: number;
  offset: number;
}

export interface EtfFilters {
  instrumentId?: string;
  keyword?: string;
  exchange?: string;
  listStatus?: string;
  limit?: number;
  offset?: number;
}

export class DataCollectionApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "DataCollectionApiError";
  }
}

function headers(): HeadersInit {
  const token = readApiToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const token = readApiToken();
  // Refreshes inspect persisted facts again; cancellation also guards callers
  // against a response whose JSON finishes after the selection has changed.
  const response = await fetch(path, { headers: headers(), cache: "no-store", signal });
  if ([401, 403].includes(response.status)) {
    invalidateDataAssetsSession(response.status === 401 ? "authentication" : "permission");
    throw new DataCollectionApiError(response.status === 401 ? "登录状态已失效。" : "当前账号无权读取标的目录。", response.status);
  }
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）。`;
    try {
      const body = await response.json() as { detail?: unknown };
      if (typeof body.detail === "string") message = body.detail;
    } catch {
      // Keep the HTTP fallback when a proxy or server sends a non-JSON response.
    }
    throw new DataCollectionApiError(message, response.status);
  }
  const value = await response.json() as T;
  signal?.throwIfAborted();
  if (readApiToken() !== token) throw new DOMException("Credential changed", "AbortError");
  return value;
}

export function listEtfs(filters: EtfFilters, signal?: AbortSignal): Promise<EtfPage> {
  const params = new URLSearchParams();
  if (filters.instrumentId) params.set("instrument_id", filters.instrumentId);
  if (filters.keyword) params.set("keyword", filters.keyword);
  if (filters.exchange) params.set("exchange", filters.exchange);
  if (filters.listStatus) params.set("list_status", filters.listStatus);
  params.set("limit", String(filters.limit ?? 50));
  params.set("offset", String(filters.offset ?? 0));
  return request<EtfPage>(`/api/admin/data-collections/etfs?${params}`, signal);
}
