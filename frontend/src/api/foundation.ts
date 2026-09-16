import { readApiToken } from "../auth/tokenStorage";

export class FoundationApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}
export async function foundationApi<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await fetch(`/api/admin/data-foundation${path}`, {
    method: body === undefined ? "GET" : "POST", signal,
    headers: { Authorization: `Bearer ${readApiToken() ?? ""}`, "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) })
  });
  if (!response.ok) {
    let message = `数据服务暂不可用（HTTP ${response.status}），请重试。`;
    try { const error = await response.json(); if (typeof error.detail?.message === "string") message = error.detail.message; } catch { /* Keep the safe local summary. */ }
    throw new FoundationApiError(response.status, message);
  }
  return response.json();
}
export interface Dataset {
  dataset: string; version: string; name: string; series: string; profile: string;
  current_release: string | null; business_as_of: string | null; published_at: string | null; source_observed_at: string | null;
  candidate_work_id?: string | null; pending_governance?: boolean; source_records: number | null; candidate_records: number; official_keys: number; expected_business_keys: number | null; quarantined_records: number;
  range: { from: string | null; to: string | null }; subjects: { instrument_id: string; code: string; name: string }[];
  fields: { key: string; name: string; unit: string }[]; limitations: string[]; work_ids: string[]; work_summaries?: { id: string; label: string }[]; as_of: string;
  normalization_delay_seconds?: number | null; publication_delay_seconds?: number | null;
}
export interface OfficialResult {
  state: string; request_satisfied: boolean; release_id: string | null; checked_at: string;
  requirements: { id: string; result: string; message: string }[];
  scope_summary: { expected_business_keys: number | null; official_keys: number; currently_readable_keys: number };
  items: { instrument_id: string; trade_date: string; official_id: string; close?: string; volume?: string; turnover?: string }[];
}
export interface ProcessView {
  id: string; kind: string; status: string; current_release: string | null; output_releases: string[];
  input_manifest: { source_ref_id: string | null; candidate_manifest_id: string | null; fingerprint: string; dependency_id: string; execution_id: string };
  counters: { processed: number; total: number | null; unit: string };
  steps: { step: string; status: string; events: { sequence: number; message: string; at: string }[] }[];
  as_of: string; view_snapshot_id: string;
}
export interface Lineage {
  official_id: string; candidate_id: string; decision_id: string; source_ref_id: string; source: string;
  source_hash: string; binding_id: string; dependency_id: string; assessment_id: string;
  policy: { reason: string; comparison: string; field_quality: Record<string,string> };
}
