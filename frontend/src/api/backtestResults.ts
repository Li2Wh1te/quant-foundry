import { readRunResource, RunApiError } from "./runService";

export type ResultRow = Record<string, any>;
export type ResultKind = "equity" | "orders" | "trades" | "positions" | "records" | "logs";
export const RESULT_LIMIT = 100;
export interface ResultSummary extends ResultRow { schema_id: string; run_id: string; partial: boolean }
export interface ResultPage extends ResultSummary { items: ResultRow[]; next_cursor: string | null }
export interface CompareItem extends ResultRow { run_id: string; engine_kind: string; status: string; partial: boolean; config: ResultRow }
export interface CompareResult { items: CompareItem[]; ranking: null; warnings: string[] }
const base = "/api/admin/backtests";
const hidden = new Set<(ids: string[]) => void>();
export function onResultHidden(callback: (ids: string[]) => void) { hidden.add(callback); return () => { hidden.delete(callback); }; }
async function read<T>(path: string, ids: string[], init: RequestInit, signal?: AbortSignal): Promise<T> {
  try { return await readRunResource<T>(path, init, signal); }
  catch (e) { if (e instanceof RunApiError && e.status === 404) for (const callback of hidden) callback(ids); throw e; }
}
function envelope(value: ResultSummary, id: string) {
  if (!value || value.run_id !== id || !["qf.results.v1", "legacy"].includes(value.schema_id) || typeof value.partial !== "boolean") throw new RunApiError(502, "INVALID_CONTRACT");
}
export async function resultSummary(id: string, signal?: AbortSignal): Promise<ResultSummary> {
  const value = await read<ResultSummary>(`${base}/${encodeURIComponent(id)}/results/summary?limit=${RESULT_LIMIT}`, [id], {}, signal);
  envelope(value, id); return value;
}
/** Exactly one bounded service page. No auto-follow or client-side full Tick cache. */
export async function resultPage(id: string, kind: ResultKind, cursor?: string, signal?: AbortSignal): Promise<ResultPage> {
  const query = new URLSearchParams({ limit: String(RESULT_LIMIT) }); if (cursor) query.set("cursor", cursor);
  const value = await read<ResultPage>(`${base}/${encodeURIComponent(id)}/results/${kind}?${query}`, [id], {}, signal);
  envelope(value, id);
  if (!Array.isArray(value.items) || value.items.length > RESULT_LIMIT || value.items.some(row => !row || typeof row !== "object" || Array.isArray(row)) ||
    value.next_cursor != null && (typeof value.next_cursor !== "string" || !value.next_cursor || value.next_cursor === cursor) || value.has_more && !value.next_cursor) throw new RunApiError(502, "INVALID_CONTRACT");
  if (value.schema_id === "qf.results.v1") {
    const fields = ["time_ns", "submitted_ns", "execution_time_ns", "result_sequence", "price", "limit_price", "fee", "equity", "cash", "average_cost", "market_value"];
    if (value.items.some(row => fields.some(field => row[field] != null && typeof row[field] !== "string"))) throw new RunApiError(502, "INVALID_CONTRACT");
    let previous: bigint | null = null;
    for (const row of value.items) {
      if (typeof row.result_sequence !== "string" || !/^[0-9]+$/.test(row.result_sequence)) throw new RunApiError(502, "INVALID_CONTRACT");
      const sequence = BigInt(row.result_sequence); if (previous !== null && sequence <= previous) throw new RunApiError(502, "INVALID_CONTRACT"); previous = sequence;
    }
  }
  return { ...value, next_cursor: value.next_cursor ?? null };
}
export async function compareResults(ids: string[], baseline: string, signal?: AbortSignal): Promise<CompareResult> {
  if (ids.length < 2 || ids.length > 16 || new Set(ids).size !== ids.length || !ids.includes(baseline)) throw new RunApiError(422, "INVALID_RUN_CONFIG");
  const ordered = [baseline, ...ids.filter(id => id !== baseline)];
  const value = await read<CompareResult>(`${base}/compare`, ids, { method: "POST", body: JSON.stringify({ run_ids: ordered }) }, signal);
  if (!Array.isArray(value.items) || value.items.length !== ids.length || new Set(value.items.map(row => row.run_id)).size !== ids.length ||
    value.items.some(row => !ids.includes(row.run_id) || !["rust", "legacy_python"].includes(row.engine_kind) || typeof row.partial !== "boolean" || !row.config) ||
    value.ranking !== null || !Array.isArray(value.warnings)) throw new RunApiError(502, "INVALID_CONTRACT");
  return value;
}
