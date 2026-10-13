import { readApiToken } from "../auth/tokenStorage";
import { invalidateDataAssetsSession } from "../features/data-assets/data/lifecycle";
import { DataCollectionApiError } from "./dataCollections";

export interface WatchItem {
  ts_code: string;
  name: string | null;
  exchange: string | null;
  created_at: string;
  trade_date: string | null;
  close: string | null;
  pre_close: string | null;
  change: string | null;
  pct_chg: string | null;
  price_basis: "raw_daily_close";
  message: string;
}
export interface WatchPage {
  items: WatchItem[];
  limit: number;
  offset: number;
  has_more: boolean;
}

async function request<T>(
  path: string,
  method: string,
  signal?: AbortSignal,
): Promise<T> {
  const token = readApiToken();
  const response = await fetch(`/api/admin/etf-watchlist${path}`, {
    method,
    signal,
    cache: "no-store",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if ([401, 403].includes(response.status)) {
    invalidateDataAssetsSession(response.status === 401 ? "authentication" : "permission");
    throw new DataCollectionApiError(response.status === 401 ? "登录状态已失效。" : "当前账号无权读取自选。", response.status);
  }
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new DataCollectionApiError(
      typeof body?.detail === "string"
        ? body.detail
        : `自选操作失败（HTTP ${response.status}）。`,
      response.status,
    );
  signal?.throwIfAborted();
  if (readApiToken() !== token) throw new DOMException("Credential changed", "AbortError");
  return body as T;
}
export const listWatchlist = (offset = 0, signal?: AbortSignal) =>
  request<WatchPage>(`?limit=50&offset=${offset}`, "GET", signal);
export const setWatched = (code: string, watched: boolean) =>
  request<{ changed: boolean; message: string }>(
    `/${encodeURIComponent(code)}`,
    watched ? "PUT" : "DELETE",
  );
