import { readRunResource, RunApiError } from "./runService";
export type ComparisonRow = Record<string, any>;
export interface SavedComparison { id: string; name: string; run_ids: string[]; baseline_run_id: string; version: number; created_at: string; updated_at: string }
export interface SavedComparisonPage { items: SavedComparison[]; total: number; offset: number; limit: number; has_more: boolean }
const base = "/api/admin/backtest-comparisons";
function saved(value: SavedComparison) {
  if (!value || typeof value.id !== "string" || typeof value.name !== "string" || !Array.isArray(value.run_ids) || value.run_ids.length < 2 || value.run_ids.length > 10 || new Set(value.run_ids).size !== value.run_ids.length || !value.run_ids.includes(value.baseline_run_id)) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
/** Existing saved definitions remain readable; no historical writes or reruns. */
export async function listComparisons(offset = 0, signal?: AbortSignal) {
  const value = await readRunResource<SavedComparisonPage>(`${base}?limit=20&offset=${offset}`, {}, signal);
  if (!Array.isArray(value.items) || value.items.length > 20 || typeof value.has_more !== "boolean") throw new RunApiError(502, "INVALID_CONTRACT");
  value.items.forEach(saved); return value;
}
export async function getComparison(id: string, signal?: AbortSignal) {
  const value = saved(await readRunResource<SavedComparison>(`${base}/${encodeURIComponent(id)}`, {}, signal));
  if (value.id !== id) throw new RunApiError(502, "INVALID_CONTRACT"); return value;
}
