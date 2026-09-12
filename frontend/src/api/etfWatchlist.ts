import { readApiToken } from "../auth/tokenStorage";
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
  const body = await response.json().catch(() => null);
  if (!response.ok)
    throw new DataCollectionApiError(
      typeof body?.detail === "string"
        ? body.detail
        : `自选操作失败（HTTP ${response.status}）。`,
      response.status,
    );
  signal?.throwIfAborted();
  return body as T;
}
export const listWatchlist = (offset = 0, signal?: AbortSignal) =>
  request<WatchPage>(`?limit=50&offset=${offset}`, "GET", signal);
export const setWatched = (code: string, watched: boolean) =>
  request<{ changed: boolean; message: string }>(
    `/${encodeURIComponent(code)}`,
    watched ? "PUT" : "DELETE",
  );
