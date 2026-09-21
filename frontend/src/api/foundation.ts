import { readApiToken } from "../auth/tokenStorage";

export class FoundationApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function foundationApi<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  signal?.throwIfAborted();
  const payload = body === undefined ? undefined : JSON.stringify(body);
  const controller = new AbortController();
  const cancel = () => controller.abort(signal?.reason);
  signal?.addEventListener("abort", cancel, { once: true });
  let timedOut = false;
  // Bound both the request and response-body read. Keep caller cancellation
  // distinct so a superseded page request cannot render a spurious error.
  const deadline = setTimeout(() => { timedOut = true; controller.abort(); }, 15_000);
  try {
    const response = await fetch(`/api/admin/data-foundation${path}`, {
      method: body === undefined ? "GET" : "POST", signal: controller.signal,
      headers: { Authorization: `Bearer ${readApiToken() ?? ""}`, "Content-Type": "application/json" },
      ...(payload === undefined ? {} : { body: payload })
    });
    if (!response.ok) {
      let message = `数据服务暂不可用（HTTP ${response.status}），请重试。`;
      try { const error = await response.json(); if (typeof error.detail?.message === "string") message = error.detail.message; } catch { /* Keep the safe local summary. */ }
      throw new FoundationApiError(response.status, message);
    }
    return await response.json();
  } catch (error) {
    if (signal?.aborted || error instanceof FoundationApiError) throw error;
    if (timedOut) throw new FoundationApiError(504, "数据服务响应超时，请稍后重试。");
    if (error instanceof SyntaxError) throw new FoundationApiError(502, "数据服务返回格式异常，请稍后重试。");
    throw new FoundationApiError(0, "无法连接数据服务，请检查网络后重试。");
  } finally {
    clearTimeout(deadline);
    signal?.removeEventListener("abort", cancel);
  }
}
export interface Dataset {
  dataset: string; version: string; name: string; series: string; profile: string;
  current_release: string | null; business_as_of: string | null; published_at: string | null; source_observed_at: string | null;
  candidate_work_id?: string | null; pending_governance?: boolean; source_records: number | null; candidate_records: number; official_keys: number; expected_business_keys: number | null; quarantined_records: number;
  range: { from: string | null; to: string | null }; subjects: { instrument_id: string; code: string; name: string }[];
  fields: { key: string; name: string; unit: string }[]; limitations: string[]; work_ids: string[]; work_summaries?: { id: string; label: string }[]; as_of: string;
  contract?: { core_fields: Record<string, {precision:number;scale:number;required:boolean}>; optional_fields: Record<string,{precision:number;scale:number}> };
  support?: {read:string;update:string;replay:string}; projection?: {version:string;hash:string};
  normalization_delay_seconds?: number | null; publication_delay_seconds?: number | null;
}
export interface OfficialResult {
  state: string; request_satisfied: boolean; release_id: string | null; checked_at: string;
  requirements: { id: string; result: string; message: string }[];
  scope_summary: { expected_business_keys: number | null; official_keys: number; currently_readable_keys: number };
  items: { instrument_id: string; trade_date: string; official_id: string; open?: string; high?: string; low?: string; close?: string; volume?: string; turnover?: string }[];
}
export interface ProcessView {
  id: string; kind: string; status: string; current_release: string | null; output_releases: string[];
  input_manifest: { source_ref_id: string | null; candidate_manifest_id: string | null; fingerprint: string; dependency_id: string; execution_id: string };
  counters: { processed: number; total: number | null; unit: string; members?:number };
  steps: { step: string; status: string; events: { work_id?: string; sequence: number; message: string; at: string }[] }[];
  as_of: string; view_snapshot_id: string;
}
export interface Lineage {
  official_id: string; candidate_id: string; decision_id: string; source_ref_id: string; source: string;
  source_hash: string; binding_id: string; dependency_id: string; assessment_id: string;
  policy: { reason: string; comparison: string; field_quality: Record<string,string> };
}

export interface ReadRequest {
  dataset_id: string; contract_version: string; profile_id: string; semantic_series_id: string;
  subjects: string[]; business_range: { from: string; to: string }; fields: string[];
  release: string; require_complete: boolean; allow_partial: boolean; time_mode: string;
  max_staleness_days?: number;
}
export interface ReadResult extends OfficialResult {
  resolution_token?: string | null; expires_at?: number; next_cursor?: string | null;
  next_excluded_cursor?: string | null; excluded_total?: number;
  excluded?: { instrument_id: string; trade_date: string; reason: string }[];
  request: ReadRequest; issue_state_version?: number; has_more?: boolean; total_readable?: number;
  projection_version?: string;
}
export interface ReleaseItem { id: string; parent_id: string | null; published_at: string; manifest_hash: string; work_id: string }
export interface DetailedProcess extends ProcessView {
  rules: Record<string, Record<string, string>>; diagnostic: Record<string, unknown>;
  scope: Record<string, string>;
  steps: (ProcessView['steps'][number] & { detail: { input_works?: {work_id:string; input:Record<string,unknown>; basis:Record<string,unknown>}[]; input: Record<string,string>; processing: string;
    output: { events: number; releases: string[] }; basis: Record<string,unknown>; impact: string; next_step: string } })[];
}
