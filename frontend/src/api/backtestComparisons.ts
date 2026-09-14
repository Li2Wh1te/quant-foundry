import { readApiToken } from "../auth/tokenStorage";
import { BacktestApiError } from "./backtestRuns";
export type ComparisonRow = Record<string, any>;
export interface SavedComparison { id: string; name: string; run_ids: string[]; baseline_run_id: string; version: number; created_at: string; updated_at: string; }
export interface SavedComparisonPage { items: SavedComparison[]; total: number; offset: number; limit: number; has_more: boolean; }
export interface ComparisonResult { baseline_run_id: string; run_summaries: ComparisonRow[]; equity_curve_series: { run_id: string; points: ComparisonRow[] }[]; metric_matrix: ComparisonRow[]; configuration_diff: ComparisonRow[]; }
async function request<T>(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<T> {
  const response = await fetch(path, { ...init, signal, headers: { Authorization: `Bearer ${readApiToken() || ""}`, "Content-Type": "application/json", ...init.headers } });
  if (!response.ok) { const body = await response.json().catch(() => ({})); const detail = body.detail; throw new BacktestApiError(typeof detail === "string" ? detail : detail?.message || `请求失败（HTTP ${response.status}）`, response.status); }
  return response.status === 204 ? undefined as T : response.json();
}
/** Mutations retain server versions; POST identities are supplied by the caller
 * so an uncertain response can be retried without creating a second save. */
const base = "/api/admin/backtest-comparisons";
export const listComparisons = (offset = 0, signal?: AbortSignal) => request<SavedComparisonPage>(`${base}?limit=20&offset=${offset}`, {}, signal);
export const getComparison = (id: string, signal?: AbortSignal) => request<SavedComparison>(`${base}/${encodeURIComponent(id)}`, {}, signal);
export const saveComparison = (payload: { id: string; name: string; run_ids: string[]; baseline_run_id: string }) => request<SavedComparison>(base, { method: "POST", body: JSON.stringify(payload) });
export const updateComparison = (id: string, payload: { version: number; name?: string; run_ids?: string[]; baseline_run_id?: string }) => request<SavedComparison>(`${base}/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(payload) });
export const deleteComparison = (id: string, version: number) => request<void>(`${base}/${encodeURIComponent(id)}?version=${version}`, { method: "DELETE" });
export const fetchComparison = (run_ids: string[], baseline_run_id: string, signal?: AbortSignal) => request<ComparisonResult>("/api/admin/backtests/compare", { method: "POST", body: JSON.stringify({ run_ids, baseline_run_id }) }, signal);
